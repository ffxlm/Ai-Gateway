from fastapi import APIRouter, Request, HTTPException, Depends, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import os
from app.core.config import settings
from app.core.database import db_session, get_setting, update_setting
from app.services.user_service import (
    get_or_create_user, regenerate_user_key, add_vip_days, revoke_vip, delete_user,
    toggle_user_ban, reset_user_quota, get_all_users, get_portal_stats, is_vip_active,
    get_user_analytics, apply_daily_rollover
)
from app.services.payment_service import (
    generate_promptpay_payload, generate_qr_data_url, verify_slip_with_slipok, is_trans_ref_used,
    record_payment_transaction, get_recent_payments
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
        user = dict(row) if row else None
    if not user:
        return None
    # Apply the daily quota rollover so the dashboard shows the reset right after midnight.
    return apply_daily_rollover(user)

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
        "promptpay_id": get_setting("promptpay_id", settings.PROMPTPAY_ID),
        "promptpay_name": get_setting("promptpay_name", settings.PROMPTPAY_NAME),
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
        "vip_daily_price": get_setting("vip_daily_price", str(settings.VIP_DAILY_PRICE)),
        "vip_weekly_price": get_setting("vip_weekly_price", str(settings.VIP_WEEKLY_PRICE)),
        "promptpay_id": get_setting("promptpay_id", settings.PROMPTPAY_ID),
        "slipok_branch_id": get_setting("slipok_branch_id", settings.SLIPOK_BRANCH_ID),
        "slipok_api_key": get_setting("slipok_api_key", settings.SLIPOK_API_KEY)
    }
    recent_payments = get_recent_payments(20)
    
    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "stats": stats,
        "users": all_users,
        "settings": current_settings,
        "payments": recent_payments
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

@pages_router.get("/api/payment/promptpay-info")
async def get_promptpay_info(request: Request, pass_type: str = "daily"):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    promptpay_id = get_setting("promptpay_id", settings.PROMPTPAY_ID).strip()
    if pass_type == "weekly":
        amount = float(get_setting("vip_weekly_price", str(settings.VIP_WEEKLY_PRICE)))
        days = 7
        title = "Weekly Pass (7 วัน)"
    else:
        amount = float(get_setting("vip_daily_price", str(settings.VIP_DAILY_PRICE)))
        days = 1
        title = "Daily Pass (24 ชั่วโมง)"

    qr_payload = generate_promptpay_payload(promptpay_id, amount) if promptpay_id else ""
    qr_image_url = generate_qr_data_url(qr_payload) if qr_payload else ""
    promptpay_name = get_setting("promptpay_name", settings.PROMPTPAY_NAME)
    return JSONResponse({
        "status": "ok",
        "pass_type": pass_type,
        "title": title,
        "amount": amount,
        "days": days,
        "promptpay_id": promptpay_id,
        "promptpay_name": promptpay_name,
        "qr_payload": qr_payload,
        "qr_image_url": qr_image_url
    })

@pages_router.post("/api/payment/verify-slip")
async def verify_slip_api(
    request: Request,
    pass_type: str = Form(...),
    slip: UploadFile = File(...)
):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if pass_type == "weekly":
        expected_amount = float(get_setting("vip_weekly_price", str(settings.VIP_WEEKLY_PRICE)))
        days = 7
    else:
        expected_amount = float(get_setting("vip_daily_price", str(settings.VIP_DAILY_PRICE)))
        days = 1

    file_bytes = await slip.read()
    if len(file_bytes) == 0:
        return JSONResponse({"status": "error", "message": "กรุณาแนบไฟล์รูปภาพสลิป"}, status_code=400)
    if len(file_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"status": "error", "message": "ขนาดไฟล์สลิปใหญ่เกิน 10MB"}, status_code=400)

    # Call SlipOK verification
    verification = await verify_slip_with_slipok(file_bytes, slip.filename, expected_amount)
    if not verification.get("success"):
        return JSONResponse({
            "status": "error",
            "message": verification.get("message", "การตรวจสอบสลิปไม่สำเร็จ")
        }, status_code=400)

    trans_ref = str(verification.get("trans_ref", "")).strip()
    if not trans_ref or is_trans_ref_used(trans_ref):
        return JSONResponse({
            "status": "error",
            "message": "สลิปนี้ถูกใช้งานไปแล้ว ไม่สามารถใช้ซ้ำได้"
        }, status_code=400)

    # Record payment and grant VIP
    recorded = record_payment_transaction(user["id"], pass_type, expected_amount, trans_ref, "slipok")
    if not recorded:
        return JSONResponse({
            "status": "error",
            "message": "เกิดข้อผิดพลาดในการบันทึกข้อมูล หรือสลิปถูกใช้งานไปแล้ว"
        }, status_code=400)

    new_exp = add_vip_days(user["id"], days)
    return JSONResponse({
        "status": "ok",
        "message": f"ชำระเงินสำเร็จ! บัญชีของคุณได้รับการอัปเกรดเป็น VIP เรียบร้อยแล้ว (+{days} วัน)",
        "days_added": days,
        "vip_expires_at": new_exp
    })
