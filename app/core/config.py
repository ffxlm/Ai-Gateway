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
    
    # 9Router Upstream Bridge (free models)
    MASTER_ROUTER_URL: str = os.getenv("MASTER_ROUTER_URL", "https://api.thirx.com").rstrip("/")
    MASTER_ROUTER_KEY: str = os.getenv("MASTER_ROUTER_KEY", "sk-f73d7eb77e144c2b-k4aj7o-97d09de1")

    # Premium (xHigh) upstream provider, routed separately from the free bridge.
    # May include or omit a trailing "/v1"; it is normalised when building URLs.
    PREMIUM_UPSTREAM_URL: str = os.getenv("PREMIUM_UPSTREAM_URL", "https://api.inferhub.dev/v1")
    PREMIUM_UPSTREAM_KEY: str = os.getenv("PREMIUM_UPSTREAM_KEY", "")
    
    # Discord OAuth2
    DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "")
    DISCORD_CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    DISCORD_REDIRECT_URI: str = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8080/auth/discord/callback")
    
    # Business Rules
    # Free models are unlimited; the wallet only pays for premium (xHigh) usage.
    #
    # Concurrency is split into two independent pools so each can be tuned to the
    # real capacity of its own upstream (9Router absorbs far more parallel calls
    # than the premium provider's advertised rate limit). A per-user cap stops a
    # single account from monopolising a pool. All three are editable live from
    # the Admin panel; these env values are only the initial defaults.
    FREE_CONCURRENCY_LIMIT: int = int(os.getenv("FREE_CONCURRENCY_LIMIT", "40"))
    PREMIUM_CONCURRENCY_LIMIT: int = int(os.getenv("PREMIUM_CONCURRENCY_LIMIT", "30"))
    PER_USER_CONCURRENCY_LIMIT: int = int(os.getenv("PER_USER_CONCURRENCY_LIMIT", "5"))
    # Premium models get this many free tokens per day before wallet billing kicks in.
    PREMIUM_TRIAL_TOKENS_PER_DAY: int = int(os.getenv("PREMIUM_TRIAL_TOKENS_PER_DAY", "1000000"))
    # Admission control: the most a single premium request may generate is capped
    # here (per-model override possible in the catalog). Without a cap the
    # pre-flight budget check could not bound a request's worst-case cost.
    PREMIUM_MAX_OUTPUT_TOKENS: int = int(os.getenv("PREMIUM_MAX_OUTPUT_TOKENS", "32768"))
    # How long a wallet reservation is held while its request is in flight.
    # Must comfortably exceed the 180s upstream timeout, or an in-flight request
    # could outlive its own hold and let a concurrent one double-spend the same
    # dollars; a crashed request frees its hold automatically once this elapses.
    WALLET_RESERVATION_TTL_SECONDS: int = int(os.getenv("WALLET_RESERVATION_TTL_SECONDS", "300"))
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

# Synchronize process and C runtime timezone for SQLite datetime('now', 'localtime')
import time
if hasattr(time, "tzset"):
    os.environ["TZ"] = settings.TIMEZONE
    time.tzset()
