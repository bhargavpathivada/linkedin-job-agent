"""Google OAuth sign-in / sign-up for LetItApply users."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any

from jobsearch_saas.config import (
    GOOGLE_AUTH_REDIRECT_URI,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    SECRET_KEY,
)
from jobsearch_saas.email.oauth import fetch_userinfo, oauth_configured


def sso_configured() -> bool:
    return oauth_configured()


def _sign(raw: str) -> str:
    return hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()[:20]


def make_state(
    *,
    mode: str,
    consents: dict[str, bool] | None = None,
    plan_id: str = "",
    next_path: str = "",
) -> str:
    """Signed state survives Google redirect even if session cookie is lost."""
    payload = {
        "n": secrets.token_urlsafe(10),
        "mode": mode,
        "consents": consents or {},
        "plan_id": (plan_id or "").strip(),
        "next_path": (next_path or "").strip(),
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw)}"


def parse_state(state: str) -> dict[str, Any]:
    if not state or "." not in state:
        raise ValueError("Invalid OAuth state")
    raw, sig = state.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), sig):
        raise ValueError("Invalid OAuth state signature")
    padded = raw + "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def build_auth_url(*, state: str) -> str:
    if not oauth_configured():
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured")
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_AUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict[str, Any]:
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_AUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if "redirect_uri_mismatch" in body:
            raise RuntimeError(
                f"Google redirect URI mismatch. Add this exact URL in Google Cloud Console: {GOOGLE_AUTH_REDIRECT_URI}"
            ) from exc
        raise RuntimeError(f"Google token exchange failed: {body[:200]}") from exc


def profile_from_code(code: str) -> dict[str, str]:
    tokens = exchange_code(code)
    info = fetch_userinfo(tokens["access_token"])
    email = (info.get("email") or "").strip().lower()
    if not email:
        raise RuntimeError("Google did not return an email address")
    if info.get("email_verified") is False:
        raise RuntimeError("Please verify your Google email before signing in")
    return {
        "email": email,
        "full_name": (info.get("name") or info.get("given_name") or "").strip(),
        "picture": (info.get("picture") or "").strip(),
    }
