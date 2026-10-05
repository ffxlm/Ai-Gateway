import sqlite3
import os
import re
import contextlib
from datetime import datetime
from app.core.config import settings

def get_db_connection():
    conn = sqlite3.connect(settings.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

@contextlib.contextmanager
def db_session(immediate: bool = False):
    """Yield a connection inside a transaction.

    ``immediate=True`` takes SQLite's write lock up front (``BEGIN IMMEDIATE``).
    That serialises read-modify-write sequences such as wallet debits and trial
    counters, so two concurrent requests for the same user can neither lose a
    charge (lost update) nor double-spend the free daily trial.
    """
    conn = get_db_connection()
    if immediate:
        conn.isolation_level = None  # take manual control of the transaction
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if immediate:
            conn.execute("COMMIT")
        else:
            conn.commit()
    except Exception:
        if immediate:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        else:
            conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_session() as conn:
        cursor = conn.cursor()
        # High concurrency pragmas (WAL mode)
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        
        # 1. Users table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            discriminator TEXT DEFAULT '0000',
            avatar_url TEXT DEFAULT '',
            api_key TEXT UNIQUE NOT NULL,
            role TEXT DEFAULT 'user',
            tier TEXT DEFAULT 'free',
            vip_expires_at TEXT DEFAULT NULL,
            daily_token_usage INTEGER DEFAULT 0,
            last_usage_date TEXT DEFAULT '',
            is_banned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        """)
        
        # 2. System Settings table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        
        # 3. Request Metrics & Logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            model TEXT,
            tokens_used INTEGER,
            latency_ms REAL,
            status_code INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        """)

        # 4. Payment Transactions (PromptPay / SlipOK top-up records, amounts in THB)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            pass_type TEXT NOT NULL,
            amount REAL NOT NULL,
            trans_ref TEXT UNIQUE NOT NULL,
            payment_method TEXT DEFAULT 'slipok',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        """)

        # 5. Per-model daily free-trial usage (one row per user/model/day)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_trial_usage (
            user_id TEXT NOT NULL,
            model TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, model, usage_date)
        );
        """)

        # 6. Wallet Ledger (amounts in USD; signed: + credit, - debit)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            tx_type TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            balance_after REAL NOT NULL,
            description TEXT DEFAULT '',
            model TEXT DEFAULT '',
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            trans_ref TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        """)
        
        # Seed default settings if not exists
        default_settings = {
            "master_router_url": settings.MASTER_ROUTER_URL,
            "master_router_key": settings.MASTER_ROUTER_KEY,
            "discord_invite_url": settings.DISCORD_INVITE_URL,
            "promptpay_id": settings.PROMPTPAY_ID,
            "promptpay_name": settings.PROMPTPAY_NAME,
            "slipok_branch_id": settings.SLIPOK_BRANCH_ID,
            "slipok_api_key": settings.SLIPOK_API_KEY,
            "usd_to_thb": str(settings.USD_TO_THB),
            "min_topup_thb": str(settings.MIN_TOPUP_THB),
            "topup_packages_thb": settings.TOPUP_PACKAGES_THB,
            "premium_trial_tokens_per_day": str(settings.PREMIUM_TRIAL_TOKENS_PER_DAY),
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?);", (k, v))

        _migrate_wallet(cursor)
        _migrate_observability(cursor)


def _migrate_observability(cursor):
    """Add per-request audit columns so every request is fully inspectable.

    Idempotent: only adds columns that do not already exist, so it is safe to run
    on every start and on older databases (existing rows get the column defaults).
    """
    existing = {row["name"] for row in cursor.execute("PRAGMA table_info(request_logs)").fetchall()}
    for column, ddl in (
        ("tokens_in", "INTEGER DEFAULT 0"),
        ("tokens_out", "INTEGER DEFAULT 0"),
        ("trial_tokens", "INTEGER DEFAULT 0"),
        ("paid_tokens", "INTEGER DEFAULT 0"),
        ("cost_usd", "REAL DEFAULT 0"),
        ("is_premium", "INTEGER DEFAULT 0"),
        ("balance_after", "REAL DEFAULT 0"),
        ("usage_source", "TEXT DEFAULT ''"),
    ):
        if column not in existing:
            cursor.execute(f"ALTER TABLE request_logs ADD COLUMN {column} {ddl}")


def _migrate_wallet(cursor):
    """Add wallet columns and convert past VIP payments into wallet credit (once)."""
    # 1. Add wallet columns to users if this is an older database.
    existing = {row["name"] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for column, ddl in (
        ("balance", "REAL DEFAULT 0"),
        ("total_topped_up", "REAL DEFAULT 0"),
        ("total_spent", "REAL DEFAULT 0"),
    ):
        if column not in existing:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")

    # 2. Shorten legacy ledger descriptions that overflow the mobile wallet list.
    #    Idempotent: only matches the old long wording, so it is a no-op afterwards.
    for row in cursor.execute(
        "SELECT id, description FROM wallet_transactions "
        "WHERE description LIKE 'Converted past VIP payments%' OR description LIKE 'Top-up % @ %'"
    ).fetchall():
        desc = row["description"] or ""
        match = re.search(r"฿([\d,]+(?:\.\d+)?)", desc)
        amount = match.group(1) if match else ""
        if desc.startswith("Converted"):
            short = f"VIP credit (฿{amount})" if amount else "VIP credit"
        else:
            short = f"Top-up ฿{amount}" if amount else "Top-up"
        cursor.execute("UPDATE wallet_transactions SET description = ? WHERE id = ?", (short, row["id"]))

    # 3. Rename the legacy premium model id (cb/… → …) in historical rows so
    #    trial counters and ledger entries stay attached to the model. Idempotent.
    cursor.execute("UPDATE request_logs SET model = 'deepseek-v4.1-flash' WHERE model = 'cb/deepseek-v4.1-flash'")
    cursor.execute("UPDATE wallet_transactions SET model = 'deepseek-v4.1-flash' WHERE model = 'cb/deepseek-v4.1-flash'")
    cursor.execute("UPDATE model_trial_usage SET model = 'deepseek-v4.1-flash' WHERE model = 'cb/deepseek-v4.1-flash'")

    # 4. One-time conversion of previous VIP payments into wallet balance.
    already = cursor.execute("SELECT value FROM system_settings WHERE key = 'wallet_migrated_v1'").fetchone()
    if already:
        return

    rate_row = cursor.execute("SELECT value FROM system_settings WHERE key = 'usd_to_thb'").fetchone()
    try:
        rate = float(rate_row["value"]) if rate_row else 35.0
    except (TypeError, ValueError):
        rate = 35.0
    if rate <= 0:
        rate = 35.0

    for user in cursor.execute("SELECT id FROM users").fetchall():
        total_row = cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payment_transactions WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        thb = float(total_row["total"] or 0)
        if thb <= 0:
            continue
        usd = round(thb / rate, 4)
        cursor.execute(
            "UPDATE users SET balance = balance + ?, total_topped_up = total_topped_up + ? WHERE id = ?",
            (usd, usd, user["id"]),
        )
        balance_after = cursor.execute("SELECT balance FROM users WHERE id = ?", (user["id"],)).fetchone()["balance"]
        cursor.execute(
            """INSERT INTO wallet_transactions
               (user_id, tx_type, amount_usd, balance_after, description, trans_ref)
               VALUES (?, 'migration', ?, ?, ?, ?)""",
            (user["id"], usd, balance_after,
             f"VIP credit (฿{thb:,.2f})", "vip-migration"),
        )

    cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('wallet_migrated_v1', '1')")

def get_setting(key: str, default: str = "") -> str:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def update_setting(key: str, value: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, value))
