import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from app.core.database import db_session, get_setting
from app.core.config import settings

def generate_api_key() -> str:
    return f"sk-portal-{secrets.token_urlsafe(32)}"

def now_local() -> datetime:
    """Naive wall-clock time in the configured business timezone (default Asia/Bangkok).

    Kept naive on purpose so it stays comparable with timestamps stored as naive
    local strings (e.g. vip_expires_at) and SQLite's datetime('now','localtime').
    """
    try:
        return datetime.now(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)
    except Exception:
        return datetime.now()

def get_today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

def get_or_create_user(discord_id: str, username: str, avatar_url: str = "", role: str = "user") -> Dict[str, Any]:
    today = get_today_str()
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
            # Daily quota rollover check
            if user["last_usage_date"] != today:
                cursor.execute("UPDATE users SET daily_token_usage = 0, last_usage_date = ? WHERE id = ?", (today, discord_id))
                user["daily_token_usage"] = 0
                user["last_usage_date"] = today
            # Update avatar/username if changed
            cursor.execute("UPDATE users SET username = ?, avatar_url = ? WHERE id = ?", (username, avatar_url, discord_id))
            return user
        else:
            api_key = generate_api_key()
            cursor.execute("SELECT COUNT(*) AS total FROM users")
            user_count = cursor.fetchone()["total"]
            assigned_role = "admin" if (user_count == 0 or is_superadmin) else role

            cursor.execute("""
            INSERT INTO users (id, username, avatar_url, api_key, role, tier, daily_token_usage, last_usage_date)
            VALUES (?, ?, ?, ?, ?, 'free', 0, ?)
            """, (discord_id, username, avatar_url, api_key, assigned_role, today))
            cursor.execute("SELECT * FROM users WHERE id = ?", (discord_id,))
            return dict(cursor.fetchone())

def get_user_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    today = get_today_str()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE api_key = ?", (api_key,))
        row = cursor.fetchone()
        if not row:
            return None
        user = dict(row)
        # Check quota rollover
        if user["last_usage_date"] != today:
            cursor.execute("UPDATE users SET daily_token_usage = 0, last_usage_date = ? WHERE id = ?", (today, user["id"]))
            user["daily_token_usage"] = 0
            user["last_usage_date"] = today
        return user

def apply_daily_rollover(user: Dict[str, Any]) -> Dict[str, Any]:
    """Reset a single user's daily usage if the business date has changed.

    Used by read paths (e.g. the dashboard session lookup) that do not go through
    the API-key / login flow, so the UI reflects the reset immediately after midnight.
    """
    today = get_today_str()
    if user.get("last_usage_date") != today:
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET daily_token_usage = 0, last_usage_date = ? WHERE id = ?", (today, user["id"]))
        user["daily_token_usage"] = 0
        user["last_usage_date"] = today
    return user

def reset_all_stale_quota() -> int:
    """Reset daily usage for every user whose last_usage_date is not the current business date.

    Called on startup and by the midnight scheduler in app.main.
    """
    today = get_today_str()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET daily_token_usage = 0, last_usage_date = ? WHERE last_usage_date != ?", (today, today))
        return cursor.rowcount

def is_vip_active(user: Dict[str, Any]) -> bool:
    if user.get("tier") == "vip" and user.get("vip_expires_at"):
        try:
            exp = datetime.fromisoformat(user["vip_expires_at"])
            return exp > now_local()
        except Exception:
            return False
    return False

def check_user_quota(user: Dict[str, Any]) -> tuple[bool, str]:
    if user.get("is_banned"):
        return False, "Your account has been suspended by the administrator."
    
    if is_vip_active(user):
        return True, "VIP Active (Unlimited)"
    
    daily_limit = int(get_setting("daily_free_tokens", "5000000"))
    used = user.get("daily_token_usage", 0)
    
    if used >= daily_limit:
        return False, f"Daily free token quota exceeded ({used:,} / {daily_limit:,} tokens). Upgrading to VIP or resets at 00:00."
    
    return True, f"Remaining: {daily_limit - used:,} tokens"

def atomic_record_usage(user_id: str, tokens: int, model: str = "", latency_ms: float = 0.0, status_code: int = 200):
    today = get_today_str()
    with db_session() as conn:
        cursor = conn.cursor()
        # Atomic token increment
        cursor.execute("""
        UPDATE users 
        SET daily_token_usage = daily_token_usage + ?, last_usage_date = ?
        WHERE id = ?
        """, (tokens, today, user_id))
        
        # Log request
        cursor.execute("""
        INSERT INTO request_logs (user_id, model, tokens_used, latency_ms, status_code)
        VALUES (?, ?, ?, ?, ?)
        """, (user_id, model, tokens, latency_ms, status_code))

def regenerate_user_key(user_id: str) -> str:
    new_key = generate_api_key()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user_id))
    return new_key

def add_vip_days(user_id: str, days: int) -> str:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tier, vip_expires_at FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return ""
        
        current_exp = None
        if row["vip_expires_at"]:
            try:
                parsed = datetime.fromisoformat(row["vip_expires_at"])
                if parsed > now_local():
                    current_exp = parsed
            except Exception:
                pass
        
        start_time = current_exp if current_exp else now_local()
        new_exp = start_time + timedelta(days=days)
        new_exp_str = new_exp.isoformat()
        
        cursor.execute("""
        UPDATE users 
        SET tier = 'vip', vip_expires_at = ?
        WHERE id = ?
        """, (new_exp_str, user_id))
        return new_exp_str

