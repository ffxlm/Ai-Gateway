import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

class Settings:
    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8080"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-key-change-in-production-123456789")
    
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
    DAILY_FREE_TOKENS: int = int(os.getenv("DAILY_FREE_TOKENS", "5000000"))
    FREE_CONCURRENCY_LIMIT: int = int(os.getenv("FREE_CONCURRENCY_LIMIT", "5"))
    VIP_CONCURRENCY_LIMIT: int = int(os.getenv("VIP_CONCURRENCY_LIMIT", "20"))
    
    # Admin & Support
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "admin-pass-2026")
    ADMIN_DISCORD_IDS: str = os.getenv("ADMIN_DISCORD_IDS", "1519726876984086528")
    DISCORD_INVITE_URL: str = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/")

settings = Settings()
