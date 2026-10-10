import os
import asyncio
import contextlib
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.core.database import init_db
from app.services.user_service import expire_stale_reservations
from app.routers.gateway import gateway_router
from app.routers.auth import auth_router
from app.routers.pages import pages_router

# How often the reservation janitor retires holds from crashed requests.
RESERVATION_SWEEP_SECONDS = 60

# Static asset cache policy. Files requested with a version query (e.g.
# /static/portal.css?v=2) are content-addressed by that query, so they can be
# cached forever; unversioned assets (favicon, icons) get a short TTL so that
# updates still propagate to browsers within a day.
IMMUTABLE_MAX_AGE = 31536000  # 1 year
DEFAULT_MAX_AGE = 86400       # 1 day


async def _reservation_janitor() -> None:
    """Periodically expire wallet holds whose request never settled.

    Reservations are also expired lazily on every premium request; this sweep
    just guarantees the dollars come back promptly during quiet periods.
    """
    while True:
        await asyncio.sleep(RESERVATION_SWEEP_SECONDS)
        try:
            expire_stale_reservations()
        except Exception as exc:  # never let the janitor kill the app
            print(f"[janitor] reservation expiry failed: {exc}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize SQLite WAL mode database (creates tables + runs migrations)
    init_db()
    print("[✓] AI Gateway Portal database initialized successfully.")
    janitor = asyncio.create_task(_reservation_janitor())
    try:
        yield
    finally:
        # Shutdown clean up if needed
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        print("[*] AI Gateway Portal shutting down gracefully.")

app = FastAPI(
    title="AI Gateway Portal",
    description="High-Concurrency OpenAI-Compatible LLM Gateway & Subscription Platform",
    version="1.1.0",
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
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.middleware("http")
async def static_cache_headers(request, call_next):
    """Attach Cache-Control to static assets so browsers reuse them."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        if "v" in request.query_params:
            response.headers["Cache-Control"] = f"public, max-age={IMMUTABLE_MAX_AGE}, immutable"
        else:
            response.headers["Cache-Control"] = f"public, max-age={DEFAULT_MAX_AGE}"
    return response
app.include_router(gateway_router)
app.include_router(auth_router)
app.include_router(pages_router)

@app.get("/healthz")
async def health_check():
    return {"status": "ok", "service": "ai-gateway-portal"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
