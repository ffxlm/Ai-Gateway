import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import init_db
from app.routers.gateway import gateway_router
from app.routers.auth import auth_router
from app.routers.pages import pages_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize SQLite WAL mode database
    init_db()
    print("[✓] AI Gateway Portal database initialized successfully.")
    yield
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
