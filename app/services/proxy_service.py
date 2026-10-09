import json
import time
import codecs
import httpx
from typing import AsyncGenerator, Dict, Any, Tuple
from app.core.config import settings
from app.core.database import get_setting
from app.core.catalog import api_models, premium_max_output_tokens, upstream_model_id, is_premium_model
from app.services.user_service import record_usage, release_reservation
from app.services.concurrency import acquire_slot, release_slot


def _build_chat_url(base: str) -> str:
    """Normalise a provider base URL into its chat-completions endpoint.

    Accepts bases with or without a trailing ``/v1`` so both the 9Router bridge
    (``https://api.thirx.com``) and the premium provider
    (``https://api.inferhub.dev/v1``) can be stored exactly as issued.
    """
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/v1/chat/completions"


def resolve_upstream(model: str) -> Tuple[str, str, str]:
    """Map a public model id to (chat_url, api_key, upstream_model).

    Free models keep flowing through the shared 9Router bridge. Premium models
    are routed to their own provider using the private ``upstream_model`` name
    from the catalog, so the provider's real naming never leaves this function.
    """
    if is_premium_model(model):
        base = get_setting("premium_upstream_url", settings.PREMIUM_UPSTREAM_URL)
        key = get_setting("premium_upstream_key", settings.PREMIUM_UPSTREAM_KEY)
    else:
        base = get_setting("master_router_url", settings.MASTER_ROUTER_URL)
        key = get_setting("master_router_key", settings.MASTER_ROUTER_KEY)
    return _build_chat_url(base), key, upstream_model_id(model)


def _sanitize_error(status: int, raw: bytes, context: str = "") -> Tuple[int, str, bytes]:
    """Convert an upstream error into a neutral portal error envelope.

    The raw body is logged server-side only: provider identity, host names and
    upstream request ids must never reach the client. The returned status code
    is also remapped so an upstream auth failure cannot be mistaken for the
    client's own credentials being wrong.

    ``context`` (public model, resolved upstream model, upstream URL) is added to
    the server log so an operator can tell a *config* mistake (wrong model id or
    URL) from a genuine upstream outage, without ever leaking it to the client.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = str(raw)
    where = f" {context}" if context else ""
    print(f"[upstream-error]{where} status={status} body={text[:2000]}")

    if status == 429:
        code, message = 429, "Upstream rate limit reached. Please retry shortly."
    elif status in (400, 404, 413, 422):
        code, message = 400, "The upstream provider rejected this request."
    else:
        # 401/403/5xx and anything unexpected are our problem, not the client's.
        code, message = 502, "Upstream provider is unavailable. Please try again."
    body = json.dumps({"error": {"message": message, "type": "upstream_error", "code": code}}).encode("utf-8")
    return code, "application/json", body


def _rewrite_sse_line(line: str, public_model: str) -> str:
    """Swap the upstream model name for our public id on a single SSE line."""
    if not line.startswith("data:"):
        return line
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return line
    try:
        obj = json.loads(payload)
    except Exception:
        return line
    if not isinstance(obj, dict) or obj.get("model") == public_model:
        return line
    obj["model"] = public_model
    cr = "\r" if line.endswith("\r") else ""
    return "data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + cr


def _count_text_tokens(text: Any) -> int:
    """Rough token estimate for a string (~4 chars/token)."""
    if not isinstance(text, str) or not text:
        return 0
    return max(len(text) // 4, 1)


def estimate_prompt_tokens(payload: Dict[str, Any]) -> int:
    """Estimate input tokens from the request payload for admission control.

    Deliberately conservative and cheap: no tokenizer dependency, just a
    character heuristic plus a small per-message overhead.
    """
    total = 0
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        total += 4  # per-message role/format overhead
        content = msg.get("content")
        if isinstance(content, str):
            total += _count_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _count_text_tokens(part.get("text") or "")
        if msg.get("name"):
            total += _count_text_tokens(str(msg["name"]))
    for tool in payload.get("tools") or []:
        try:
            total += _count_text_tokens(json.dumps(tool))
        except (TypeError, ValueError):
            pass
    return total


def estimate_request_tokens(payload: Dict[str, Any], model: str) -> Tuple[int, int]:
    """Worst-case (input, output) token estimate used to size the wallet hold."""
    est_in = estimate_prompt_tokens(payload)
    try:
        requested = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
    except (TypeError, ValueError):
        requested = 0
    cap = premium_max_output_tokens(model)
    est_out = requested if 0 < requested <= cap else cap
    return est_in, est_out


def _token_breakdown(usage: Dict[str, Any]) -> Tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from an upstream usage object."""
    if not usage:
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt == 0 and completion == 0:
        # Only a total is available: attribute it to output (the pricier side).
        return 0, int(usage.get("total_tokens") or 0)
    return prompt, completion


