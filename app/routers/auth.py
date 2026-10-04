import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from app.core.config import settings
from app.services.user_service import get_or_create_user

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])

DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"

def get_discord_redirect_uri(request: Request) -> str:
    host = request.headers.get("host")
    if host:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return f"{proto}://{host}/auth/discord/callback"
    return settings.DISCORD_REDIRECT_URI

@auth_router.get("/discord/login")
async def discord_login(request: Request):
    if not settings.DISCORD_CLIENT_ID or not settings.DISCORD_CLIENT_SECRET:
        # If Discord credentials not set yet, redirect to quick dev login
        return RedirectResponse(url="/auth/dev-login")
        
    redirect_uri = get_discord_redirect_uri(request)
    params = {
        "client_id": settings.DISCORD_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify email"
    }
    return RedirectResponse(f"{DISCORD_AUTH_URL}?{urlencode(params)}")

@auth_router.get("/discord/callback")
async def discord_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse(url="/login?error=discord_denied")
        
    redirect_uri = get_discord_redirect_uri(request)
    data = {
        "client_id": settings.DISCORD_CLIENT_ID,
        "client_secret": settings.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(DISCORD_TOKEN_URL, data=data, headers=headers)
        if token_resp.status_code != 200:
            return RedirectResponse(url="/login?error=token_failed")
            
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        
        user_resp = await client.get(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}"})
        if user_resp.status_code != 200:
            return RedirectResponse(url="/login?error=user_fetch_failed")
            
        discord_user = user_resp.json()
        
    discord_id = discord_user["id"]
    username = discord_user["username"]
    avatar_hash = discord_user.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png" if avatar_hash else "https://cdn.discordapp.com/embed/avatars/0.png"
    
    # Create or update user
    user = get_or_create_user(discord_id=discord_id, username=username, avatar_url=avatar_url)
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="portal_session",
        value=user["id"],
        max_age=86400 * 30, # 30 days
        httponly=True,
        samesite="lax"
    )
    return response

@auth_router.get("/dev-login")
async def dev_login(request: Request, role: str = "admin"):
    """
    Seamless test login for local development before Discord credentials are configured.
    Supports ?role=admin (Film_Admin) and ?role=user (Demo_User).
    """
    if role == "user":
        dev_id = "dev_user_002"
        dev_username = "Film_User"
        dev_avatar = "https://cdn.discordapp.com/embed/avatars/2.png"
        user = get_or_create_user(discord_id=dev_id, username=dev_username, avatar_url=dev_avatar, role="user")
    else:
        dev_id = "dev_admin_001"
        dev_username = "Film_Admin"
        dev_avatar = "https://cdn.discordapp.com/embed/avatars/1.png"
        user = get_or_create_user(discord_id=dev_id, username=dev_username, avatar_url=dev_avatar, role="admin")
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="portal_session",
        value=user["id"],
        max_age=86400 * 30,
        httponly=True,
        samesite="lax"
    )
    return response

@auth_router.get("/dev-user")
async def dev_user_login(request: Request):
    return await dev_login(request, role="user")

@auth_router.get("/dev-admin")
async def dev_admin_login(request: Request):
    return await dev_login(request, role="admin")

@auth_router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="portal_session")
    return response
