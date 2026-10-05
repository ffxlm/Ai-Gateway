from fastapi import APIRouter, Request, HTTPException, Depends, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
import csv
import io
import os
from app.core.config import settings
from app.core.database import db_session, get_setting, update_setting
from app.core.catalog import FREE_MODELS, PREMIUM_MODELS
from app.services.user_service import (
    get_or_create_user, regenerate_user_key, delete_user, toggle_user_ban,
    get_all_users, get_portal_stats, get_user_analytics, get_portal_analytics,
    add_balance, get_wallet_transactions, get_all_wallet_transactions,
    premium_trial_status, count_request_logs, get_request_logs,
    get_trial_history, get_reconciliation, get_request_summary, find_user_ids,
)
from app.services.payment_service import (
    generate_promptpay_payload, generate_qr_data_url, verify_slip_with_slipok, is_trans_ref_used,
    record_payment_transaction, get_recent_payments,
)
from app.services.proxy_service import fetch_upstream_models

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

pages_router = APIRouter(tags=["Pages"])


def _fmt_usd(value) -> str:
    """Compact USD formatting: 2 decimals normally, more when the amount is tiny."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    a = abs(v)
    if a == 0:
        return "0.00"
    if a >= 1:
        return f"{v:,.2f}"
    if a >= 0.01:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _fmt_thb(value, rate) -> str:
    """Convert a USD amount to THB at ``rate`` and format it with 2 decimals."""
    try:
        v = float(value) * float(rate)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:,.2f}"


templates.env.filters["usd"] = _fmt_usd
templates.env.filters["thb"] = _fmt_thb


def _usd_from_thb(amount_thb: float, rate: float) -> float:
    """Convert THB to USD keeping enough precision that the THB round-trips.

    Rounding to 2 decimals would over-credit (฿10 ÷ 35 = $0.2857 → $0.29 → ฿10.15),
    so keep 4 decimals; the THB display then rounds back to the paid amount.
    """
    if rate <= 0:
        return 0.0
    return round(float(amount_thb) / rate, 4)


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
    return user


def _usd_rate() -> float:
    try:
        rate = float(get_setting("usd_to_thb", str(settings.USD_TO_THB)) or settings.USD_TO_THB)
    except (TypeError, ValueError):
        rate = float(settings.USD_TO_THB)
    return rate if rate > 0 else float(settings.USD_TO_THB)


def _min_topup_thb() -> float:
    try:
        return float(get_setting("min_topup_thb", str(settings.MIN_TOPUP_THB)) or settings.MIN_TOPUP_THB)
    except (TypeError, ValueError):
        return float(settings.MIN_TOPUP_THB)


def _topup_packages(rate: float) -> list:
    raw = get_setting("topup_packages_thb", settings.TOPUP_PACKAGES_THB) or ""
    packages = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            thb = int(float(token))
        except ValueError:
            continue
        if thb <= 0:
            continue
        packages.append({"thb": thb, "usd": _usd_from_thb(thb, rate)})
    return packages


def _parse_request_filters(request: Request) -> dict:
    """Shared query-string parsing for the Request Log page and its CSV export."""
    q = request.query_params
    status = (q.get("status") or "").strip()
    if not status.isdigit():
        status = None
    user_id = (q.get("user_id") or "").strip() or None
    user_q = (q.get("user_q") or "").strip() or None
    if user_id:
        user_q = None  # an exact id (e.g. from a deep link) wins over the search box
    return {
        "user_id": user_id,
        "user_q": user_q,
        "model": (q.get("model") or "").strip() or None,
        "date_from": (q.get("date_from") or "").strip() or None,
        "date_to": (q.get("date_to") or "").strip() or None,
        "status": status,
        "premium_only": q.get("premium_only") in ("1", "true", "on"),
    }


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

    rate = _usd_rate()
    base_url = str(request.base_url).rstrip("/")
    api_endpoint = f"{base_url}/v1"

    analytics = get_user_analytics(user["id"], "24h")
    wallet_tx = get_wallet_transactions(user["id"], 12)

    # Per-model daily trial meters (each premium model gets its own allowance).
    trial_by_model = {m["id"]: premium_trial_status(user["id"], m["id"]) for m in PREMIUM_MODELS}
    any_trial_remaining = any(s["remaining"] > 0 for s in trial_by_model.values())

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "is_admin": is_real_admin,
        "preview_as_user": preview_as_user,
        "effective_role": effective_role,
        "balance": float(user.get("balance") or 0),
        "usd_to_thb": rate,
        "min_topup_thb": _min_topup_thb(),
        "topup_packages": _topup_packages(rate),
        "api_endpoint": api_endpoint,
        "discord_invite": get_setting("discord_invite_url", settings.DISCORD_INVITE_URL),
        "promptpay_id": get_setting("promptpay_id", settings.PROMPTPAY_ID),
        "promptpay_name": get_setting("promptpay_name", settings.PROMPTPAY_NAME),
        "models": FREE_MODELS,
        "premium_models": PREMIUM_MODELS,
        "trial_by_model": trial_by_model,
        "any_trial_remaining": any_trial_remaining,
        "wallet_tx": wallet_tx,
        "analytics": analytics,
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

    return templates.TemplateResponse(request, "login.html", {
        "error": error_msg,
        "free_models": FREE_MODELS,
        "usd_to_thb": _usd_rate(),
    })


@pages_router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/")

    stats = get_portal_stats()
    all_users = get_all_users()

    current_settings = {
        "master_router_url": get_setting("master_router_url", settings.MASTER_ROUTER_URL),
        "master_router_key": get_setting("master_router_key", settings.MASTER_ROUTER_KEY),
        "discord_invite_url": get_setting("discord_invite_url", settings.DISCORD_INVITE_URL),
        "promptpay_id": get_setting("promptpay_id", settings.PROMPTPAY_ID),
        "promptpay_name": get_setting("promptpay_name", settings.PROMPTPAY_NAME),
        "slipok_branch_id": get_setting("slipok_branch_id", settings.SLIPOK_BRANCH_ID),
        "slipok_api_key": get_setting("slipok_api_key", settings.SLIPOK_API_KEY),
        "usd_to_thb": get_setting("usd_to_thb", str(settings.USD_TO_THB)),
        "min_topup_thb": get_setting("min_topup_thb", str(settings.MIN_TOPUP_THB)),
        "topup_packages_thb": get_setting("topup_packages_thb", settings.TOPUP_PACKAGES_THB),
        "premium_trial_tokens_per_day": get_setting("premium_trial_tokens_per_day", str(settings.PREMIUM_TRIAL_TOKENS_PER_DAY)),
    }
    recent_payments = get_recent_payments(20)
    wallet_tx = get_all_wallet_transactions(30)
    portal_analytics = get_portal_analytics("7d")
    reconciliation = get_reconciliation()

    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "stats": stats,
        "users": all_users,
        "settings": current_settings,
        "payments": recent_payments,
        "wallet_tx": wallet_tx,
        "analytics": portal_analytics,
        "reconciliation": reconciliation,
    })


@pages_router.get("/admin/requests", response_class=HTMLResponse)
async def admin_requests_page(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/")

    filters = _parse_request_filters(request)
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    total = count_request_logs(**filters)
    logs = get_request_logs(**filters, limit=per_page, offset=offset)
    summary = get_request_summary(**filters)

    # Trial history follows the same user filter (resolved from the search box).
    if filters["user_id"]:
        trial_user_ids = [filters["user_id"]]
    elif filters["user_q"]:
        trial_user_ids = find_user_ids(filters["user_q"])
    else:
        trial_user_ids = None
    trial_limit = 200
    trial_history = get_trial_history(user_ids=trial_user_ids, days=14, limit=trial_limit)

    return templates.TemplateResponse(request, "admin_requests.html", {
        "user": user,
        "logs": logs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1),
        "filters": filters,
        "summary": summary,
        "model_options": FREE_MODELS + [m["id"] for m in PREMIUM_MODELS],
        "trial_history": trial_history,
        "trial_limit": trial_limit,
        "usd_to_thb": _usd_rate(),
    })


@pages_router.get("/admin/requests/export")
async def admin_requests_export(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    filters = _parse_request_filters(request)
    rows = get_request_logs(**filters, limit=100000, offset=0)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "time", "user_id", "username", "model", "premium",
        "tokens_in", "tokens_out", "tokens_total", "trial_tokens", "paid_tokens",
        "cost_usd", "balance_after", "usage_source", "status_code", "latency_ms",
    ])
    for r in rows:
        writer.writerow([
            r.get("created_at"), r.get("user_id"), r.get("username") or "", r.get("model"),
            1 if r.get("is_premium") else 0,
            r.get("tokens_in"), r.get("tokens_out"), r.get("tokens_used"),
            r.get("trial_tokens"), r.get("paid_tokens"),
            r.get("cost_usd"), r.get("balance_after"), r.get("usage_source"),
            r.get("status_code"), r.get("latency_ms"),
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="requests.csv"'},
    )


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


@pages_router.get("/api/admin/analytics")
async def admin_analytics_api(request: Request, range: str = "24h"):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = get_portal_analytics(range)
    return JSONResponse({"status": "ok", "data": data})


@pages_router.post("/api/admin/adjust-balance")
async def admin_adjust_balance(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    target_id = data.get("user_id")
    try:
        amount = float(data.get("amount_usd", 0))
    except (TypeError, ValueError):
        amount = 0.0
    note = str(data.get("note", "") or "").strip()
    if not target_id or amount == 0:
        return JSONResponse({"status": "error", "message": "ระบุผู้ใช้และจำนวนเงิน USD"}, status_code=400)
    new_balance = add_balance(
        target_id, amount, "adjust",
        note or ("Admin credit" if amount > 0 else "Admin debit"),
    )
    return JSONResponse({"status": "ok", "balance": new_balance})


@pages_router.post("/api/admin/delete-user")
async def admin_delete_user(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    delete_user(data.get("user_id"))
    return JSONResponse({"status": "ok"})


@pages_router.post("/api/admin/toggle-ban")
async def admin_toggle_ban(request: Request):
    user = get_session_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    is_banned = toggle_user_ban(data.get("user_id"))
    return JSONResponse({"status": "ok", "is_banned": is_banned})


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
async def get_promptpay_info(request: Request, amount: float = 0):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    rate = _usd_rate()
    min_thb = _min_topup_thb()
    amount = round(float(amount or 0), 2)
    if amount < min_thb:
        return JSONResponse({"status": "error", "message": f"ยอดเติมขั้นต่ำ ฿{min_thb:,.0f}"}, status_code=400)

    amount_usd = _usd_from_thb(amount, rate)
    promptpay_id = get_setting("promptpay_id", settings.PROMPTPAY_ID).strip()
    promptpay_name = get_setting("promptpay_name", settings.PROMPTPAY_NAME)
    qr_payload = generate_promptpay_payload(promptpay_id, amount) if promptpay_id else ""
    qr_image_url = generate_qr_data_url(qr_payload) if qr_payload else ""

    return JSONResponse({
        "status": "ok",
        "amount_thb": amount,
        "amount_usd": amount_usd,
        "usd_to_thb": rate,
        "promptpay_id": promptpay_id,
        "promptpay_name": promptpay_name,
        "qr_payload": qr_payload,
        "qr_image_url": qr_image_url,
    })


@pages_router.post("/api/payment/verify-slip")
async def verify_slip_api(
    request: Request,
    amount: float = Form(...),
    slip: UploadFile = File(...),
):
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    rate = _usd_rate()
    min_thb = _min_topup_thb()
    amount = round(float(amount or 0), 2)
    if amount < min_thb:
        return JSONResponse({"status": "error", "message": f"ยอดเติมขั้นต่ำ ฿{min_thb:,.0f}"}, status_code=400)

    file_bytes = await slip.read()
    if len(file_bytes) == 0:
        return JSONResponse({"status": "error", "message": "กรุณาแนบไฟล์รูปภาพสลิป"}, status_code=400)
    if len(file_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"status": "error", "message": "ขนาดไฟล์สลิปใหญ่เกิน 10MB"}, status_code=400)

    # Call SlipOK verification against the THB amount
    verification = await verify_slip_with_slipok(file_bytes, slip.filename, amount)
    if not verification.get("success"):
        return JSONResponse({
            "status": "error",
            "message": verification.get("message", "การตรวจสอบสลิปไม่สำเร็จ"),
        }, status_code=400)

    trans_ref = str(verification.get("trans_ref", "")).strip()
    if not trans_ref or is_trans_ref_used(trans_ref):
        return JSONResponse({
            "status": "error",
            "message": "สลิปนี้ถูกใช้งานไปแล้ว ไม่สามารถใช้ซ้ำได้",
        }, status_code=400)

    recorded = record_payment_transaction(user["id"], "topup", amount, trans_ref, "slipok")
    if not recorded:
        return JSONResponse({
            "status": "error",
            "message": "เกิดข้อผิดพลาดในการบันทึกข้อมูล หรือสลิปถูกใช้งานไปแล้ว",
        }, status_code=400)

    usd_credited = _usd_from_thb(amount, rate)
    new_balance = add_balance(
        user["id"], usd_credited, "topup",
        f"Top-up ฿{amount:,.2f}", trans_ref,
    )

    return JSONResponse({
        "status": "ok",
        "message": f"เติมเงินสำเร็จ! ได้รับ ${_fmt_usd(usd_credited)} เข้ากระเป๋าของคุณ",
        "usd_credited": usd_credited,
        "amount_thb": amount,
        "balance": new_balance,
    })
