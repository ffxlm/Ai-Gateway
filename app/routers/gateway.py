from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse, Response
from app.services.user_service import get_user_by_api_key, check_user_quota
from app.services.proxy_service import forward_chat_completion, fetch_upstream_models

gateway_router = APIRouter(prefix="/v1", tags=["OpenAI Gateway"])

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
    
    allowed, reason = check_user_quota(user)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": reason, "type": "quota_exceeded", "code": 429}}
        )
        
    return user

@gateway_router.post("/chat/completions")
async def chat_completions(request: Request, user: dict = Depends(get_current_api_user)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Malformed JSON payload.", "type": "invalid_request", "code": 400}})
    
    status_code, content_type, result = await forward_chat_completion(user, payload)
    
    if content_type == "text/event-stream":
        return StreamingResponse(result, media_type="text/event-stream")
    else:
        return Response(content=result, status_code=status_code, media_type=content_type)

@gateway_router.get("/models")
async def get_models(user: dict = Depends(get_current_api_user)):
    models_data = await fetch_upstream_models()
    return JSONResponse(content=models_data)
