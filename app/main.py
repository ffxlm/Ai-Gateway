import os
import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import init_db
from app.routers.gateway import gateway_router
from app.routers.auth import auth_router
from app.routers.pages import pages_router
from app.services.user_service import now_local, reset_all_stale_quota

async def daily_quota_reset_loop():
    """Reset every user's daily token usage at 00:00 in the configured timezone, then repeat daily."""
    while True:
        now = now_local()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep(max((next_midnight - now).total_seconds(), 1))
        try:
            count = reset_all_stale_quota()
            print(f"[✓] Daily quota reset at {now_local().isoformat(timespec='seconds')} ({count} user(s)).")
        except Exception as e:
            print(f"[!] Daily quota reset failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize SQLite WAL mode database
    init_db()
    # Catch up any rollover missed while the server was down, then schedule the next midnight.
    try:
        count = reset_all_stale_quota()
        if count:
            print(f"[✓] Startup quota rollover applied to {count} user(s).")
    except Exception as e:
        print(f"[!] Startup quota rollover failed: {e}")
    reset_task = asyncio.create_task(daily_quota_reset_loop())
    print("[✓] AI Gateway Portal database initialized successfully.")
    try:
        yield
    finally:
        reset_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reset_task
        # Shutdown clean up if needed
        print("[*] AI Gateway Portal shutting down gracefully.")

app = FastAPI(
    title="AI Gateway Portal",
    description="High-Concurrency OpenAI-Compatible LLM Gateway & Subscription Platform",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Configuration for external integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Sub-Routers
app.include_router(gateway_router)
app.include_router(auth_router)
app.include_router(pages_router)

@app.get("/healthz")
async def health_check():
    return {"status": "ok", "service": "ai-gateway-portal"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
