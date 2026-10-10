"""Single source of truth for the model catalog.

Both the public dashboard (``app.routers.pages``) and the API layer
(``app.services.proxy_service``) read the catalog from here so the free and
premium model lists can never drift apart.
"""

from decimal import Decimal, InvalidOperation, ROUND_DOWN

from app.core.config import settings

# Free tier: unlimited usage, never charged against the wallet.
FREE_MODELS = [
    "deepseek-v4-flash",
    "GLM-5.3-Flash",
    "MiniMax-M2.7",
]

# Upstream (InferHub) bills a prompt-cache read at 10% of the normal input
# rate. Measured from ``usage.cost`` on cb/deepseek-v4.1-flash, cb/gpt-6-sol and
# cb/gpt-6-astra: the cached-input rate is 0.099–0.100x of the input ask. We
# pass the *same* ratio through to customers, so the cache margin equals the
# normal margin instead of being captured (or, if set too low, lost).
CACHE_INPUT_RATIO = 0.10

# Premium "xHigh" tier: billed per token from the user's USD wallet.
#
# Each model may declare its own ``trial_tokens_per_day`` free daily allowance.
# Admin settings can override each model's allowance. When no override exists,
# the catalog value is used, or the global ``premium_trial_tokens_per_day``
# setting when omitted (as for DeepSeek below).
# Set it to 0 to give a model no free trial at all.
#
# ``max_output_tokens`` bounds the admission-control estimate: a request can
# never reserve more than this many output tokens, which is what keeps a single
# request from ever driving the wallet negative.
PREMIUM_MODELS = [
    {
        "id": "deepseek-v4.1-flash",
        # ``upstream_model`` is the name the premium provider expects. It is only
        # ever used when building the outbound request; clients and the public
        # model list keep seeing ``id`` above, so the origin stays hidden.
        "upstream_model": "cb/deepseek-v4.1-flash",
        "name": "deepseek-v4.1-flash",
        "provider": "DeepSeek",
        "tag": "Flagship Reasoning",
        "price_in_usd": 0.015,    # our price, per 1M input tokens
        "price_cached_in_usd": 0.0015,  # our price for a cache-read input token (0.1x)
        "price_out_usd": 0.06,    # our price, per 1M output tokens
        "official_in_usd": 0.15,  # official list price, struck through on the card
        "official_out_usd": 0.60,
        "discount": 90,           # % cheaper than official
        "max_output_tokens": 32768,
    },
    {
        "id": "gpt-6-sol",
        "name": "gpt-6-sol",
        "provider": "[OI]",
        "upstream_model": "cb/gpt-6-sol",
        "tag": "Frontier Agentic",
        "price_in_usd": 0.20,     # our price, per 1M input tokens
        "price_cached_in_usd": 0.02,    # cache-read input (0.1x)
        "price_out_usd": 1.00,    # our price, per 1M output tokens
        "official_in_usd": 2.00,  # official list price, struck through on the card
        "official_out_usd": 10.00,
        "discount": 90,           # % cheaper than official
        # max_output_tokens omitted on purpose: falls back to the global
        # PREMIUM_MAX_OUTPUT_TOKENS setting so Admin keeps control.
        "trial_tokens_per_day": 200000,  # 200k free tokens/day for this model
    },
    {
        "id": "gpt-6-astra",
        "name": "gpt-6-astra",
        "provider": "[OI]",
        "upstream_model": "cb/gpt-6-astra",
        "tag": "Frontier Reasoning",
        "price_in_usd": 1.00,     # our price, per 1M input tokens
        "price_cached_in_usd": 0.10,    # cache-read input (0.1x)
        "price_out_usd": 5.00,    # our price, per 1M output tokens
        "official_in_usd": 10.00, # official list price, struck through on the card
        "official_out_usd": 50.00,
        "discount": 90,           # % cheaper than official
        # max_output_tokens omitted on purpose: falls back to the global
        # PREMIUM_MAX_OUTPUT_TOKENS setting so Admin keeps control.
        "trial_tokens_per_day": 50000,  # 50k free tokens/day for this model
    },
]

_PREMIUM_BY_ID = {m["id"]: m for m in PREMIUM_MODELS}