async def forward_chat_completion(user: Dict[str, Any], payload: Dict[str, Any],
                                  reservation_id: int = 0):
    """
    Handles queuing and reverse-proxying [OI]-compatible chat completion
    requests to the resolved upstream (9Router for free, the premium provider
    for billed models).

    The client's model id (``public_model``) is kept for billing and for every
    response we emit. Only the outbound copy of the payload carries the
    provider's real model name, so the origin never leaks.
    """
    public_model = payload.get("model", "unknown")
    upstream_url, upstream_key, provider_model = resolve_upstream(public_model)

    is_stream = payload.get("stream", False)

    # Reserve capacity: a per-user slot first (fairness), then a slot in the
    # pool for this upstream (free vs premium). The slot is held only until the
    # upstream response starts; for streams the transfer then runs unbuffered.
    pool = await acquire_slot(user["id"], is_premium_model(public_model))
    try:
        start_time = time.time()
        headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json"
        }

        # Never mutate the caller's payload: build the outbound copy explicitly.
        outbound = dict(payload)
        outbound["model"] = provider_model
        # Ensure upstream streams include usage statistics if possible
        if is_stream and "stream_options" not in outbound:
            outbound["stream_options"] = {"include_usage": True}

        client = httpx.AsyncClient(timeout=180.0)

        if is_stream:
            return await handle_streaming_proxy(client, upstream_url, headers, outbound, user, public_model, start_time, reservation_id)
        else:
            return await handle_non_streaming_proxy(client, upstream_url, headers, outbound, user, public_model, start_time, reservation_id)
    finally:
        await release_slot(user["id"], pool)


