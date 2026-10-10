"""Read-only margin reconciliation against the premium upstream (InferHub).

This module deliberately does **not** touch customer billing. It answers one
operational question: *did premium traffic make money, and did we capture every
request we paid for?* It reports three numbers for a date window:

  1. ``billed_usd``            -- what the portal charged customers for premium
                                  models (``Σ request_logs.cost_usd``).
  2. ``observed_upstream_usd`` -- what the portal *saw* the upstream charge, per
                                  response (``Σ request_logs.upstream_cost_usd``,
                                  taken from the provider's own ``usage.cost``).
  3. ``upstream_cost_usdc``    -- what the upstream account actually spent
                                  (InferHub ``/api/usage/logs`` totals).

Two margins are reported, because they answer different questions:

  * **business margin** (``billed_usd - total_cost_usd``) is the honest one: it
    prices every request we can measure against the provider's own reported
    cost, so it stays correct even when the upstream account is shared with
    other tools. This is the number to trust for "are we making money?".
  * **gross margin vs account** (``billed_usd - upstream_cost_usdc``) compares
    our revenue against the *whole account's* spend. The gap between the
    observed cost and the account spend is the leak signal: usage billed to the
    account that the portal never saw (a shared key, or estimated streams).

**Fresh measurement epoch.** The provider only started returning per-request
``usage.cost`` recently, so every request logged before that point carries a
``upstream_cost_usd`` of 0 and cannot be measured. Rather than let that old data
drag the numbers down, an operator can set a ``metrics_epoch_start``: the
business figures then count only requests at/after that instant. Old rows are
left untouched (they are still the customer's billing history); they are simply
excluded from the *margin* measurement.

Reconciliation only makes sense for premium models: free models route through a
different bridge (9Router), not this provider's account.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.database import db_session, get_setting, update_setting

# The upstream usage API returns amounts as decimal strings; treat them as
# opaque and parse defensively.
_HTTP_TIMEOUT = 20.0

# Local wall-clock format used by ``request_logs.created_at`` (SQLite
# ``datetime('now','localtime')``), so the epoch can be compared as text.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_EPOCH_SETTING = "metrics_epoch_start"


def _management_base() -> str:
    base = get_setting("premium_management_url", settings.PREMIUM_MANAGEMENT_URL) or ""
    return base.strip().rstrip("/")


def _utc_day_bounds(days: int) -> Tuple[datetime, datetime]:
    """Half-open [from, to) UTC day bounds covering the last ``days`` days.

    Days are whole UTC calendar days so the same window can be sent to the
    upstream usage API (which is UTC) and applied to our local-time logs.
    """
    days = max(int(days), 1)
    now = datetime.now(timezone.utc)
    to_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    from_day = to_day - timedelta(days=days)
    return from_day, to_day


def _to_local_naive(dt_utc: datetime) -> datetime:
    try:
        return dt_utc.astimezone(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)
    except Exception:
        return dt_utc.replace(tzinfo=None)


def _now_local_str() -> str:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None).strftime(_TS_FORMAT)


# --------------------------------------------------------------------------- #
# Fresh-measurement epoch
# --------------------------------------------------------------------------- #

def metrics_epoch() -> Optional[str]:
    """The stored measurement epoch (local ``YYYY-MM-DD HH:MM:SS``), or None."""
    raw = (get_setting(_EPOCH_SETTING, "") or "").strip()
    return raw or None


def set_metrics_epoch(value: Optional[str]) -> Optional[str]:
    """Persist (or clear) the measurement epoch. Returns the stored value."""
    clean = (value or "").strip()
    update_setting(_EPOCH_SETTING, clean)
    return clean or None


def start_fresh_epoch() -> str:
    """Start a fresh measurement window at the current local time."""
    now = _now_local_str()
    set_metrics_epoch(now)
    return now


# --------------------------------------------------------------------------- #
# Portal side
# --------------------------------------------------------------------------- #

def portal_premium_totals(from_utc: datetime, to_utc: datetime,
                          epoch_local: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate the portal's own premium request logs for a UTC window.

    ``request_logs.created_at`` is stored in the business timezone, so the UTC
    bounds are converted to local wall-clock before the comparison. When
    ``epoch_local`` is set, the window's lower bound is raised to it, so only
    requests from the fresh measurement point onward are counted.
    """
    window_from = _to_local_naive(from_utc).strftime(_TS_FORMAT)
    local_to = _to_local_naive(to_utc).strftime(_TS_FORMAT)
    local_from = window_from
    if epoch_local and epoch_local > local_from:
        local_from = epoch_local

    with db_session() as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(cost_usd), 0) AS billed_usd,
                COALESCE(SUM(upstream_cost_usd), 0) AS observed_upstream_usd,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(tokens_cached), 0) AS tokens_cached,
                COALESCE(SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END), 0) AS estimated_requests,
                COALESCE(SUM(unbilled_usd), 0) AS unbilled_usd,
                -- Split the *measured* upstream cost into the part earned on
                -- paid tokens and the part given away as free trial, in
                -- proportion to the tokens on each side of the request.
                COALESCE(SUM(CASE WHEN (trial_tokens + paid_tokens) > 0
                    THEN upstream_cost_usd * paid_tokens * 1.0 / (trial_tokens + paid_tokens)
                    ELSE upstream_cost_usd END), 0) AS paid_cost_usd,
                COALESCE(SUM(CASE WHEN (trial_tokens + paid_tokens) > 0
                    THEN upstream_cost_usd * trial_tokens * 1.0 / (trial_tokens + paid_tokens)
                    ELSE 0 END), 0) AS trial_cost_usd
            FROM request_logs
            WHERE is_premium = 1 AND created_at >= ? AND created_at < ?
            """,
            (local_from, local_to),
        ).fetchone()

        # Fallback for estimated requests (the upstream never reported usage, so
        # their cost is 0). Price their tokens at the measured average cost per
        # token of the same model, so an unverifiable request still shows a cost
        # instead of silently looking free.
        measured = {
            r["model"]: (float(r["cost"] or 0), int(r["tokens"] or 0))
            for r in cursor.execute(
                """
                SELECT model,
                       COALESCE(SUM(upstream_cost_usd), 0) AS cost,
                       COALESCE(SUM(tokens_used), 0) AS tokens
                FROM request_logs
                WHERE is_premium = 1 AND created_at >= ? AND created_at < ?
                      AND upstream_cost_usd > 0 AND tokens_used > 0
                GROUP BY model
                """,
                (local_from, local_to),
            ).fetchall()
        }
        estimated_cost = 0.0
        for r in cursor.execute(
            """
            SELECT model, COALESCE(SUM(tokens_used), 0) AS tokens
            FROM request_logs
            WHERE is_premium = 1 AND created_at >= ? AND created_at < ?
                  AND usage_source = 'estimated'
            GROUP BY model
            """,
            (local_from, local_to),
        ).fetchall():
            cost, tokens = measured.get(r["model"], (0.0, 0))
            if tokens > 0 and cost > 0:
                estimated_cost += int(r["tokens"] or 0) * (cost / tokens)

    observed = round(float(row["observed_upstream_usd"] or 0), 8)
    return {
        "requests": int(row["requests"] or 0),
        "billed_usd": round(float(row["billed_usd"] or 0), 8),
        "observed_upstream_usd": observed,
        "measured_cost_usd": observed,
        "estimated_cost_usd": round(estimated_cost, 8),
        "total_cost_usd": round(observed + estimated_cost, 8),
        "paid_cost_usd": round(float(row["paid_cost_usd"] or 0), 8),
        "trial_cost_usd": round(float(row["trial_cost_usd"] or 0), 8),
        "tokens_in": int(row["tokens_in"] or 0),
        "tokens_out": int(row["tokens_out"] or 0),
        "tokens_cached": int(row["tokens_cached"] or 0),
        "estimated_requests": int(row["estimated_requests"] or 0),
        "unbilled_usd": round(float(row["unbilled_usd"] or 0), 8),
        "from_local": local_from,
        "to_local": local_to,
        "window_from_local": window_from,
    }


# --------------------------------------------------------------------------- #
# Upstream side
# --------------------------------------------------------------------------- #

async def fetch_upstream_totals(from_utc: datetime, to_utc: datetime) -> Dict[str, Any]:
    """Fetch the upstream account's own usage totals for the same UTC window.

    Raises ``httpx.HTTPError``/``ValueError`` on failure; the caller turns that
    into a visible "upstream unavailable" state rather than a wrong margin.
    """
    base = _management_base()
    key = get_setting("premium_upstream_key", settings.PREMIUM_UPSTREAM_KEY)
    if not base or not key:
        raise ValueError("premium management URL/key not configured")

    params = {"from": from_utc.strftime("%Y-%m-%d"), "to": to_utc.strftime("%Y-%m-%d")}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(
            f"{base}/usage/logs",
            params=params,
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    def _num(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "requests": int(_num(data.get("rangeTotal") or data.get("total"))),
        "cost_usdc": round(_num(data.get("totalCostUsdc")), 8),
        "tokens": int(_num(data.get("totalTokens"))),
        "saved_usdc": round(_num(data.get("totalSavedUsdc")), 8),
    }


async def fetch_upstream_account() -> Dict[str, Any]:
    """Fetch the upstream account's identity and current balance.

    ``consumer_balance`` is spendable credit; ``fiat_pendings`` is money that has
    been funded but not yet credited. Surfacing it lets an operator see the
    account running dry *before* premium requests start failing. Raises on
    failure so the caller can show "unavailable" rather than a stale zero.
    """
    base = _management_base()
    key = get_setting("premium_upstream_key", settings.PREMIUM_UPSTREAM_KEY)
    if not base or not key:
        raise ValueError("premium management URL/key not configured")

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(f"{base}/me", headers={"Authorization": f"Bearer {key}"})
        resp.raise_for_status()
        data = resp.json()

    def _num(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    balances = data.get("balances") or {}
    return {
        "email": data.get("email"),
        "display_name": data.get("displayName"),
        "status": data.get("status"),
        "balance_usdc": round(_num(balances.get("consumer_balance")), 8),
        "fiat_pending_usdc": round(_num(balances.get("fiat_pendings")), 8),
    }


# --------------------------------------------------------------------------- #
# Reconcile
# --------------------------------------------------------------------------- #

def _business_margin(billed: float, portal: Dict[str, Any]) -> Dict[str, Any]:
    """Self-measured business margin: revenue minus every cost we can measure.

    Uses the provider's own reported cost per request (plus the estimated
    fallback), so it is unaffected by other consumers of a shared upstream key.
    """
    measured = float(portal["total_cost_usd"] or 0)
    paid_cost = float(portal["paid_cost_usd"] or 0)
    return {
        "measured_cost_usd": round(measured, 8),
        "business_gross_usd": round(billed - measured, 8),
        "business_ratio": round(billed / measured, 3) if measured > 0 else None,
        "paid_cost_usd": round(paid_cost, 8),
        "paid_gross_usd": round(billed - paid_cost, 8),
        "paid_ratio": round(billed / paid_cost, 3) if paid_cost > 0 else None,
        "trial_cost_usd": round(float(portal["trial_cost_usd"] or 0), 8),
    }


async def reconcile(days: int = 7) -> Dict[str, Any]:
    """Compare portal premium billing against upstream spend for a window."""
    from_utc, to_utc = _utc_day_bounds(days)
    epoch = metrics_epoch()
    portal = portal_premium_totals(from_utc, to_utc, epoch_local=epoch)

    # Both live on the same management API; fetch them together. Each is
    # optional: a failure is reported, never raised, so the portal side (which
    # is the part that must never be wrong) is always returned.
    async def _safe(coro):
        try:
            return await coro, None
        except Exception as exc:  # network/config failure
            return None, str(exc)

    (upstream, error), (account, account_error) = await asyncio.gather(
        _safe(fetch_upstream_totals(from_utc, to_utc)),
        _safe(fetch_upstream_account()),
    )

    result: Dict[str, Any] = {
        "days": int(days),
        "from_utc": from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_utc": to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "measurement": {
            "epoch": epoch,
            "measuring_fresh": bool(epoch),
            "window_from_local": portal["window_from_local"],
            "window_to_local": portal["to_local"],
            "effective_from_local": portal["from_local"],
            "effective_to_local": portal["to_local"],
            # True when the epoch clips the window's start, so the portal side
            # covers a shorter period than the (whole-window) account total.
            "epoch_after_window_start": bool(epoch and portal["from_local"] > portal["window_from_local"]),
        },
        "portal": portal,
        "upstream": upstream,
        "upstream_error": error,
        "account": account,
        "account_error": account_error,
    }

    if upstream is not None:
        paid = upstream["cost_usdc"]
        billed = portal["billed_usd"]
        margin: Dict[str, Any] = {
            # vs the whole upstream account (cross-check / leak detector)
            "gross_usd": round(billed - paid, 8),
            "ratio": round(billed / paid, 3) if paid > 0 else None,
            "observed_vs_upstream_gap_usd": round(portal["observed_upstream_usd"] - paid, 8),
        }
        margin.update(_business_margin(billed, portal))
        result["margin"] = margin
    return result