def revoke_vip(user_id: str):
    """Cancels VIP status immediately and resets user back to Free tier."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE users 
        SET tier = 'free', vip_expires_at = NULL 
        WHERE id = ?
        """, (user_id,))

def delete_user(user_id: str):
    """Completely removes user and their request logs from the database."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        cursor.execute("DELETE FROM request_logs WHERE user_id = ?", (user_id,))

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

def reset_user_quota(user_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET daily_token_usage = 0 WHERE id = ?", (user_id,))

def get_all_users() -> List[Dict[str, Any]]:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
        return [dict(r) for r in cursor.fetchall()]

def get_portal_stats() -> Dict[str, Any]:
    today = get_today_str()
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total_users FROM users")
        total_users = cursor.fetchone()["total_users"]
        
        cursor.execute("SELECT COUNT(*) AS total_vip FROM users WHERE tier = 'vip' AND vip_expires_at > ?", (now_local().isoformat(),))
        total_vip = cursor.fetchone()["total_vip"]
        
        cursor.execute("SELECT SUM(daily_token_usage) AS total_tokens_today FROM users WHERE last_usage_date = ?", (today,))
        total_tokens_today = cursor.fetchone()["total_tokens_today"] or 0
        
        cursor.execute("SELECT COUNT(*) AS requests_today FROM request_logs WHERE DATE(created_at) = DATE('now')")
        requests_today = cursor.fetchone()["requests_today"]
        
        return {
            "total_users": total_users,
            "total_vip": total_vip,
            "total_tokens_today": total_tokens_today,
            "requests_today": requests_today
        }

def get_user_analytics(user_id: str, time_range: str = "24h") -> Dict[str, Any]:
    now = now_local()
    with db_session() as conn:
        cursor = conn.cursor()
        
        # All-time total tokens for this user
        cursor.execute("SELECT COALESCE(SUM(tokens_used), 0) AS total FROM request_logs WHERE user_id = ?", (user_id,))
        all_time_tokens = cursor.fetchone()["total"]

        if time_range == "7d":
            # Last 7 days
            start_time = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            cursor.execute("""
                SELECT strftime('%Y-%m-%d', created_at) AS bucket,
                       COUNT(*) AS req_count,
                       COALESCE(SUM(tokens_used), 0) AS token_sum
                FROM request_logs
                WHERE user_id = ? AND created_at >= ?
                GROUP BY bucket
            """, (user_id, start_time.strftime('%Y-%m-%d %H:%M:%S')))
            rows = {r["bucket"]: r for r in cursor.fetchall()}

            labels = []
            token_data = []
            req_data = []
            cur = start_time
            for _ in range(7):
                b_key = cur.strftime("%Y-%m-%d")
                label = cur.strftime("%a %d")
                labels.append(label)
                r = rows.get(b_key)
                token_data.append(r["token_sum"] if r else 0)
                req_data.append(r["req_count"] if r else 0)
                cur += timedelta(days=1)

        elif time_range == "30d":
            # Last 30 days
            start_time = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
            cursor.execute("""
                SELECT strftime('%Y-%m-%d', created_at) AS bucket,
                       COUNT(*) AS req_count,
                       COALESCE(SUM(tokens_used), 0) AS token_sum
                FROM request_logs
                WHERE user_id = ? AND created_at >= ?
                GROUP BY bucket
            """, (user_id, start_time.strftime('%Y-%m-%d %H:%M:%S')))
            rows = {r["bucket"]: r for r in cursor.fetchall()}

            labels = []
            token_data = []
            req_data = []
            cur = start_time
            for _ in range(30):
                b_key = cur.strftime("%Y-%m-%d")
                label = cur.strftime("%d/%m")
                labels.append(label)
                r = rows.get(b_key)
                token_data.append(r["token_sum"] if r else 0)
                req_data.append(r["req_count"] if r else 0)
                cur += timedelta(days=1)

        else:  # "24h"
            # Last 24 hours (hourly)
            start_time = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
            cursor.execute("""
                SELECT strftime('%Y-%m-%d %H:00', created_at) AS bucket,
                       COUNT(*) AS req_count,
                       COALESCE(SUM(tokens_used), 0) AS token_sum
                FROM request_logs
                WHERE user_id = ? AND created_at >= ?
                GROUP BY bucket
            """, (user_id, start_time.strftime('%Y-%m-%d %H:%M:%S')))
            rows = {r["bucket"]: r for r in cursor.fetchall()}

            labels = []
            token_data = []
            req_data = []
            cur = start_time
            for _ in range(24):
                b_key = cur.strftime("%Y-%m-%d %H:00")
                label = cur.strftime("%H:00")
                labels.append(label)
                r = rows.get(b_key)
                token_data.append(r["token_sum"] if r else 0)
                req_data.append(r["req_count"] if r else 0)
                cur += timedelta(hours=1)

        return {
            "all_time_tokens": all_time_tokens,
            "period_tokens": sum(token_data),
            "period_requests": sum(req_data),
            "labels": labels,
            "tokens": token_data,
            "requests": req_data
        }

