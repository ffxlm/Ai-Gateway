from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, Response
from app.services.user_service import get_user_by_api_key, premium_trial_remaining
from app.services.proxy_service import forward_chat_completion, fetch_upstream_models
from app.core.catalog import is_premium_model

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
    model = payload.get("model", "")
    if is_premium_model(model):
        if premium_trial_remaining(user["id"], model) <= 0 and float(user.get("balance") or 0) <= 0:
            raise HTTPException(
                status_code=402,
                detail={"error": {
                    "message": f"Daily free trial used up and wallet balance is empty. Top up to keep using '{model}'.",
                    "type": "insufficient_balance",
                    "code": 402,
                }}
            )

    status_code, content_type, result = await forward_chat_completion(user, payload)

    if content_type == "text/event-stream":
        return StreamingResponse(result, media_type="text/event-stream")
    else:
        return Response(content=result, status_code=status_code, media_type=content_type)

@gateway_router.get("/models")
async def get_models(user: dict = Depends(get_current_api_user)):
    models_data = await fetch_upstream_models()
    return JSONResponse(content=models_data)
