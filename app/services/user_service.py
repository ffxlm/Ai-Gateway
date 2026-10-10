import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from app.core.database import db_session, get_setting
from app.core.config import settings
from app.core.catalog import (
    is_premium_model, premium_price, premium_trial_tokens, premium_trial_setting_key,
    premium_input_price, premium_cached_input_price,
)

def generate_api_key() -> str:
    return f"sk-portal-{secrets.token_urlsafe(32)}"

def now_local() -> datetime:
    """Naive wall-clock time in the configured business timezone (default Asia/Bangkok)."""
    try:
        return datetime.now(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)
    except Exception:
        return datetime.now()

def get_today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

def get_or_create_user(discord_id: str, username: str, avatar_url: str = "", role: str = "user") -> Dict[str, Any]:
    admin_ids = [x.strip() for x in settings.ADMIN_DISCORD_IDS.split(",") if x.strip()]
    is_superadmin = str(discord_id) in admin_ids

    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (discord_id,))
        row = cursor.fetchone()

        if row:
            user = dict(row)
            # Ensure superadmin role persists
            if is_superadmin and user["role"] != "admin":
                cursor.execute("UPDATE users SET role = 'admin' WHERE id = ?", (discord_id,))
                user["role"] = "admin"
            # Update avatar/username if changed
            cursor.execute("UPDATE users SET username = ?, avatar_url = ? WHERE id = ?", (username, avatar_url, discord_id))
            return user
        else:
            api_key = generate_api_key()
            cursor.execute("SELECT COUNT(*) AS total FROM users")
            user_count = cursor.fetchone()["total"]
            assigned_role = "admin" if (user_count == 0 or is_superadmin) else role

            cursor.execute("""
            INSERT INTO users (id, username, avatar_url, api_key, role, tier, balance)
            VALUES (?, ?, ?, ?, ?, 'free', 0)
            """, (discord_id, username, avatar_url, api_key, assigned_role))
            cursor.execute("SELECT * FROM users WHERE id = ?", (discord_id,))
            return dict(cursor.fetchone())

def get_user_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE api_key = ?", (api_key,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

# ─────────────────────────────────────────────────────────────────────────────
# Premium free trial (per-model daily token allowance before wallet billing)
# ─────────────────────────────────────────────────────────────────────────────

def premium_trial_limit(model: str) -> int:
    """Daily free-trial allowance for a model.

    An admin override wins, followed by the catalog's per-model default,
    then the legacy global ``premium_trial_tokens_per_day`` setting.
    """
    configured = get_setting(premium_trial_setting_key(model), None)
    if configured is not None:
        try:
            return max(int(configured), 0)
        except (TypeError, ValueError):
            pass
    override = premium_trial_tokens(model)
    if override is not None:
        try:
            return max(int(override), 0)
        except (TypeError, ValueError):
            pass
    try:
        return max(int(get_setting("premium_trial_tokens_per_day", str(settings.PREMIUM_TRIAL_TOKENS_PER_DAY))), 0)
    except (TypeError, ValueError):
        return settings.PREMIUM_TRIAL_TOKENS_PER_DAY

def premium_trial_used(user_id: str, model: str) -> int:
    """Tokens already consumed today for this model (0 once the day rolls over)."""
    with db_session() as conn:
        row = conn.cursor().execute(
            "SELECT tokens_used FROM model_trial_usage WHERE user_id = ? AND model = ? AND usage_date = ?",
            (user_id, model, get_today_str()),
        ).fetchone()
        return int(row["tokens_used"]) if row else 0

def premium_trial_remaining(user_id: str, model: str) -> int:
    return max(premium_trial_limit(model) - premium_trial_used(user_id, model), 0)

def premium_trial_status(user_id: str, model: str) -> Dict[str, Any]:
    """Everything the dashboard needs to render a model's trial meter."""
    limit = premium_trial_limit(model)
    used = premium_trial_used(user_id, model)
    remaining = max(limit - used, 0)
    pct = min(int((used / limit) * 100), 100) if limit > 0 else 0
    return {"model": model, "limit": limit, "used": used, "remaining": remaining, "pct": pct}

