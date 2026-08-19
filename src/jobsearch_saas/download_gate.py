"""Signed, account-bound Companion download access. Not usable if forwarded."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any
from urllib.parse import quote

from jobsearch_saas.config import BASE_URL, SECRET_KEY

DOWNLOAD_PATH = "/download"
TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
SESSION_UID_KEY = "companion_download_uid"
SESSION_EXP_KEY = "companion_download_exp"
SESSION_TTL_SECONDS = 2 * 60 * 60


def _sign(raw: str) -> str:
    return hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]


def make_download_token(user_id: str, *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{user_id}.{exp}"
    raw = f"{payload}.{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def parse_download_token(token: str) -> str:
    if not token:
        raise ValueError("Missing download token")
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception as exc:
        raise ValueError("Invalid download token") from exc
    parts = decoded.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError("Invalid download token")
    payload, sig = parts
    if not hmac.compare_digest(_sign(payload), sig):
        raise ValueError("Invalid download token")
    user_id, _, exp_raw = payload.partition(".")
    if not user_id or not exp_raw.isdigit():
        raise ValueError("Invalid download token")
    if int(exp_raw) < int(time.time()):
        raise ValueError("Download link expired")
    return user_id


def download_url_for_user(user_id: str) -> str:
    token = make_download_token(user_id)
    return f"{BASE_URL.rstrip('/')}{DOWNLOAD_PATH}?k={quote(token, safe='')}"


def grant_download_session(session: dict[str, Any], user_id: str) -> None:
    session[SESSION_UID_KEY] = user_id
    session[SESSION_EXP_KEY] = time.time() + SESSION_TTL_SECONDS


def session_allows_download(session: dict[str, Any], user_id: str) -> bool:
    if session.get(SESSION_UID_KEY) != user_id:
        return False
    try:
        exp = float(session.get(SESSION_EXP_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return exp > time.time()


def safe_next_path(value: str | None) -> str:
    path = (value or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return ""
    if path.startswith("/login") or path.startswith("/auth/") or path.startswith("/logout"):
        return ""
    return path


def login_redirect_for_download(token: str = "") -> str:
    nxt = DOWNLOAD_PATH
    if token:
        nxt = f"{DOWNLOAD_PATH}?k={quote(token, safe='')}"
    return f"/login?next={quote(nxt, safe='')}"


def token_from_request_params(params: Any) -> str:
    return str(params.get("k") or "").strip()
