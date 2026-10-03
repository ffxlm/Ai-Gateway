import sqlite3
import os
import contextlib
from datetime import datetime
from app.core.config import settings

def get_db_connection():
    conn = sqlite3.connect(settings.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

@contextlib.contextmanager
def db_session():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
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
        
        # Seed default settings if not exists
        default_settings = {
            "daily_free_tokens": str(settings.DAILY_FREE_TOKENS),
            "master_router_url": settings.MASTER_ROUTER_URL,
            "master_router_key": settings.MASTER_ROUTER_KEY,
            "discord_invite_url": settings.DISCORD_INVITE_URL,
            "vip_daily_price": "10",
            "vip_weekly_price": "50"
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?);", (k, v))

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
