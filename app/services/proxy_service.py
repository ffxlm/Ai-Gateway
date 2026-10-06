import json
import time
import asyncio
import httpx
from typing import AsyncGenerator, Dict, Any, Tuple
from app.core.config import settings
from app.core.database import get_setting
from app.core.catalog import api_models, premium_max_output_tokens
from app.services.user_service import record_usage, release_reservation

# Concurrency control: one shared queue for every request.
REQUEST_SEMAPHORE = asyncio.Semaphore(settings.CONCURRENCY_LIMIT)


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
    requests to the master 9Router upstream.
    """
    upstream_url = get_setting("master_router_url", settings.MASTER_ROUTER_URL).rstrip("/") + "/v1/chat/completions"
    upstream_key = get_setting("master_router_key", settings.MASTER_ROUTER_KEY)

    is_stream = payload.get("stream", False)
    model = payload.get("model", "unknown")

    async with REQUEST_SEMAPHORE:
        start_time = time.time()
        headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json"
        }

        # Ensure upstream streams include usage statistics if possible
        if is_stream and "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}

        client = httpx.AsyncClient(timeout=180.0)

        if is_stream:
            return await handle_streaming_proxy(client, upstream_url, headers, payload, user, model, start_time, reservation_id)
        else:
            return await handle_non_streaming_proxy(client, upstream_url, headers, payload, user, model, start_time, reservation_id)


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
            return resp.status_code, "application/json", resp.content

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
                return 200, "application/json", json.dumps(aggregated).encode("utf-8")

        if "data: [DONE]" in raw_text:
            raw_text = raw_text.split("data: [DONE]")[0].strip()

        first_brace = raw_text.find("{")
        last_brace = raw_text.rfind("}")
        if first_brace != -1 and last_brace != -1:
            clean_json_str = raw_text[first_brace:last_brace+1]
            data = json.loads(clean_json_str)
            clean_bytes = clean_json_str.encode("utf-8")
        else:
            data = resp.json()
            clean_bytes = resp.content

        tokens_in, tokens_out = _token_breakdown(data.get("usage") or {})
        source = "upstream"
        if tokens_in == 0 and tokens_out == 0:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            tokens_out = max(len(content) // 4 + 50, 100)
            source = "estimated"

        record_usage(user["id"], model, tokens_in, tokens_out, latency_ms=latency,
                     status_code=200, usage_source=source, reservation_id=reservation_id)
        return resp.status_code, "application/json", clean_bytes
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
        return resp.status_code, "application/json", err_content

    async def stream_generator() -> AsyncGenerator[bytes, None]:
        tokens_in = 0
        tokens_out = 0
        collected_chunks = 0
        settled = False
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
                # Parse SSE usage if available
                chunk_str = chunk.decode("utf-8", errors="ignore")
                for line in chunk_str.split("\n"):
                    if line.startswith("data: ") and not line.strip().endswith("[DONE]"):
                        try:
                            parsed = json.loads(line[6:].strip())
                            if parsed.get("usage"):
                                tokens_in, tokens_out = _token_breakdown(parsed["usage"])
                            collected_chunks += 1
                        except Exception:
                            pass

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
