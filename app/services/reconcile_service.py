"""Read-only margin reconciliation against the premium upstream (InferHub).

This module deliberately does **not** touch customer billing. It answers one
operational question: *did premium traffic make money, and did we capture every
request we paid for?* It compares three numbers for a date window:

  1. ``billed_usd``            -- what the portal charged customers for premium
                                  models (``Σ request_logs.cost_usd``).
  2. ``observed_upstream_usd`` -- what the portal *saw* the upstream charge, per
                                  response (``Σ request_logs.upstream_cost_usd``).
  3. ``upstream_cost_usdc``    -- what the upstream account actually spent
                                  (InferHub ``/api/usage/logs`` totals).

Gross margin is (1) - (3). The gap between (2) and (3) is the important signal:
if the upstream spent materially more than we observed, usage was billed to us
that the portal never saw -- estimated streams, or the upstream key being used
outside the portal. Neither is fixed by charging customers after the fact (that
cannot be matched to a request reliably); it is surfaced here for an operator to
act on at the source.

Reconciliation only makes sense for premium models: free models route through a
different bridge (9Router), not this provider's account.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.database import db_session, get_setting

# The upstream usage API returns amounts as decimal strings; treat them as
# opaque and parse defensively.
_HTTP_TIMEOUT = 20.0


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


def portal_premium_totals(from_utc: datetime, to_utc: datetime) -> Dict[str, Any]:
    """Aggregate the portal's own premium request logs for a UTC window.

    ``request_logs.created_at`` is stored in the business timezone, so the UTC
    bounds are converted to local wall-clock before the comparison.
    """
    local_from = _to_local_naive(from_utc).strftime("%Y-%m-%d %H:%M:%S")
    local_to = _to_local_naive(to_utc).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as conn:
        row = conn.cursor().execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(cost_usd), 0) AS billed_usd,
                COALESCE(SUM(upstream_cost_usd), 0) AS observed_upstream_usd,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(tokens_cached), 0) AS tokens_cached,
                COALESCE(SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END), 0) AS estimated_requests,
                COALESCE(SUM(unbilled_usd), 0) AS unbilled_usd
            FROM request_logs
            WHERE is_premium = 1 AND created_at >= ? AND created_at < ?
            """,
            (local_from, local_to),
        ).fetchone()
    return {
        "requests": int(row["requests"] or 0),
        "billed_usd": round(float(row["billed_usd"] or 0), 8),
        "observed_upstream_usd": round(float(row["observed_upstream_usd"] or 0), 8),
        "tokens_in": int(row["tokens_in"] or 0),
        "tokens_out": int(row["tokens_out"] or 0),
        "tokens_cached": int(row["tokens_cached"] or 0),
        "estimated_requests": int(row["estimated_requests"] or 0),
        "unbilled_usd": round(float(row["unbilled_usd"] or 0), 8),
    }


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


async def reconcile(days: int = 7) -> Dict[str, Any]:
    """Compare portal premium billing against upstream spend for a window."""
    from_utc, to_utc = _utc_day_bounds(days)
    portal = portal_premium_totals(from_utc, to_utc)

    upstream: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        upstream = await fetch_upstream_totals(from_utc, to_utc)
    except Exception as exc:  # network/config failure: still return the portal side
        error = str(exc)

    result: Dict[str, Any] = {
        "days": int(days),
        "from_utc": from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_utc": to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "portal": portal,
        "upstream": upstream,
        "upstream_error": error,
    }

    if upstream is not None:
        paid = upstream["cost_usdc"]
        billed = portal["billed_usd"]
        result["margin"] = {
            "gross_usd": round(billed - paid, 8),
            "ratio": round(billed / paid, 3) if paid > 0 else None,
            "observed_vs_upstream_gap_usd": round(portal["observed_upstream_usd"] - paid, 8),
        }
    return result
