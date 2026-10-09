from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, Response
from app.services.user_service import (
    get_user_by_api_key, premium_trial_remaining, log_rejected_request,
    reserve_wallet, release_reservation, get_reserved_usd,
)
from app.services.proxy_service import forward_chat_completion, fetch_upstream_models, estimate_request_tokens
from app.core.catalog import is_premium_model, premium_worst_case_cost

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
    #   * Once the trial is exhausted, the wallet is used until it hits zero:
    #     any positive available balance admits the request, and the hold is
    #     capped at that balance. Only a truly empty wallet is rejected.
    #
    # The hold is a *worst-case* estimate priced at the model's own input and
    # output rates (not a single blended rate), which keeps it close to the real
    # cost instead of over-reserving.
    model = payload.get("model", "")
    reservation_id = 0
    if is_premium_model(model):
        est_in, est_out = estimate_request_tokens(payload, model)
        trial_remaining = premium_trial_remaining(user["id"], model)
        balance = float(user.get("balance") or 0)
        available = max(balance - get_reserved_usd(user["id"]), 0.0)

        # Worst-case USD this request can cost after the free trial is applied.
        required = round(premium_worst_case_cost(model, est_in, est_out, trial_remaining), 8)

        if required <= 0:
            # Fully covered by the free trial: no wallet hold needed.
            reservation_id = 0
        elif trial_remaining > 0:
            # Trial may cover part of it; never reject while trial remains. Hold
            # only what the balance can actually cover against the overage.
            hold = round(min(required, available), 8)
            if hold > 0:
                reservation_id = reserve_wallet(user["id"], model, hold) or 0
        else:
            # Wallet-only: use-until-zero. Admit the request while any balance
            # remains; cap the hold at the available balance so a concurrent
            # burst can never over-commit. Only an empty wallet is rejected.
            if available <= 0:
                log_rejected_request(user["id"], model, 402)
                raise HTTPException(
                    status_code=402,
                    detail={"error": {
                        "message": f"Wallet balance is empty for '{model}'. Top up to continue.",
                        "type": "insufficient_balance",
                        "code": 402,
                    }}
                )
            hold = round(min(required, available), 8)
            reservation_id = reserve_wallet(user["id"], model, hold)
            if reservation_id is None:
                log_rejected_request(user["id"], model, 402)
                raise HTTPException(
                    status_code=402,
                    detail={"error": {
                        "message": f"Available balance is already committed by in-flight requests for '{model}'. Retry shortly.",
                        "type": "insufficient_balance",
                        "code": 402,
                    }}
                )

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
