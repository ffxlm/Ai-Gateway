"""Single source of truth for the model catalog.

Both the public dashboard (``app.routers.pages``) and the API layer
(``app.services.proxy_service``) read the catalog from here so the free and
premium model lists can never drift apart.
"""

from app.core.config import settings

# Free tier: unlimited usage, never charged against the wallet.
FREE_MODELS = [
    "deepseek-v4-flash",
    "GLM-5.3-Flash",
    "MiniMax-M2.7",
]

# Premium "xHigh" tier: billed per token from the user's USD wallet.
#
# Each model may declare its own ``trial_tokens_per_day`` free daily allowance.
# When omitted (as for DeepSeek below) the global ``premium_trial_tokens_per_day``
# setting is used instead, so the Admin panel stays in control of the default.
# Set it to 0 to give a model no free trial at all.
#
# ``max_output_tokens`` bounds the admission-control estimate: a request can
# never reserve more than this many output tokens, which is what keeps a single
# request from ever driving the wallet negative.
PREMIUM_MODELS = [
    {
        "id": "deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "provider": "DeepSeek",
        "tag": "Flagship Reasoning",
        "price_in_usd": 0.015,    # our price, per 1M input tokens
        "price_out_usd": 0.06,    # our price, per 1M output tokens
        "official_in_usd": 0.15,  # official list price, struck through on the card
        "official_out_usd": 0.60,
        "discount": 90,           # % cheaper than official
        "max_output_tokens": 32768,
    },
]

_PREMIUM_BY_ID = {m["id"]: m for m in PREMIUM_MODELS}


def is_premium_model(model_id: str) -> bool:
    return model_id in _PREMIUM_BY_ID


def get_premium_model(model_id: str):
    return _PREMIUM_BY_ID.get(model_id)


def premium_trial_tokens(model_id: str):
    """Per-model daily free-trial allowance, or None to use the global setting."""
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return None
    return m.get("trial_tokens_per_day")


def premium_price(model_id: str, tokens_in: int, tokens_out: int) -> float:
    """USD cost for a premium request, or 0.0 for free/unknown models."""
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return 0.0
    return (max(tokens_in, 0) / 1_000_000.0) * m["price_in_usd"] + \
           (max(tokens_out, 0) / 1_000_000.0) * m["price_out_usd"]


def premium_output_price(model_id: str) -> float:
    """USD per 1M output tokens (the pricier side, used for worst-case holds)."""
    m = _PREMIUM_BY_ID.get(model_id)
    return float(m["price_out_usd"]) if m else 0.0


def premium_max_output_tokens(model_id: str) -> int:
    """Hard cap on generated tokens for admission control (per-model or global)."""
    m = _PREMIUM_BY_ID.get(model_id)
    if m and m.get("max_output_tokens"):
        try:
            return max(int(m["max_output_tokens"]), 1)
        except (TypeError, ValueError):
            pass
    return max(int(settings.PREMIUM_MAX_OUTPUT_TOKENS), 1)


def _owner(model_id: str) -> str:
    low = model_id.lower()
    if "deepseek" in low:
        return "deepseek"
    if "glm" in low:
        return "zhipu"
    if "minimax" in low:
        return "minimax"
    return "gateway"


def api_models() -> list:
    """Model list exposed by GET /v1/models (free + premium)."""
    data = [{"id": mid, "object": "model", "owned_by": _owner(mid)} for mid in FREE_MODELS]
    for m in PREMIUM_MODELS:
        data.append({
            "id": m["id"],
            "object": "model",
            "owned_by": _owner(m["id"]),
            "tier": "xhigh",
            "pricing": {
                "input_per_1m_usd": m["price_in_usd"],
                "output_per_1m_usd": m["price_out_usd"],
            },
        })
    return data
