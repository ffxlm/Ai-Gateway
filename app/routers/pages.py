from fastapi import APIRouter, Request, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import os
from app.core.config import settings
from app.core.database import db_session, get_setting, update_setting
from app.services.user_service import (
    get_or_create_user, regenerate_user_key, add_vip_days, revoke_vip, delete_user,
    toggle_user_ban, reset_user_quota, get_all_users, get_portal_stats, is_vip_active,
    get_user_analytics
)
from app.services.proxy_service import fetch_upstream_models

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

pages_router = APIRouter(tags=["Pages"])

def get_session_user(request: Request):
    user_id = request.cookies.get("portal_session")
    if not user_id:
        return None
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

@pages_router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
        
    is_real_admin = (user.get("role") == "admin")

    # Handle view switcher for admin
    view_query = request.query_params.get("view")
    if is_real_admin and view_query:
        if view_query == "user":
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie("portal_view_override", "user", max_age=86400, httponly=True, samesite="lax")
            return resp
        elif view_query == "admin":
            resp = RedirectResponse(url="/", status_code=303)
            resp.delete_cookie("portal_view_override")
            return resp

    view_override = request.cookies.get("portal_view_override") if is_real_admin else None
    preview_as_user = (view_override == "user")
    effective_role = "user" if preview_as_user else user.get("role")

    daily_limit = int(get_setting("daily_free_tokens", str(settings.DAILY_FREE_TOKENS)))
    is_vip = is_vip_active(user)
    discord_invite = get_setting("discord_invite_url", settings.DISCORD_INVITE_URL)
    vip_daily_price = get_setting("vip_daily_price", "10")
    vip_weekly_price = get_setting("vip_weekly_price", "50")
    
    # Calculate percentage for quota bar
    used = user.get("daily_token_usage", 0)
    pct = min(int((used / daily_limit) * 100), 100) if daily_limit > 0 else 0
    
    base_url = str(request.base_url).rstrip("/")
    api_endpoint = f"{base_url}/v1"
    
    # Exactly the 7 models requested
    TARGET_MODELS = [
        "deepseek-v4-flash",
        "GLM-5.3-Flash",
        "grok-4.7-xhigh",
        "grok-4.7",
        "qwen3.8-27b",
        "MiniMax-M2.7",
        "muse-spark-1.3"
    ]
    models_list = TARGET_MODELS
    
    # User Usage Analytics (initial 24h)
    analytics = get_user_analytics(user["id"], "24h")
    
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "is_vip": is_vip,
        "is_admin": is_real_admin,
        "preview_as_user": preview_as_user,
        "effective_role": effective_role,
        "daily_limit": daily_limit,
        "quota_pct": pct,
        "api_endpoint": api_endpoint,
        "discord_invite": discord_invite,
        "vip_daily_price": vip_daily_price,
        "vip_weekly_price": vip_weekly_price,
        "models": models_list,
        "analytics": analytics
    })

@pages_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_session_user(request)
    if user:
        return RedirectResponse(url="/")
        
    error = request.query_params.get("error")
    error_msg = None
    if error == "discord_denied":
        error_msg = "Discord authorization was cancelled."
    elif error:
        error_msg = "Authentication failed. Please try again."

    daily_tokens = int(get_setting("daily_free_tokens", str(settings.DAILY_FREE_TOKENS)))
    return templates.TemplateResponse(request, "login.html", {
        "daily_tokens": f"{daily_tokens:,}",
        "error": error_msg
    })

@pages_router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/")
        
    stats = get_portal_stats()
    all_users = get_all_users()
    
    current_settings = {
        "daily_free_tokens": get_setting("daily_free_tokens", str(settings.DAILY_FREE_TOKENS)),
        "master_router_url": get_setting("master_router_url", settings.MASTER_ROUTER_URL),
        "master_router_key": get_setting("master_router_key", settings.MASTER_ROUTER_KEY),
        "discord_invite_url": get_setting("discord_invite_url", settings.DISCORD_INVITE_URL),
        "vip_daily_price": get_setting("vip_daily_price", "10"),
        "vip_weekly_price": get_setting("vip_weekly_price", "50")
    }
    
    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "stats": stats,
        "users": all_users,
        "settings": current_settings
    })

@pages_router.post("/api/user/regenerate-key")
async def regenerate_key_api(request: Request):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    new_key = regenerate_user_key(user["id"])
    return JSONResponse({"status": "ok", "api_key": new_key})

@pages_router.get("/api/user/analytics")
async def user_analytics_api(request: Request, range: str = "24h"):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = get_user_analytics(user["id"], range)
    return JSONResponse({"status": "ok", "data": data})

@pages_router.post("/api/admin/upgrade-vip")
async def admin_upgrade_vip(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    days = int(data.get("days", 1))
    new_exp = add_vip_days(target_id, days)
    return JSONResponse({"status": "ok", "vip_expires_at": new_exp})

@pages_router.post("/api/admin/revoke-vip")
async def admin_revoke_vip(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    revoke_vip(target_id)
    return JSONResponse({"status": "ok"})

@pages_router.post("/api/admin/delete-user")
async def admin_delete_user(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    delete_user(target_id)
    return JSONResponse({"status": "ok"})

@pages_router.post("/api/admin/toggle-ban")
async def admin_toggle_ban(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    is_banned = toggle_user_ban(target_id)
    return JSONResponse({"status": "ok", "is_banned": is_banned})

@pages_router.post("/api/admin/reset-quota")
async def admin_reset_quota(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    reset_user_quota(target_id)
    return JSONResponse({"status": "ok"})

@pages_router.post("/api/admin/settings")
async def admin_update_settings(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    form = await request.form()
    for k, v in form.items():
        update_setting(k, str(v))
    return RedirectResponse(url="/admin", status_code=303)
