import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

class Settings:
    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8080"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-key-change-in-production-123456789")
    # Business timezone used for daily quota rollover (00:00 local time)
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Bangkok")
    
    # Database
    DATABASE_PATH: str = os.path.join(BASE_DIR, "portal.db")
    
    # 9Router Upstream Bridge
    MASTER_ROUTER_URL: str = os.getenv("MASTER_ROUTER_URL", "https://api.thirx.com").rstrip("/")
    MASTER_ROUTER_KEY: str = os.getenv("MASTER_ROUTER_KEY", "sk-f73d7eb77e144c2b-k4aj7o-97d09de1")
    
    # Discord OAuth2
    DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "")
    DISCORD_CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    DISCORD_REDIRECT_URI: str = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8080/auth/discord/callback")
    
    # Business Rules
    # Free models are unlimited; the wallet only pays for premium (xHigh) usage.
    CONCURRENCY_LIMIT: int = int(os.getenv("CONCURRENCY_LIMIT", "10"))
    # Premium models get this many free tokens per day before wallet billing kicks in.
    PREMIUM_TRIAL_TOKENS_PER_DAY: int = int(os.getenv("PREMIUM_TRIAL_TOKENS_PER_DAY", "1000000"))
    USD_TO_THB: float = float(os.getenv("USD_TO_THB", "35"))
    MIN_TOPUP_THB: int = int(os.getenv("MIN_TOPUP_THB", "10"))
    # Comma-separated THB quick-pick amounts shown in the top-up modal.
    TOPUP_PACKAGES_THB: str = os.getenv("TOPUP_PACKAGES_THB", "10,35,70,175,350,700")

    # Admin & Support
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "admin-pass-2026")
    ADMIN_DISCORD_IDS: str = os.getenv("ADMIN_DISCORD_IDS", "1519726876984086528")
    DISCORD_INVITE_URL: str = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/N7Kuayuzxb")

    # Payment & SlipOK
    PROMPTPAY_ID: str = os.getenv("PROMPTPAY_ID", "")
    PROMPTPAY_NAME: str = os.getenv("PROMPTPAY_NAME", "ธีรภัทร สุขเพีย")
    SLIPOK_BRANCH_ID: str = os.getenv("SLIPOK_BRANCH_ID", "77682")
    SLIPOK_API_KEY: str = os.getenv("SLIPOK_API_KEY", "SLIPOK4JOF2LJ")

settings = Settings()