# ─────────────────────────────────────────────────────────────────────────────
# Wallet
# ─────────────────────────────────────────────────────────────────────────────

def get_balance(user_id: str) -> float:
    with db_session() as conn:
        row = conn.cursor().execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        return float(row["balance"] or 0) if row else 0.0

def add_balance(user_id: str, amount_usd: float, tx_type: str = "topup",
                description: str = "", trans_ref: str = "") -> float:
    """Credit (positive) or debit (negative) the wallet atomically. Returns the new balance."""
    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return 0.0
        new_balance = float(row["balance"] or 0) + float(amount_usd)
        if tx_type == "topup" and amount_usd > 0:
            cursor.execute(
                "UPDATE users SET balance = ?, total_topped_up = total_topped_up + ? WHERE id = ?",
                (new_balance, amount_usd, user_id),
            )
        else:
            cursor.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
        cursor.execute("""
        INSERT INTO wallet_transactions (user_id, tx_type, amount_usd, balance_after, description, trans_ref)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, tx_type, amount_usd, new_balance, description, trans_ref))
        return new_balance

# ─────────────────────────────────────────────────────────────────────────────
# Wallet reservations (admission control for in-flight premium requests)
# ─────────────────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")

def _expire_reservations(cursor, now_str: str) -> None:
    """Retire holds whose request never settled (crash, disconnect, timeout)."""
    cursor.execute(
        "UPDATE wallet_reservations SET status = 'expired' "
        "WHERE status = 'active' AND expires_at <= ?",
        (now_str,),
    )

def _active_reserved(cursor, user_id: str) -> float:
    row = cursor.execute(
        "SELECT COALESCE(SUM(reserved_usd), 0) AS n FROM wallet_reservations "
        "WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchone()
    return float(row["n"] or 0)

def get_reserved_usd(user_id: str) -> float:
    """USD currently held by this user's in-flight requests."""
    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        _expire_reservations(cursor, _now_str())
        return _active_reserved(cursor, user_id)

def reserve_wallet(user_id: str, model: str, amount_usd: float,
                   ttl_seconds: Optional[int] = None) -> Optional[int]:
    """Hold ``amount_usd`` against the wallet for one in-flight request.

    Returns the reservation id, ``0`` when no hold is needed (the request is
    fully covered by the free trial), or ``None`` when the available balance
    cannot cover the hold.

    The availability check and the insert share a single ``BEGIN IMMEDIATE``
    transaction, so two concurrent requests can never both pass on the same
    dollars: ``available = balance - Σ(active holds)``.
    """
    amount_usd = max(float(amount_usd or 0.0), 0.0)
    if amount_usd <= 0:
        return 0
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.WALLET_RESERVATION_TTL_SECONDS)
    now = now_local()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires = (now + timedelta(seconds=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        _expire_reservations(cursor, now_str)
        row = cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        available = float(row["balance"] or 0) - _active_reserved(cursor, user_id)
        if amount_usd > available + 1e-9:
            return None
        cursor.execute(
            "INSERT INTO wallet_reservations (user_id, model, reserved_usd, status, expires_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (user_id, model, amount_usd, expires),
        )
        return int(cursor.lastrowid)

def release_reservation(reservation_id: Optional[int], status: str = "released") -> None:
    """Release a hold that will not be settled (upstream error, disconnect)."""
    if not reservation_id:
        return
    with db_session(immediate=True) as conn:
        conn.cursor().execute(
            "UPDATE wallet_reservations SET status = ? WHERE id = ? AND status = 'active'",
            (status, int(reservation_id)),
        )


def expire_stale_reservations() -> int:
    """Retire holds whose request never settled (crash, disconnect, timeout).

    Returns the number of holds expired. Runs on every reserve/read too, but a
    periodic caller frees a crashed request's dollars promptly even when no new
    premium traffic is arriving to trigger the lazy path.
    """
    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        _expire_reservations(cursor, _now_str())
        return int(cursor.rowcount or 0)


def record_usage(user_id: str, model: str, tokens_in: int, tokens_out: int,
                 latency_ms: float = 0.0, status_code: int = 200,
                 usage_source: str = "upstream",
                 reservation_id: Optional[int] = None,
                 tokens_cached: int = 0,
                 upstream_cost_usd: float = 0.0) -> float:
    """Log a completed request and settle its cost in one atomic transaction.

    Free models are never charged. Premium models consume the daily free trial
    first (input tokens, then output tokens); anything beyond the trial is billed
    from the wallet at the model's per-token rates.

    ``tokens_in`` is the total prompt size and ``tokens_cached`` is the
    cache-read subset, billed at the model's cheaper cached-input rate. Passing
    ``tokens_cached=0`` reproduces the old all-uncached behaviour.

    Two guarantees are enforced here:

    * The wallet can never go below zero. The charge is capped at the current
      balance (``charge = min(cost, balance)``); whatever cannot be collected is
      recorded as ``unbilled_usd`` instead of being pushed into a negative
      balance.
    * Usage the upstream did not report (``usage_source == 'estimated'``) is
      never billed from the wallet, because its cost cannot be proven. It still
      consumes the free trial.

    The trial counter, wallet balance, ledger entry, reservation settlement and
    request-log snapshot are all written inside a single ``BEGIN IMMEDIATE``
    transaction, so concurrent requests cannot double-grant the trial or lose a
    charge.

    Returns the USD amount actually charged (0.0 for free, trial-covered, or
    unbilled usage).
    """
    tokens_in = max(int(tokens_in or 0), 0)
    tokens_out = max(int(tokens_out or 0), 0)
    tokens_cached = min(max(int(tokens_cached or 0), 0), tokens_in)
    uncached_in = tokens_in - tokens_cached
    total_tokens = tokens_in + tokens_out

    is_premium = is_premium_model(model)
    limit = premium_trial_limit(model) if is_premium else 0
    today = get_today_str()

    trial_tokens = 0
    paid_tokens = 0
    paid_in = 0
    paid_cached = 0
    paid_out = 0
    cache_savings = 0.0
    charge = 0.0
    unbilled = 0.0
    balance_before = 0.0
    balance_after = 0.0

    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            balance_before = float(row["balance"] or 0)
            balance_after = balance_before

        if is_premium and row:
            used_row = cursor.execute(
                "SELECT tokens_used FROM model_trial_usage WHERE user_id = ? AND model = ? AND usage_date = ?",
                (user_id, model, today),
            ).fetchone()
            used = int(used_row["tokens_used"]) if used_row else 0

            # Apply this model's free trial: input tokens first (uncached before
            # cached, leaving the cheaper cached tokens as the billable part),
            # then output tokens. This mirrors premium_worst_case_cost, which
            # sizes the hold assuming no cache at all.
            remaining = max(limit - used, 0)
            free_uncached = min(uncached_in, remaining)
            remaining -= free_uncached
            free_cached = min(tokens_cached, remaining)
            remaining -= free_cached
            free_out = min(tokens_out, remaining)
            paid_cached = tokens_cached - free_cached
            paid_in = (uncached_in - free_uncached) + paid_cached
            paid_out = tokens_out - free_out
            trial_tokens = free_uncached + free_cached + free_out
            paid_tokens = paid_in + paid_out

            # Theoretical cost of the billed (post-trial) tokens.
            theoretical = premium_price(model, paid_in, paid_out, tokens_cached=paid_cached)
            # What the customer would have paid for those cached tokens at the
            # full input rate: the discount the cache pass-through gave them.
            cache_savings = round(
                (paid_cached / 1_000_000.0)
                * (premium_input_price(model) - premium_cached_input_price(model)),
                12,
            )
            if usage_source == "estimated":
                # Unverifiable usage is never charged; it shows up as unbilled.
                charge = 0.0
            else:
                # Cap at the balance so the wallet can never go negative.
                charge = min(theoretical, max(balance_before, 0.0))
            unbilled = round(theoretical - charge, 12)
            balance_after = balance_before - charge

            # Accumulate today's trial usage for this model only.
            cursor.execute("""
            INSERT INTO model_trial_usage (user_id, model, usage_date, tokens_used)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, model, usage_date)
            DO UPDATE SET tokens_used = tokens_used + excluded.tokens_used
            """, (user_id, model, today, total_tokens))

            cursor.execute(
                "UPDATE users SET balance = ?, total_spent = total_spent + ? WHERE id = ?",
                (balance_after, charge, user_id),
            )
            if charge > 0:
                cursor.execute("""
                INSERT INTO wallet_transactions
                (user_id, tx_type, amount_usd, balance_after, description, model, tokens_in, tokens_out)
                VALUES (?, 'usage', ?, ?, ?, ?, ?, ?)
                """, (user_id, -charge, balance_after, f"{total_tokens:,} tokens", model, paid_in, paid_out))

        # Settle this request's hold, if any, in the same transaction.
        if reservation_id:
            cursor.execute(
                "UPDATE wallet_reservations SET status = 'settled' WHERE id = ? AND status = 'active'",
                (int(reservation_id),),
            )

        # Full per-request audit snapshot (written in the same transaction).
        cursor.execute("""
        INSERT INTO request_logs
        (user_id, model, tokens_used, tokens_in, tokens_out, tokens_cached, trial_tokens, paid_tokens,
         cost_usd, cache_savings_usd, upstream_cost_usd, unbilled_usd, is_premium, balance_after,
         usage_source, latency_ms, status_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, model, total_tokens, tokens_in, tokens_out, tokens_cached, trial_tokens, paid_tokens,
              charge, cache_savings, upstream_cost_usd, unbilled, 1 if is_premium else 0, balance_after,
              usage_source, latency_ms, status_code))

    return charge


def log_rejected_request(user_id: str, model: str, status_code: int) -> None:
    """Record a request blocked before it reached upstream (e.g. HTTP 402).

    Gives admins an auditable trail of enforcement: you can see exactly when a
    user's trial ran out and their wallet was empty.
    """
    is_premium = is_premium_model(model)
    with db_session(immediate=True) as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        balance = float(row["balance"] or 0) if row else 0.0
        cursor.execute("""
        INSERT INTO request_logs
        (user_id, model, tokens_used, tokens_in, tokens_out, trial_tokens, paid_tokens,
         cost_usd, is_premium, balance_after, usage_source, latency_ms, status_code)
        VALUES (?, ?, 0, 0, 0, 0, 0, 0, ?, ?, 'rejected', 0, ?)
        """, (user_id, model, 1 if is_premium else 0, balance, status_code))

def get_wallet_transactions(user_id: str, limit: int = 25) -> List[Dict[str, Any]]:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT * FROM wallet_transactions WHERE user_id = ?
        ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in cursor.fetchall()]

def get_all_wallet_transactions(limit: int = 50) -> List[Dict[str, Any]]:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT w.*, u.username, u.avatar_url
        FROM wallet_transactions w
        LEFT JOIN users u ON w.user_id = u.id
        ORDER BY w.id DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in cursor.fetchall()]

# ─────────────────────────────────────────────────────────────────────────────
# User administration
# ─────────────────────────────────────────────────────────────────────────────

def regenerate_user_key(user_id: str) -> str:
    new_key = generate_api_key()
    with db_session() as conn:
        conn.cursor().execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user_id))
    return new_key

def delete_user(user_id: str):
    """Completely removes user, their request logs and wallet ledger."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        cursor.execute("DELETE FROM request_logs WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM wallet_transactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM model_trial_usage WHERE user_id = ?", (user_id,))

def toggle_user_ban(user_id: str) -> int:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_banned FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return 0
        new_status = 0 if row["is_banned"] else 1
        cursor.execute("UPDATE users SET is_banned = ? WHERE id = ?", (new_status, user_id))
        return new_status

def get_all_users() -> List[Dict[str, Any]]:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT u.*,
               COALESCE((
                   SELECT SUM(r.tokens_used) FROM request_logs r
                   WHERE r.user_id = u.id AND DATE(r.created_at) = DATE('now', 'localtime')
               ), 0) AS today_tokens
        FROM users u
        ORDER BY u.created_at DESC
        """)
        return [dict(r) for r in cursor.fetchall()]

def get_portal_stats() -> Dict[str, Any]:
    with db_session() as conn:
        cursor = conn.cursor()
        total_users = cursor.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

        # Wallet liability = total USD currently held by all users.
        row = cursor.execute("SELECT COALESCE(SUM(balance), 0) AS n FROM users").fetchone()
        total_balance = float(row["n"] or 0)

        row = cursor.execute("SELECT COALESCE(SUM(total_topped_up), 0) AS n FROM users").fetchone()
        total_topped_up = float(row["n"] or 0)

        row = cursor.execute("SELECT COALESCE(SUM(total_spent), 0) AS n FROM users").fetchone()
        total_spent = float(row["n"] or 0)

        total_tokens_today = cursor.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) AS n FROM request_logs WHERE DATE(created_at) = DATE('now', 'localtime')"
        ).fetchone()["n"] or 0

        requests_today = cursor.execute(
            "SELECT COUNT(*) AS n FROM request_logs WHERE DATE(created_at) = DATE('now', 'localtime')"
        ).fetchone()["n"]

        all_time_tokens = cursor.execute("SELECT COALESCE(SUM(tokens_used), 0) AS n FROM request_logs").fetchone()["n"]

        active_days = cursor.execute("SELECT COUNT(DISTINCT DATE(created_at)) AS n FROM request_logs").fetchone()["n"] or 0
        avg_per_day = int(all_time_tokens / active_days) if active_days > 0 else 0

        return {
            "total_users": total_users,
            "total_balance": total_balance,
            "total_topped_up": total_topped_up,
            "total_spent": total_spent,
            "total_tokens_today": total_tokens_today,
            "requests_today": requests_today,
            "all_time_tokens": all_time_tokens,
            "avg_per_day": avg_per_day,
        }

# ─────────────────────────────────────────────────────────────────────────────
# Usage analytics
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_usage(time_range: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate request_logs into time buckets, optionally scoped to a single user."""
    now = now_local()
    user_clause = "AND user_id = ?" if user_id else ""
    user_params = (user_id,) if user_id else ()

    with db_session() as conn:
        cursor = conn.cursor()

        if user_id:
            cursor.execute("SELECT COALESCE(SUM(tokens_used), 0) AS total FROM request_logs WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("SELECT COALESCE(SUM(tokens_used), 0) AS total FROM request_logs")
        all_time_tokens = cursor.fetchone()["total"]

        if time_range == "7d":
            start_time = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            bucket_fmt, label_fmt, steps, step = "%Y-%m-%d", "%a %d", 7, timedelta(days=1)
        elif time_range == "30d":
            start_time = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
            bucket_fmt, label_fmt, steps, step = "%Y-%m-%d", "%d/%m", 30, timedelta(days=1)
        else:  # "24h"
            start_time = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
            bucket_fmt, label_fmt, steps, step = "%Y-%m-%d %H:00", "%H:00", 24, timedelta(hours=1)

        cursor.execute(f"""
            SELECT strftime('{bucket_fmt}', created_at) AS bucket,
                   COUNT(*) AS req_count,
                   COALESCE(SUM(tokens_used), 0) AS token_sum
            FROM request_logs
            WHERE created_at >= ? {user_clause}
            GROUP BY bucket
        """, (start_time.strftime('%Y-%m-%d %H:%M:%S'),) + user_params)
        rows = {r["bucket"]: r for r in cursor.fetchall()}

        labels, token_data, req_data = [], [], []
        cur = start_time
        for _ in range(steps):
            b_key = cur.strftime(bucket_fmt)
            labels.append(cur.strftime(label_fmt))
            r = rows.get(b_key)
            token_data.append(r["token_sum"] if r else 0)
            req_data.append(r["req_count"] if r else 0)
            cur += step

        return {
            "all_time_tokens": all_time_tokens,
            "period_tokens": sum(token_data),
            "period_requests": sum(req_data),
            "labels": labels,
            "tokens": token_data,
            "requests": req_data,
        }

def get_user_analytics(user_id: str, time_range: str = "24h") -> Dict[str, Any]:
    return _aggregate_usage(time_range, user_id)

def get_portal_analytics(time_range: str = "24h") -> Dict[str, Any]:
    data = _aggregate_usage(time_range)
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT DATE(created_at)) AS active_days FROM request_logs")
        active_days = cursor.fetchone()["active_days"] or 0
    data["active_days"] = active_days
    data["avg_per_day"] = int(data["all_time_tokens"] / active_days) if active_days > 0 else 0
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Auditing: per-request log, trial history, wallet reconciliation
# ─────────────────────────────────────────────────────────────────────────────

def find_user_ids(term: str, limit: int = 500) -> List[str]:
    """Resolve a partial username or ID to matching user ids (for search filters).

    Keeps the Request Log usable with thousands of users: admins type a few
    characters instead of scrolling a huge dropdown.
    """
    term = (term or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    with db_session() as conn:
        rows = conn.cursor().execute(
            "SELECT id FROM users WHERE username LIKE ? OR id LIKE ? ORDER BY username COLLATE NOCASE LIMIT ?",
            (like, like, int(limit)),
        ).fetchall()
        return [r["id"] for r in rows]


def _request_log_filters(user_id=None, user_q=None, model=None, date_from=None,
                         date_to=None, status=None, premium_only=False):
    """Build the shared WHERE clause for the request-log views and CSV export."""
    clauses, params = [], []
    if user_id:
        clauses.append("r.user_id = ?")
        params.append(user_id)
    elif user_q:
        ids = find_user_ids(user_q)
        if not ids:
            clauses.append("1 = 0")
        else:
            clauses.append("r.user_id IN (" + ",".join(["?"] * len(ids)) + ")")
            params.extend(ids)
    if model:
        clauses.append("r.model = ?")
        params.append(model)
    if date_from:
        clauses.append("DATE(r.created_at) >= DATE(?)")
        params.append(date_from)
    if date_to:
        clauses.append("DATE(r.created_at) <= DATE(?)")
        params.append(date_to)
    if status:
        try:
            clauses.append("r.status_code = ?")
            params.append(int(status))
        except (TypeError, ValueError):
            pass
    if premium_only:
        clauses.append("r.is_premium = 1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count_request_logs(user_id=None, user_q=None, model=None, date_from=None, date_to=None,
                       status=None, premium_only=False) -> int:
    where, params = _request_log_filters(user_id, user_q, model, date_from, date_to, status, premium_only)
    with db_session() as conn:
        row = conn.cursor().execute(
            f"SELECT COUNT(*) AS n FROM request_logs r{where}", params
        ).fetchone()
        return int(row["n"] or 0)


def get_request_logs(user_id=None, user_q=None, model=None, date_from=None, date_to=None,
                     status=None, premium_only=False, limit: int = 100,
                     offset: int = 0) -> List[Dict[str, Any]]:
    """Per-request audit rows, newest first, with the username joined in."""
    where, params = _request_log_filters(user_id, user_q, model, date_from, date_to, status, premium_only)
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT r.*, u.username, u.avatar_url
        FROM request_logs r
        LEFT JOIN users u ON u.id = r.user_id
        {where}
        ORDER BY r.id DESC
        LIMIT ? OFFSET ?
        """, params + [int(limit), int(offset)])
        return [dict(x) for x in cursor.fetchall()]


def get_request_summary(user_id=None, user_q=None, model=None, date_from=None, date_to=None,
                        status=None, premium_only=False) -> Dict[str, Any]:
    """Totals for the current request-log filter (for the on-page summary row)."""
    where, params = _request_log_filters(user_id, user_q, model, date_from, date_to, status, premium_only)
    with db_session() as conn:
        row = conn.cursor().execute(f"""
        SELECT COUNT(*) AS requests,
               COALESCE(SUM(r.tokens_used), 0) AS tokens_total,
               COALESCE(SUM(r.tokens_in), 0) AS tokens_in,
               COALESCE(SUM(r.tokens_out), 0) AS tokens_out,
               COALESCE(SUM(r.trial_tokens), 0) AS trial_tokens,
               COALESCE(SUM(r.paid_tokens), 0) AS paid_tokens,
               COALESCE(SUM(r.cost_usd), 0) AS cost_usd,
               COALESCE(SUM(r.unbilled_usd), 0) AS unbilled_usd
        FROM request_logs r{where}
        """, params).fetchone()
        return dict(row)


def get_trial_history(user_ids=None, days: int = 14, limit: int = 200) -> List[Dict[str, Any]]:
    """Daily trial consumption per user/model, newest first, with limit and % used.

    ``user_ids`` may be a list (from a username search) or None for everyone.
    An explicit empty list means "no matching user", so nothing is returned.
    Capped at ``limit`` rows (largest consumers first) so the table never grows
    unbounded; the caller can show a hint to narrow the search when it fills.
    """
    if user_ids is not None and not user_ids:
        return []
    params = [f"-{int(days)} days"]
    clause = ""
    if user_ids:
        clause = "AND t.user_id IN (" + ",".join(["?"] * len(user_ids)) + ")"
        params.extend(user_ids)
    params.append(int(limit))
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT t.user_id, t.model, t.usage_date, t.tokens_used,
               u.username, u.avatar_url
        FROM model_trial_usage t
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.usage_date >= DATE('now', 'localtime', ?) {clause}
        ORDER BY t.usage_date DESC, t.tokens_used DESC
        LIMIT ?
        """, params)
        rows = [dict(r) for r in cursor.fetchall()]
    for r in rows:
        limit = premium_trial_limit(r["model"])
        r["limit"] = limit
        r["pct"] = min(int((r["tokens_used"] / limit) * 100), 100) if limit > 0 else 0
    return rows


def get_reconciliation() -> List[Dict[str, Any]]:
    """Compare each wallet's ledger-derived balance with the stored balance.

    ``expected = Σ(ledger credits) + Σ(ledger debits)``. Any non-zero
    ``balance_diff`` means a write bypassed the ledger and must be investigated.

    ``spent_diff`` compares ``users.total_spent`` with the sum of per-request
    ``cost_usd``; it is non-zero for premium usage that predates the audit
    migration (those old request rows carry the column default 0).

    Also reports ``reserved_usd`` (money held by in-flight requests) and
    ``unbilled_usd`` (premium cost that could not be collected and was recorded
    instead of driving the balance negative).
    """
    with db_session() as conn:
        cursor = conn.cursor()
        _expire_reservations(cursor, _now_str())
        users = [dict(r) for r in cursor.execute(
            "SELECT id, username, avatar_url, balance, total_topped_up, total_spent "
            "FROM users ORDER BY username COLLATE NOCASE"
        ).fetchall()]
        for u in users:
            agg = cursor.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN amount_usd > 0 THEN amount_usd ELSE 0 END), 0) AS credits,
                    COALESCE(SUM(CASE WHEN amount_usd < 0 THEN amount_usd ELSE 0 END), 0) AS debits
                FROM wallet_transactions WHERE user_id = ?
            """, (u["id"],)).fetchone()
            expected = float(agg["credits"] or 0) + float(agg["debits"] or 0)
            actual = float(u["balance"] or 0)
            u["expected_balance"] = expected
            u["actual_balance"] = actual
            u["balance_diff"] = round(actual - expected, 8)

            logged = cursor.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM request_logs WHERE user_id = ? AND is_premium = 1",
                (u["id"],),
            ).fetchone()["n"]
            u["logged_cost"] = float(logged or 0)
            u["spent_diff"] = round(float(u["total_spent"] or 0) - float(logged or 0), 8)

            # Money held by in-flight requests, and premium cost that could not be
            # collected (billed up to the balance, remainder recorded as unbilled).
            u["reserved_usd"] = _active_reserved(cursor, u["id"])
            unbilled = cursor.execute(
                "SELECT COALESCE(SUM(unbilled_usd), 0) AS n FROM request_logs WHERE user_id = ? AND is_premium = 1",
                (u["id"],),
            ).fetchone()["n"]
            u["unbilled_usd"] = float(unbilled or 0)
        return users
