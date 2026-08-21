"""HTTP routes for authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_user
from ..mongodb import get_db as get_mongo_db
from ..repositories import ask_ai_repository
from ..runtime import settings
from ..services import auth_service
from ..utils.request_context import get_request_context

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=schemas.Token)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    try:
        token = auth_service.login(
            db,
            username=form.username,
            password=form.password,
            context=get_request_context(request),
        )
        # Login creates the durable AuthSession and audit record.  Persist
        # both before returning the token; otherwise the immediate /me call
        # rejects the token because its session row is still uncommitted.
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise


@router.get("/me", response_model=schemas.UserOut)
def me(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return auth_service.current_user(db, user)


# ── Google Drive OAuth ────────────────────────────────────────────────────────


@router.get("/google/status")
async def google_drive_status(
    user: models.User = Depends(get_current_user),
):
    """Return the Google Drive connection status for the current user."""
    mongo_db = get_mongo_db()
    mongo_user = await ask_ai_repository.get_mongo_user(mongo_db, user.id)
    connected = mongo_user is not None and mongo_user.google_drive_connected
    return {
        "connected": connected,
        "email": mongo_user.google_drive_email if connected and mongo_user else None,
    }


# ── Google Drive OAuth ────────────────────────────────────────────────────────

import os
import logging
_log = logging.getLogger(__name__)

# Allow HTTP transport for local development (http://localhost:8080)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Temporary in-memory state tracking for PKCE code verifier and user ID
OAUTH_SESSIONS: dict[str, dict] = {}


@router.get("/google/status")
async def google_drive_status(
    user: models.User = Depends(get_current_user),
):
    """Return the Google Drive connection status for the current user."""
    mongo_db = get_mongo_db()
    mongo_user = await ask_ai_repository.get_mongo_user(mongo_db, user.id)
    connected = mongo_user is not None and mongo_user.google_drive_connected
    return {
        "connected": connected,
        "email": mongo_user.google_drive_email if connected and mongo_user else None,
    }


@router.get("/google/connect")
def google_drive_connect(
    user: models.User = Depends(get_current_user),
):
    """Return the Google OAuth authorization URL for Drive access.

    The frontend opens this URL in a popup window to start the OAuth flow.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured on this server. Set DOCVAULT_GOOGLE_CLIENT_ID and DOCVAULT_GOOGLE_CLIENT_SECRET.",
        )
    try:
        from google_auth_oauthlib.flow import Flow  # type: ignore[import]

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uris": [settings.google_redirect_uri],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=[
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "openid",
            ],
        )
        flow.redirect_uri = settings.google_redirect_uri
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        # Store user_id and code_verifier for PKCE token exchange
        OAUTH_SESSIONS[state] = {
            "user_id": user.id,
            "code_verifier": getattr(flow, "code_verifier", None),
        }
        return {"auth_url": auth_url, "state": state}
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="google-auth-oauthlib is not installed. Run: pip install google-auth-oauthlib",
        )


@router.get("/google/callback", response_class=HTMLResponse)
async def google_drive_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Handle the OAuth callback from Google.

    Exchanges the authorization code for tokens, fetches the user's email,
    and stores the connection in MongoDB.  The popup then closes itself.
    """
    if error:
        _log.warning("Google OAuth callback returned error: %s", error)
        return _oauth_popup_close(success=False, message=f"Google OAuth error: {error}")

    if not code:
        _log.warning("Google OAuth callback missing code")
        return _oauth_popup_close(success=False, message="Missing OAuth authorization code.")

    session_info = OAUTH_SESSIONS.pop(state, {}) if state else {}
    postgres_user_id = session_info.get("user_id")
    if not postgres_user_id and state:
        try:
            postgres_user_id = int(state)
        except ValueError:
            postgres_user_id = 1
    elif not postgres_user_id:
        postgres_user_id = 1

    code_verifier = session_info.get("code_verifier")

    try:
        import httpx

        # Direct HTTP token exchange with Google
        token_payload = {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.google_redirect_uri,
        }
        if code_verifier:
            token_payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=20.0) as client:
            token_res = await client.post("https://oauth2.googleapis.com/token", data=token_payload)
            if token_res.status_code != 200:
                _log.error("Google token exchange failed (%s): %s", token_res.status_code, token_res.text)
                return _oauth_popup_close(success=False, message=f"Token exchange failed: {token_res.text}")

            token_data = token_res.json()
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")

            # Fetch user email from Google
            drive_email = None
            try:
                userinfo_res = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_res.status_code == 200:
                    drive_email = userinfo_res.json().get("email")
            except Exception as u_err:
                _log.warning("Failed to fetch userinfo from Google: %s", u_err)

        # Persist token and connection in MongoDB
        mongo_db = get_mongo_db()
        await ask_ai_repository.set_drive_connected(
            mongo_db,
            postgres_user_id=postgres_user_id,
            connected=True,
            email=drive_email,
            token={
                "token": access_token,
                "refresh_token": refresh_token,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "scopes": [
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                    "openid",
                ],
            },
        )
        _log.info("Google Drive successfully connected for user %s (%s)", postgres_user_id, drive_email)
        return _oauth_popup_close(success=True, email=drive_email)

    except Exception as exc:
        _log.exception("Google OAuth callback exception: %s", exc)
        return _oauth_popup_close(success=False, message=str(exc))


@router.post("/google/disconnect")
async def google_drive_disconnect(
    user: models.User = Depends(get_current_user),
):
    """Remove the Google Drive connection for the current user."""
    mongo_db = get_mongo_db()
    await ask_ai_repository.set_drive_connected(mongo_db, user.id, connected=False)
    return {"disconnected": True}


def _oauth_popup_close(success: bool, message: str = "", email: str | None = None) -> str:
    """Return an HTML page that posts a message to the opener and closes the popup."""
    import json
    payload = {"success": success, "message": message, "email": email or ""}
    payload_js = json.dumps(payload)
    close_script = "setTimeout(function() { window.close(); }, 600);" if success else ""
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Google Drive Connection</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: grid;
      place-content: center;
      min-height: 80vh;
      text-align: center;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 20px;
    }}
    .box {{
      padding: 24px 32px;
      border-radius: 12px;
      background: #1e293b;
      border: 1px solid {'#10b981' if success else '#ef4444'};
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
    }}
    h2 {{
      color: {'#10b981' if success else '#ef4444'};
      margin-top: 0;
    }}
    p {{
      color: #94a3b8;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="box">
    <h2>{"✓ Google Drive Connected!" if success else "✗ Connection Failed"}</h2>
    <p>{"Your Google Drive is now linked. You can close this window." if success else f"Error: {message}"}</p>
    {f'<p style="color:#38bdf8;font-weight:600;">{email}</p>' if email else ''}
  </div>
<script>
  (function() {{
    var payload = {payload_js};
    if (window.opener) {{
      window.opener.postMessage({{ type: 'GOOGLE_DRIVE_OAUTH', ...payload }}, '*');
    }}
    {close_script}
  }})();
</script>
</body>
</html>"""


