import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from app.core.database import db_session, get_setting
from app.core.config import settings
from app.core.catalog import is_premium_model, premium_price, premium_trial_tokens

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

    A per-model value from the catalog wins; otherwise the global
    ``premium_trial_tokens_per_day`` setting (editable in the Admin panel) is used.
    """
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
    with db_session() as conn:
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

def record_usage(user_id: str, model: str, tokens_in: int, tokens_out: int,
                 latency_ms: float = 0.0, status_code: int = 200) -> float:
    """Log a completed request.

    Free models are never charged. Premium models consume the daily free trial
    first (input tokens, then output tokens); anything beyond the trial is billed
    from the wallet at the model's per-token rates.

    Returns the USD amount charged (0.0 for free models or trial-covered usage).
    """
    tokens_in = max(int(tokens_in or 0), 0)
    tokens_out = max(int(tokens_out or 0), 0)
    total_tokens = tokens_in + tokens_out
    cost = 0.0

    is_premium = is_premium_model(model)
    limit = premium_trial_limit(model) if is_premium else 0
    today = get_today_str()

    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO request_logs (user_id, model, tokens_used, latency_ms, status_code)
        VALUES (?, ?, ?, ?, ?)
        """, (user_id, model, total_tokens, latency_ms, status_code))

        if is_premium:
            row = cursor.execute(
                "SELECT balance FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            used_row = cursor.execute(
                "SELECT tokens_used FROM model_trial_usage WHERE user_id = ? AND model = ? AND usage_date = ?",
                (user_id, model, today),
            ).fetchone()
            if row:
                used = int(used_row["tokens_used"]) if used_row else 0

                # Apply this model's free trial: input tokens first, then output tokens.
                remaining = max(limit - used, 0)
                free_in = min(tokens_in, remaining)
                remaining -= free_in
                free_out = min(tokens_out, remaining)
                paid_in = tokens_in - free_in
                paid_out = tokens_out - free_out

                cost = premium_price(model, paid_in, paid_out)
                new_balance = float(row["balance"] or 0)
                if cost > 0:
                    new_balance -= cost

                # Accumulate today's trial usage for this model only.
                cursor.execute("""
                INSERT INTO model_trial_usage (user_id, model, usage_date, tokens_used)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, model, usage_date)
                DO UPDATE SET tokens_used = tokens_used + excluded.tokens_used
                """, (user_id, model, today, total_tokens))

                cursor.execute(
                    "UPDATE users SET balance = ?, total_spent = total_spent + ? WHERE id = ?",
                    (new_balance, cost, user_id),
                )
                if cost > 0:
                    cursor.execute("""
                    INSERT INTO wallet_transactions
                    (user_id, tx_type, amount_usd, balance_after, description, model, tokens_in, tokens_out)
                    VALUES (?, 'usage', ?, ?, ?, ?, ?, ?)
                    """, (user_id, -cost, new_balance, f"{total_tokens:,} tokens", model, paid_in, paid_out))
    return cost

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
