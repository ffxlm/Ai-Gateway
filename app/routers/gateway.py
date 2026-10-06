from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, Response
from app.services.user_service import (
    get_user_by_api_key, premium_trial_remaining, log_rejected_request,
    reserve_wallet, release_reservation, get_reserved_usd,
)
from app.services.proxy_service import forward_chat_completion, fetch_upstream_models, estimate_request_tokens
from app.core.catalog import is_premium_model, premium_output_price

gateway_router = APIRouter(prefix="/v1", tags=["[OI] Gateway"])

async def get_current_api_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing Authorization header. Use 'Bearer sk-portal-...' format.", "type": "auth_error", "code": 401}}
        )

    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid Authorization header format. Expected 'Bearer <key>'.", "type": "auth_error", "code": 401}}
        )

    api_key = parts[1].strip()
    user = get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid or non-existent API Key.", "type": "invalid_api_key", "code": 401}}
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=403,
            detail={"error": {"message": "Your account has been suspended by the administrator.", "type": "account_suspended", "code": 403}}
        )

    return user

@gateway_router.post("/chat/completions")
async def chat_completions(request: Request, user: dict = Depends(get_current_api_user)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Malformed JSON payload.", "type": "invalid_request", "code": 400}})

    # Premium (xHigh) models are billed from the USD wallet, but each user gets a
    # daily free trial allowance first. Free models are unlimited.
    #
    # Admission control runs BEFORE the upstream call and is cost-aware, so a
    # single request can never debit past zero:
    #   * While trial remains, the request is never rejected (the trial may
    #     cover it); the balance only holds whatever it can against the overage.
    #   * Once the trial is exhausted, the wallet must fully cover the worst-case
    #     token cost, which is held so concurrent requests cannot double-spend.
    model = payload.get("model", "")
    reservation_id = 0
    if is_premium_model(model):
        est_in, est_out = estimate_request_tokens(payload, model)
        est_total = est_in + est_out
        trial_remaining = premium_trial_remaining(user["id"], model)
        balance = float(user.get("balance") or 0)
        available = max(balance - get_reserved_usd(user["id"]), 0.0)
        # Convert USD to tokens at the priciest (output) rate, for a worst case.
        price_per_token = premium_output_price(model) / 1_000_000.0

        if trial_remaining <= 0:
            # Wallet-only mode: it must cover the whole worst case.
            required = round(est_total * price_per_token, 8)
            if available <= 0 or required > available + 1e-9:
                log_rejected_request(user["id"], model, 402)
                raise HTTPException(
                    status_code=402,
                    detail={"error": {
                        "message": f"Daily free trial used up and wallet balance is too low to cover this request for '{model}'. Top up or lower max_tokens.",
                        "type": "insufficient_balance",
                        "code": 402,
                    }}
                )
            reservation_id = reserve_wallet(user["id"], model, required)
            if reservation_id is None:
                log_rejected_request(user["id"], model, 402)
                raise HTTPException(
                    status_code=402,
                    detail={"error": {
                        "message": f"Insufficient available balance to cover this request for '{model}'. Top up or lower max_tokens.",
                        "type": "insufficient_balance",
                        "code": 402,
                    }}
                )
        else:
            # Trial-first: hold only what the balance can cover of the overage.
            # A request the trial might fully satisfy is never rejected here.
            billable_est = max(est_total - trial_remaining, 0)
            hold = round(min(billable_est * price_per_token, available), 8)
            if hold > 0:
                reservation_id = reserve_wallet(user["id"], model, hold) or 0

    try:
        status_code, content_type, result = await forward_chat_completion(user, payload, reservation_id)
    except Exception:
        # The request never reached settlement: release the hold immediately.
        release_reservation(reservation_id)
        raise

    if content_type == "text/event-stream":
        return StreamingResponse(result, media_type="text/event-stream")
    else:
        return Response(content=result, status_code=status_code, media_type=content_type)

@gateway_router.get("/models")
async def get_models(user: dict = Depends(get_current_api_user)):
    models_data = await fetch_upstream_models()
    return JSONResponse(content=models_data)
