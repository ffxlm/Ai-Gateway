import json
import time
import asyncio
import httpx
from typing import AsyncGenerator, Dict, Any
from app.core.config import settings
from app.core.database import get_setting
from app.services.user_service import atomic_record_usage, is_vip_active

# Concurrency control semaphores
FREE_QUEUE_SEMAPHORE = asyncio.Semaphore(settings.FREE_CONCURRENCY_LIMIT)
VIP_QUEUE_SEMAPHORE = asyncio.Semaphore(settings.VIP_CONCURRENCY_LIMIT)

async def forward_chat_completion(user: Dict[str, Any], payload: Dict[str, Any]):
    """
    Handles priority queuing and reverse-proxying OpenAI-compatible chat completion
    requests to the master 9Router upstream.
    """
    upstream_url = get_setting("master_router_url", settings.MASTER_ROUTER_URL).rstrip("/") + "/v1/chat/completions"
    upstream_key = get_setting("master_router_key", settings.MASTER_ROUTER_KEY)
    
    is_vip = is_vip_active(user)
    is_stream = payload.get("stream", False)
    model = payload.get("model", "unknown")
    
    # Priority Queue Assignment: VIP gets fast-track high-capacity semaphore
    sem = VIP_QUEUE_SEMAPHORE if is_vip else FREE_QUEUE_SEMAPHORE
    
    async with sem:
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
            return await handle_streaming_proxy(client, upstream_url, headers, payload, user, model, start_time)
        else:
            return await handle_non_streaming_proxy(client, upstream_url, headers, payload, user, model, start_time)

async def handle_non_streaming_proxy(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, user: dict, model: str, start_time: float):
    try:
        resp = await client.post(url, headers=headers, json=payload)
        latency = (time.time() - start_time) * 1000
        
        if resp.status_code == 200:
            raw_text = resp.text.strip()
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

            tokens = data.get("usage", {}).get("total_tokens", 0)
            if tokens == 0:
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                tokens = max(len(content) // 4 + 50, 100)
            
            atomic_record_usage(user["id"], tokens, model=model, latency_ms=latency, status_code=200, is_vip=is_vip_active(user))
            return resp.status_code, "application/json", clean_bytes
        else:
            return resp.status_code, "application/json", resp.content
    finally:
        await client.aclose()

async def handle_streaming_proxy(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, user: dict, model: str, start_time: float):
    req = client.build_request("POST", url, headers=headers, json=payload)
    resp = await client.send(req, stream=True)
    
    if resp.status_code != 200:
        err_content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return resp.status_code, "application/json", err_content
    
    async def stream_generator() -> AsyncGenerator[bytes, None]:
        total_tokens = 0
        collected_chunks = 0
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
                # Parse SSE usage if available
                chunk_str = chunk.decode("utf-8", errors="ignore")
                for line in chunk_str.split("\n"):
                    if line.startswith("data: ") and not line.strip().endswith("[DONE]"):
                        try:
                            parsed = json.loads(line[6:].strip())
                            if "usage" in parsed and parsed["usage"]:
                                usage = parsed["usage"]
                                if usage.get("total_tokens"):
                                    total_tokens = usage["total_tokens"]
                            collected_chunks += 1
                        except Exception:
                            pass
            
            # Fallback estimation if upstream didn't send usage in stream
            if total_tokens == 0:
                total_tokens = max(collected_chunks * 3, 50)
            
            latency = (time.time() - start_time) * 1000
            atomic_record_usage(user["id"], total_tokens, model=model, latency_ms=latency, status_code=200, is_vip=is_vip_active(user))
        finally:
            await resp.aclose()
            await client.aclose()
            
    return 200, "text/event-stream", stream_generator()

async def fetch_upstream_models() -> Dict[str, Any]:
    specified = [
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "GLM-5.3-Flash", "object": "model", "owned_by": "zhipu"},
        {"id": "grok-4.7-xhigh", "object": "model", "owned_by": "xai"},
        {"id": "grok-4.7", "object": "model", "owned_by": "xai"},
        {"id": "qwen3.8-27b", "object": "model", "owned_by": "qwen"},
        {"id": "MiniMax-M2.7", "object": "model", "owned_by": "minimax"},
        {"id": "muse-spark-1.3", "object": "model", "owned_by": "muse"}
    ]
    return {
        "object": "list",
        "data": specified
    }