def price_savings(current, reference) -> str | None:
    """Display savings against a reference price, never a hard-coded discount."""
    try:
        current, reference = Decimal(str(current)), Decimal(str(reference))
        if not current.is_finite() or not reference.is_finite():
            return None
        if current < 0 or reference <= 0 or current >= reference:
            return None
        percent = ((reference - current) / reference * 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if percent <= 0:
            return None
        return format(percent, "f").rstrip("0").rstrip(".")
    except (InvalidOperation, TypeError, ValueError):
        return None


def is_premium_model(model_id: str) -> bool:
    return model_id in _PREMIUM_BY_ID


def get_premium_model(model_id: str):
    return _PREMIUM_BY_ID.get(model_id)


def upstream_model_id(model_id: str) -> str:
    """Provider-side model name for a public id (falls back to the public id)."""
    m = _PREMIUM_BY_ID.get(model_id)
    if m and m.get("upstream_model"):
        return m["upstream_model"]
    return model_id


def premium_trial_tokens(model_id: str):
    """Per-model daily free-trial allowance, or None to use the global setting."""
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return None
    return m.get("trial_tokens_per_day")


def premium_trial_setting_key(model_id: str) -> str:
    """Persistent admin override for a public model's daily allowance."""
    return f"premium_trial_tokens_per_day:{model_id}"


def premium_price(model_id: str, tokens_in: int, tokens_out: int,
                  tokens_cached: int = 0) -> float:
    """USD cost for a premium request, or 0.0 for free/unknown models.

    ``tokens_in`` is the *total* prompt size (cache hits + misses) and
    ``tokens_cached`` is the cache-read subset, billed at the model's cached
    input rate. ``tokens_cached`` is clamped to ``tokens_in`` so a misbehaving
    upstream cannot bill a negative uncached count.
    """
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return 0.0
    tokens_in = max(int(tokens_in or 0), 0)
    tokens_out = max(int(tokens_out or 0), 0)
    tokens_cached = min(max(int(tokens_cached or 0), 0), tokens_in)
    uncached_in = tokens_in - tokens_cached
    cached_price = m.get("price_cached_in_usd")
    if cached_price is None:
        # No verified cache rate for this model: bill the full input rate so we
        # can never sell cache below its cost.
        cached_price = m["price_in_usd"]
    return (uncached_in / 1_000_000.0) * m["price_in_usd"] + \
           (tokens_cached / 1_000_000.0) * cached_price + \
           (tokens_out / 1_000_000.0) * m["price_out_usd"]


def premium_cached_input_price(model_id: str) -> float:
    """USD per 1M cache-read input tokens.

    Falls back to the full input rate when a model has no verified cache rate,
    so an unconfigured model is never discounted below cost.
    """
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return 0.0
    cached = m.get("price_cached_in_usd")
    return float(m["price_in_usd"] if cached is None else cached)


def premium_input_price(model_id: str) -> float:
    """USD per 1M input tokens (the cheaper side of the price pair)."""
    m = _PREMIUM_BY_ID.get(model_id)
    return float(m["price_in_usd"]) if m else 0.0


def premium_output_price(model_id: str) -> float:
    """USD per 1M output tokens (the pricier side, used for worst-case holds)."""
    m = _PREMIUM_BY_ID.get(model_id)
    return float(m["price_out_usd"]) if m else 0.0


def premium_worst_case_cost(model_id: str, tokens_in: int, tokens_out: int,
                            trial_remaining: int = 0) -> float:
    """Worst-case USD a request can cost, after the free trial is applied.

    Prices input and output tokens at their *own* rates instead of multiplying
    the whole estimate by the output rate. That single-rate shortcut over-held up
    to ~4x (input is 4x cheaper on the DeepSeek tier) and could reject a wallet
    that could actually pay.

    The trial split mirrors ``user_service.record_usage`` exactly: the daily
    allowance is consumed from input tokens first, then output tokens, so the
    hold and the eventual charge agree on what is billable.
    """
    m = _PREMIUM_BY_ID.get(model_id)
    if not m:
        return 0.0
    tokens_in = max(int(tokens_in or 0), 0)
    tokens_out = max(int(tokens_out or 0), 0)
    trial_remaining = max(int(trial_remaining or 0), 0)

    free_in = min(tokens_in, trial_remaining)
    remaining = trial_remaining - free_in
    free_out = min(tokens_out, remaining)
    paid_in = tokens_in - free_in
    paid_out = tokens_out - free_out

    return (paid_in / 1_000_000.0) * m["price_in_usd"] + \
           (paid_out / 1_000_000.0) * m["price_out_usd"]


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
    if "gpt" in low:
        return "openai"
    if "claude" in low:
        return "anthropic"
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
                "cached_input_per_1m_usd": premium_cached_input_price(m["id"]),
                "output_per_1m_usd": m["price_out_usd"],
            },
        })
    return data