def _aggregate_sse(raw_text: str, model: str) -> Dict[str, Any]:
    """Collapse an SSE chat-completion stream into a single completion object.

    Some upstream models stream even when ``stream=false`` was requested, so the
    non-streaming proxy path uses this to rebuild a normal JSON response (and to
    recover token usage from the final chunk). Returns None if no chunks found.
    """
    content_parts: list = []
    reasoning_parts: list = []
    role = "assistant"
    finish_reason = None
    completion_id = None
    created = None
    usage = None
    saw_chunk = False

    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        saw_chunk = True
        completion_id = obj.get("id", completion_id)
        created = obj.get("created", created)
        if obj.get("usage"):
            usage = obj["usage"]
        for choice in obj.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("role"):
                role = delta["role"]
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    if not saw_chunk:
        return None

    message: Dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    result: Dict[str, Any] = {
        "id": completion_id or "chatcmpl",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        result["usage"] = usage
    return result


async def handle_non_streaming_proxy(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, user: dict, model: str, start_time: float, reservation_id: int = 0):
    try:
        resp = await client.post(url, headers=headers, json=payload)
        latency = (time.time() - start_time) * 1000

        if resp.status_code != 200:
            # Nothing was consumed upstream: drop the hold immediately.
            release_reservation(reservation_id)
            return _sanitize_error(
                resp.status_code, resp.content,
                context=f"public={model} upstream={payload.get('model')} url={url}",
            )

        raw_text = resp.text.strip()
        content_type = resp.headers.get("content-type", "")

        # Upstream may stream even though stream=false; rebuild a JSON completion.
        if "text/event-stream" in content_type or raw_text.startswith("data:"):
            aggregated = _aggregate_sse(raw_text, model)
            if aggregated is not None:
                tokens_in, tokens_out = _token_breakdown(aggregated.get("usage") or {})
                source = "upstream"
                if tokens_in == 0 and tokens_out == 0:
                    content = aggregated["choices"][0]["message"].get("content", "")
                    tokens_out = max(len(content) // 4 + 50, 100)
                    source = "estimated"
                record_usage(user["id"], model, tokens_in, tokens_out, latency_ms=latency,
                             status_code=200, usage_source=source, reservation_id=reservation_id)
                return 200, "application/json", json.dumps(aggregated, ensure_ascii=False).encode("utf-8")

        if "data: [DONE]" in raw_text:
            raw_text = raw_text.split("data: [DONE]")[0].strip()

        first_brace = raw_text.find("{")
        last_brace = raw_text.rfind("}")
        if first_brace != -1 and last_brace != -1:
            data = json.loads(raw_text[first_brace:last_brace+1])
        else:
            data = resp.json()

        # Never echo the provider's own model name back to the client.
        if isinstance(data, dict):
            data["model"] = model
        clean_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")

        tokens_in, tokens_out = _token_breakdown(data.get("usage") or {})
        source = "upstream"
        if tokens_in == 0 and tokens_out == 0:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            tokens_out = max(len(content) // 4 + 50, 100)
            source = "estimated"

        record_usage(user["id"], model, tokens_in, tokens_out, latency_ms=latency,
                     status_code=200, usage_source=source, reservation_id=reservation_id)
        return 200, "application/json", clean_bytes
    except Exception:
        release_reservation(reservation_id)
        raise
    finally:
        await client.aclose()


async def handle_streaming_proxy(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, user: dict, model: str, start_time: float, reservation_id: int = 0):
    req = client.build_request("POST", url, headers=headers, json=payload)
    resp = await client.send(req, stream=True)

    if resp.status_code != 200:
        err_content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        release_reservation(reservation_id)
        return _sanitize_error(
            resp.status_code, err_content,
            context=f"public={model} upstream={payload.get('model')} url={url}",
        )

    async def stream_generator() -> AsyncGenerator[bytes, None]:
        # Incremental decoder so a multi-byte UTF-8 character split across two
        # transport chunks is not corrupted when we re-emit the stream.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        tokens_in = 0
        tokens_out = 0
        collected_chunks = 0
        settled = False
        try:
            async for chunk in resp.aiter_raw():
                buffer += decoder.decode(chunk)
                # Re-emit whole lines so the provider's model name can be
                # replaced with our public id on every SSE event.
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    raw_line = line.rstrip("\r")
                    if raw_line.startswith("data: ") and not raw_line.strip().endswith("[DONE]"):
                        try:
                            parsed = json.loads(raw_line[6:].strip())
                            if parsed.get("usage"):
                                tokens_in, tokens_out = _token_breakdown(parsed["usage"])
                            collected_chunks += 1
                        except Exception:
                            pass
                    yield (_rewrite_sse_line(line, model) + "\n").encode("utf-8")

            tail = buffer + decoder.decode(b"", final=True)
            if tail:
                yield _rewrite_sse_line(tail, model).encode("utf-8")

            # Fallback estimation if upstream didn't send usage in stream
            source = "upstream"
            if tokens_in == 0 and tokens_out == 0:
                tokens_out = max(collected_chunks * 3, 50)
                source = "estimated"

            latency = (time.time() - start_time) * 1000
            record_usage(user["id"], model, tokens_in, tokens_out, latency_ms=latency,
                         status_code=200, usage_source=source, reservation_id=reservation_id)
            settled = True
        finally:
            if not settled:
                # Client disconnected or the stream errored before settlement:
                # free the hold now instead of waiting for it to expire.
                release_reservation(reservation_id)
            await resp.aclose()
            await client.aclose()

    return 200, "text/event-stream", stream_generator()


async def fetch_upstream_models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": api_models(),
    }
