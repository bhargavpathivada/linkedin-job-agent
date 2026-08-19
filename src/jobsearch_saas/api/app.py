"""
LetItApply — FastAPI web app.

Run:
  uvicorn jobsearch_saas.api.app:app --reload --app-dir src
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from jobsearch_saas import companion as companion_svc
from jobsearch_saas import db
from jobsearch_saas.auth import (
    create_session,
    destroy_session,
    get_or_create_google_user,
    record_consent,
    user_from_session,
)
from jobsearch_saas import google_sso
from jobsearch_saas.billing import qr_payments, razorpay_billing
from jobsearch_saas.admin import auth as admin_auth
from jobsearch_saas.config import (
    ALLOW_BETA_GRANT,
    APP_NAME,
    BASE_URL,
    COMPANION_DOWNLOAD_URL,
    PLANS,
    SECRET_KEY,
    SESSION_COOKIE,
    SUPPORT_EMAIL,
    WEB_SESSION_COOKIE,
)
from jobsearch_saas.download_gate import (
    grant_download_session,
    login_redirect_for_download,
    parse_download_token,
    safe_next_path,
    session_allows_download,
    token_from_request_params,
)
from jobsearch_saas.drafts import (
    approve_and_send,
    create_draft_for_match,
    get_draft,
    list_drafts,
    reject_draft,
    update_draft,
)
from jobsearch_saas.email import oauth as email_oauth
from jobsearch_saas.entitlements import active_plan, grant_beta_pass, has_approved_access
from jobsearch_saas.jobs.matching import get_search_prefs, list_matches, match_user_to_open_jobs, save_search_prefs
from jobsearch_saas.jobs.sources import ingest_for_query, parse_user_paste, source_catalog, upsert_job
from jobsearch_saas.privacy import controls as privacy
from jobsearch_saas.profiles import (
    dashboard_stats,
    get_profile_bundle,
    list_applications,
    save_resume_file,
    set_application_stage,
    update_profile,
)
from jobsearch_saas.workers import queue as job_queue

PACKAGE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

app = FastAPI(title=APP_NAME, version="0.2.0")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie=WEB_SESSION_COOKIE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
try:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Read-only filesystem on some serverless hosts — package static/ is enough.
    pass
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class CompanionLoginBody(BaseModel):
    email: str
    password: str
    device_id: str = ""
    device_name: str = "LetItApply Companion"


class CompanionPostsBody(BaseModel):
    posts: list[dict[str, Any]] = Field(default_factory=list)


class CompanionStatusBody(BaseModel):
    linkedin_connected: bool | None = None
    last_error: str | None = None
    device_name: str | None = None


def require_companion_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    user = companion_svc.user_from_companion_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired companion token")
    return user


def _user(request: Request) -> dict[str, Any] | None:
    try:
        return user_from_session(request.cookies.get(SESSION_COOKIE))
    except Exception:
        return None


def require_user(request: Request) -> dict[str, Any]:
    user = _user(request)
    if not user:
        nxt = safe_next_path(request.url.path)
        location = f"/login?next={quote(nxt, safe='')}" if nxt else "/login"
        raise HTTPException(status_code=303, headers={"Location": location})
    return user


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    user = _user(request)
    plan = active_plan(user["id"]) if user else None
    context = {
        "request": request,
        "app_name": APP_NAME,
        "user": user,
        "user_initials": _user_initials(user),
        "plan": plan,
        "can_download": bool(user and has_approved_access(user["id"])),
        "support_email": SUPPORT_EMAIL,
        "flash": request.session.pop("flash", None),
    }
    context.update(ctx)
    # Starlette 0.37+ requires (request, name, context); older used (name, context).
    return templates.TemplateResponse(request, name, context)


def render_admin(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    context = {
        "request": request,
        "app_name": APP_NAME,
        "admin_email": admin_auth.admin_from_session(request),
        "flash": request.session.pop("admin_flash", None),
    }
    context.update(ctx)
    return templates.TemplateResponse(request, name, context)


def admin_flash(request: Request, message: str) -> None:
    request.session["admin_flash"] = message


def require_admin(request: Request) -> str:
    email = admin_auth.admin_from_session(request)
    if not email:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return email


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _user_initials(user: dict[str, Any] | None) -> str:
    if not user:
        return "?"
    name = (user.get("full_name") or user.get("email") or "?").strip()
    parts = [p for p in name.replace("@", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return (parts[0][:2] if parts else "?").upper()


# ── Public / legal ─────────────────────────────────────────────


@app.on_event("startup")
def _startup() -> None:
    try:
        db.init_db()
    except Exception as exc:
        # Don't brick the whole deploy if Mongo is briefly unreachable on cold start.
        print(f"db.init_db deferred: {exc}")


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return render(request, "landing.html", plans=razorpay_billing.catalog_for_display())


@app.get("/legal/terms", response_class=HTMLResponse)
def terms(request: Request) -> HTMLResponse:
    return render(request, "legal/terms.html")


@app.get("/legal/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    return render(request, "legal/privacy.html")


@app.get("/legal/refunds", response_class=HTMLResponse)
def refunds(request: Request) -> HTMLResponse:
    return render(request, "legal/refunds.html")


@app.get("/legal/sources", response_class=HTMLResponse)
def sources_disclosure(request: Request) -> HTMLResponse:
    return render(request, "legal/sources.html", sources=source_catalog())


# ── Auth ───────────────────────────────────────────────────────


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request) -> HTMLResponse:
    if _user(request):
        plan = request.query_params.get("plan", "")
        if plan in PLANS and plan != "free":
            return RedirectResponse(f"/billing/checkout?plan={plan}", status_code=303)
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "auth/signup.html",
        purposes=privacy.CONSENT_PURPOSES,
        oauth_ready=google_sso.sso_configured(),
        selected_plan=request.query_params.get("plan", ""),
    )


@app.post("/signup")
def signup_start(
    request: Request,
    consent_job_matching: str | None = Form(None),
    consent_email_sending: str | None = Form(None),
    consent_product_updates: str | None = Form(None),
    plan_id: str = Form(""),
) -> RedirectResponse:
    if not google_sso.sso_configured():
        flash(request, "Google sign-in is not configured.")
        return RedirectResponse("/signup", status_code=303)
    if not consent_job_matching:
        flash(request, "Job matching consent is required to use the product.")
        return RedirectResponse("/signup", status_code=303)
    plan = plan_id if plan_id in PLANS and plan_id != "free" else ""
    state = google_sso.make_state(
        mode="signup",
        consents={
            "job_matching": True,
            "email_sending": bool(consent_email_sending),
            "product_updates": bool(consent_product_updates),
        },
        plan_id=plan,
    )
    return RedirectResponse(google_sso.build_auth_url(state=state), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    next_path = safe_next_path(request.query_params.get("next", ""))
    if _user(request):
        return RedirectResponse(next_path or "/", status_code=303)
    selected_plan = request.query_params.get("plan", "")
    google_href = "/auth/google"
    params: list[str] = []
    if selected_plan:
        params.append(f"plan={quote(selected_plan, safe='')}")
    if next_path:
        params.append(f"next={quote(next_path, safe='')}")
    if params:
        google_href = f"{google_href}?{'&'.join(params)}"
    return render(
        request,
        "auth/login.html",
        oauth_ready=google_sso.sso_configured(),
        selected_plan=selected_plan,
        next_path=next_path,
        google_href=google_href,
    )


@app.get("/auth/google")
def auth_google_start(request: Request, plan: str = "", next: str = "") -> RedirectResponse:
    if not google_sso.sso_configured():
        flash(request, "Google sign-in is not configured.")
        return RedirectResponse("/login", status_code=303)
    plan_id = plan if plan in PLANS and plan != "free" else ""
    state = google_sso.make_state(
        mode="login",
        plan_id=plan_id,
        next_path=safe_next_path(next),
    )
    return RedirectResponse(google_sso.build_auth_url(state=state), status_code=303)


@app.get("/auth/google/callback")
def auth_google_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not code:
        flash(request, "Google sign-in was cancelled or failed.")
        return RedirectResponse("/login", status_code=303)
    try:
        state_data = google_sso.parse_state(state)
    except ValueError:
        flash(request, "Google sign-in expired — please try again.")
        return RedirectResponse("/login", status_code=303)
    try:
        profile = google_sso.profile_from_code(code)
        user, is_new = get_or_create_google_user(
            email=profile["email"],
            full_name=profile["full_name"],
        )
    except Exception as exc:
        msg = str(exc) or "Google sign-in failed."
        if db.is_mongo_unreachable(exc):
            msg = db.MONGO_UNREACHABLE_HINT
        flash(request, msg)
        return RedirectResponse("/login", status_code=303)

    consents = state_data.get("consents") or {}
    if is_new:
        record_consent(user["id"], purpose="job_matching", granted=True)
        record_consent(user["id"], purpose="email_sending", granted=bool(consents.get("email_sending")))
        record_consent(user["id"], purpose="product_updates", granted=bool(consents.get("product_updates")))
    token = create_session(user["id"])
    pending_plan = (state_data.get("plan_id") or "").strip()
    next_path = safe_next_path(state_data.get("next_path") or "")
    if pending_plan in PLANS and pending_plan != "free":
        try:
            order = qr_payments.start_checkout(user["id"], pending_plan)
            dest = f"/billing/pay/{order['payment_id']}"
        except Exception:
            dest = "/billing"
    elif is_new:
        dest = "/onboarding"
    elif next_path:
        dest = next_path
    else:
        dest = "/"
    flash(request, "Signed in with Google.")
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return resp


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(
        request,
        "dashboard/profile.html",
        bundle=get_profile_bundle(user["id"]),
        entitlement=active_plan(user["id"]),
        transactions=qr_payments.list_user_transactions(user["id"]),
    )


# ── Onboarding ─────────────────────────────────────────────────


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    bundle = get_profile_bundle(user["id"])
    prefs = get_search_prefs(user["id"])
    return render(
        request,
        "onboarding/index.html",
        bundle=bundle,
        prefs=prefs,
        step=request.query_params.get("step", "1"),
        email_conn=email_oauth.get_connection(user["id"]),
        oauth_ready=email_oauth.oauth_configured(),
    )


@app.post("/onboarding/profile")
async def onboarding_profile(
    request: Request,
    user: dict = Depends(require_user),
    full_name: str = Form(...),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    github_url: str = Form(""),
    headline: str = Form(""),
    years_experience: float = Form(0),
    skills: str = Form(""),
    summary: str = Form(""),
) -> RedirectResponse:
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    update_profile(
        user["id"],
        full_name=full_name,
        phone=phone,
        linkedin_url=linkedin_url,
        github_url=github_url,
        headline=headline,
        years_experience=years_experience,
        skills=skill_list,
        summary=summary,
    )
    flash(request, "Profile saved.")
    return RedirectResponse("/onboarding?step=2", status_code=303)


@app.post("/onboarding/resume")
async def onboarding_resume(
    request: Request,
    user: dict = Depends(require_user),
    resume: UploadFile = File(...),
    label: str = Form("Primary"),
) -> RedirectResponse:
    content = await resume.read()
    if not content:
        flash(request, "Empty file.")
        return RedirectResponse("/onboarding?step=2", status_code=303)
    save_resume_file(user["id"], filename=resume.filename or "resume.pdf", content=content, label=label)
    flash(request, "Resume uploaded.")
    return RedirectResponse("/onboarding?step=3", status_code=303)


@app.post("/onboarding/prefs")
def onboarding_prefs(
    request: Request,
    user: dict = Depends(require_user),
    roles: str = Form("Software Engineer, Python Developer"),
    locations: str = Form("India, Remote"),
    max_years_experience: float = Form(3),
    daily_application_limit: int = Form(10),
    exclusions: str = Form(""),
    preferred_apply_route: str = Form("email"),
) -> RedirectResponse:
    save_search_prefs(
        user["id"],
        {
            "roles": [r.strip() for r in roles.split(",") if r.strip()],
            "locations": [r.strip() for r in locations.split(",") if r.strip()],
            "max_years_experience": max_years_experience,
            "daily_application_limit": daily_application_limit,
            "exclusions": [r.strip() for r in exclusions.split(",") if r.strip()],
            "preferred_apply_route": preferred_apply_route,
            "auto_send_enabled": False,
        },
    )
    flash(request, "Search preferences saved.")
    return RedirectResponse("/onboarding?step=4", status_code=303)


@app.post("/onboarding/preview")
def onboarding_preview(request: Request, user: dict = Depends(require_user)) -> RedirectResponse:
    prefs = get_search_prefs(user["id"])
    query = (prefs.get("roles") or ["software engineer"])[0]
    ingest_for_query(query)
    matches = match_user_to_open_jobs(user["id"], limit=10)
    # Create up to 3 drafts for highest matches with apply email
    drafted = 0
    for m in matches:
        if drafted >= 3:
            break
        full = list_matches(user["id"], limit=50)
        row = next((x for x in full if x["id"] == m["id"]), None)
        if row and row.get("apply_email"):
            try:
                create_draft_for_match(user["id"], m["id"])
                drafted += 1
            except Exception:
                continue
    flash(request, f"Preview ready: {len(matches)} matches, {drafted} drafts.")
    return RedirectResponse("/dashboard", status_code=303)


# ── Dashboard ──────────────────────────────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(
        request,
        "dashboard/home.html",
        stats=dashboard_stats(user["id"]),
        matches=list_matches(user["id"], limit=15),
        drafts=list_drafts(user["id"]),
        applications=list_applications(user["id"])[:10],
        companion=companion_svc.get_status(user["id"]),
    )


def _private_download_response(request: Request, name: str, status_code: int = 200) -> HTMLResponse:
    resp = render(request, name, companion_url=COMPANION_DOWNLOAD_URL)
    resp.status_code = status_code
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.post("/download/open")
def download_open(request: Request, user: dict = Depends(require_user)) -> RedirectResponse:
    if not has_approved_access(user["id"]):
        flash(request, "Companion download unlocks after your payment is approved.")
        return RedirectResponse("/billing", status_code=303)
    grant_download_session(request.session, user["id"])
    return RedirectResponse("/download", status_code=303)


@app.get("/download", response_class=HTMLResponse)
def download_page(request: Request) -> HTMLResponse:
    token = token_from_request_params(request.query_params)
    user = _user(request)
    if not user:
        return RedirectResponse(login_redirect_for_download(token), status_code=303)

    token_ok = False
    if token:
        try:
            token_user_id = parse_download_token(token)
        except ValueError:
            return _private_download_response(request, "download_locked.html", 403)
        if token_user_id != user["id"]:
            return _private_download_response(request, "download_locked.html", 403)
        token_ok = True
        grant_download_session(request.session, user["id"])

    session_ok = session_allows_download(request.session, user["id"])
    if not has_approved_access(user["id"]) or not (token_ok or session_ok):
        return _private_download_response(request, "download_locked.html", 403)

    return _private_download_response(request, "download.html")


@app.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(request, "dashboard/matches.html", matches=list_matches(user["id"], limit=50))


@app.post("/matches/refresh")
def matches_refresh(request: Request, user: dict = Depends(require_user)) -> RedirectResponse:
    prefs = get_search_prefs(user["id"])
    for role in (prefs.get("roles") or ["software"])[:3]:
        job_queue.enqueue_ingest(role)
    job_queue.process_all(max_jobs=5)
    match_user_to_open_jobs(user["id"], limit=20)
    flash(request, "Matches refreshed from permitted sources.")
    return RedirectResponse("/matches", status_code=303)


@app.post("/matches/{match_id}/draft")
def match_draft(request: Request, match_id: str, user: dict = Depends(require_user)) -> RedirectResponse:
    try:
        draft = create_draft_for_match(user["id"], match_id)
    except Exception as exc:
        flash(request, str(exc))
        return RedirectResponse("/matches", status_code=303)
    return RedirectResponse(f"/drafts/{draft['id']}", status_code=303)


@app.get("/drafts/{draft_id}", response_class=HTMLResponse)
def draft_workspace(request: Request, draft_id: str, user: dict = Depends(require_user)) -> HTMLResponse:
    draft = get_draft(user["id"], draft_id)
    if not draft:
        raise HTTPException(404)
    return render(
        request,
        "dashboard/draft.html",
        draft=draft,
        email_conn=email_oauth.get_connection(user["id"]),
    )


@app.post("/drafts/{draft_id}/save")
def draft_save(
    request: Request,
    draft_id: str,
    user: dict = Depends(require_user),
    subject: str = Form(...),
    body: str = Form(...),
    to_email: str = Form(""),
) -> RedirectResponse:
    update_draft(user["id"], draft_id, subject=subject, body=body, to_email=to_email or None)
    flash(request, "Draft saved.")
    return RedirectResponse(f"/drafts/{draft_id}", status_code=303)


@app.post("/drafts/{draft_id}/approve")
def draft_approve(request: Request, draft_id: str, user: dict = Depends(require_user)) -> RedirectResponse:
    try:
        approve_and_send(user["id"], draft_id)
        flash(request, "Application email sent from your Gmail.")
    except Exception as exc:
        flash(request, str(exc))
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
    return RedirectResponse("/tracker", status_code=303)


@app.post("/drafts/{draft_id}/reject")
def draft_reject(request: Request, draft_id: str, user: dict = Depends(require_user)) -> RedirectResponse:
    reject_draft(user["id"], draft_id)
    flash(request, "Draft rejected.")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(request, "dashboard/tracker.html", applications=list_applications(user["id"]))


@app.post("/tracker/{application_id}/stage")
def tracker_stage(
    request: Request,
    application_id: str,
    user: dict = Depends(require_user),
    stage: str = Form(...),
) -> RedirectResponse:
    try:
        set_application_stage(user["id"], application_id, stage)
        flash(request, f"Stage updated to {stage}.")
    except Exception as exc:
        flash(request, str(exc))
    return RedirectResponse("/tracker", status_code=303)


@app.get("/jobs/paste", response_class=HTMLResponse)
def paste_job_form(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(request, "dashboard/paste_job.html")


@app.post("/jobs/paste")
def paste_job(
    request: Request,
    user: dict = Depends(require_user),
    title: str = Form(...),
    company: str = Form(...),
    description: str = Form(...),
    location: str = Form("India"),
    apply_email: str = Form(""),
    apply_url: str = Form(""),
) -> RedirectResponse:
    job = parse_user_paste(
        title=title,
        company=company,
        description=description,
        location=location,
        apply_email=apply_email,
        apply_url=apply_url,
    )
    upsert_job(job)
    match_user_to_open_jobs(user["id"], limit=5)
    flash(request, "Job saved and matched.")
    return RedirectResponse("/matches", status_code=303)


# ── Settings / email OAuth / privacy ───────────────────────────


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    return render(
        request,
        "dashboard/settings.html",
        bundle=get_profile_bundle(user["id"]),
        prefs=get_search_prefs(user["id"]),
        email_conn=email_oauth.get_connection(user["id"]),
        consents=privacy.latest_consent_map(user["id"]),
        purposes=privacy.CONSENT_PURPOSES,
        oauth_ready=email_oauth.oauth_configured(),
        payments=razorpay_billing.list_payments(user["id"]),
        companion=companion_svc.get_status(user["id"]),
        companion_devices=companion_svc.list_devices(user["id"]),
    )


@app.post("/settings/companion/revoke")
def settings_companion_revoke(
    request: Request,
    user: dict = Depends(require_user),
    device_id: str = Form(...),
) -> RedirectResponse:
    companion_svc.revoke_device(user["id"], device_id)
    flash(request, "Companion device revoked.")
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/email/connect")
def email_connect(request: Request, user: dict = Depends(require_user), send: int = 1, read: int = 0) -> RedirectResponse:
    if not email_oauth.oauth_configured():
        flash(request, "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        return RedirectResponse("/settings", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    url = email_oauth.build_auth_url(state=state, include_send=bool(send), include_read=bool(read))
    return RedirectResponse(url, status_code=303)


@app.get("/settings/email/callback")
def email_callback(request: Request, user: dict = Depends(require_user), code: str = "", state: str = "") -> RedirectResponse:
    if not code or state != request.session.get("oauth_state"):
        flash(request, "OAuth state mismatch.")
        return RedirectResponse("/settings", status_code=303)
    tokens = email_oauth.exchange_code(code)
    access = tokens["access_token"]
    refresh = tokens.get("refresh_token", "")
    info = email_oauth.fetch_userinfo(access)
    scopes = (tokens.get("scope") or "").split()
    email_oauth.save_connection(
        user["id"],
        email_address=info.get("email") or user["email"],
        access_token=access,
        refresh_token=refresh,
        scopes=scopes,
    )
    if scopes and any("gmail.send" in s for s in scopes):
        record_consent(user["id"], purpose="email_sending", granted=True)
    flash(request, "Gmail connected via OAuth.")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/email/disconnect")
def email_disconnect(request: Request, user: dict = Depends(require_user)) -> RedirectResponse:
    email_oauth.disconnect(user["id"])
    flash(request, "Gmail disconnected.")
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/export")
def export_data(request: Request, user: dict = Depends(require_user)) -> JSONResponse:
    return JSONResponse(privacy.export_user_data(user["id"]))


@app.post("/settings/delete")
def delete_account(request: Request, user: dict = Depends(require_user), confirm: str = Form("")) -> RedirectResponse:
    if confirm.strip().upper() != "DELETE":
        flash(request, "Type DELETE to confirm account deletion.")
        return RedirectResponse("/settings", status_code=303)
    destroy_session(request.cookies.get(SESSION_COOKIE))
    privacy.delete_user_account(user["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ── Billing ────────────────────────────────────────────────────


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request) -> HTMLResponse:
    user = _user(request)
    return render(
        request,
        "billing/plans.html",
        plans=razorpay_billing.catalog_for_display(),
        entitlement=active_plan(user["id"]) if user else None,
        payments=razorpay_billing.list_payments(user["id"]) if user else [],
        razorpay_ready=razorpay_billing.razorpay_configured(),
        allow_beta_grant=ALLOW_BETA_GRANT,
    )


@app.get("/billing/checkout")
def billing_checkout_get(
    request: Request, plan: str = "", user: dict = Depends(require_user)
) -> RedirectResponse:
    if plan not in PLANS or plan == "free":
        flash(request, "Choose a valid plan.")
        return RedirectResponse("/billing", status_code=303)
    try:
        order = qr_payments.start_checkout(user["id"], plan)
    except Exception as exc:
        flash(request, str(exc))
        return RedirectResponse("/billing", status_code=303)
    return RedirectResponse(f"/billing/pay/{order['payment_id']}", status_code=303)


@app.post("/billing/checkout")
def billing_checkout(request: Request, user: dict = Depends(require_user), plan_id: str = Form(...)) -> HTMLResponse:
    try:
        order = qr_payments.start_checkout(user["id"], plan_id)
    except Exception as exc:
        flash(request, str(exc))
        return RedirectResponse("/billing", status_code=303)
    payment = qr_payments.get_checkout(user["id"], order["payment_id"])
    return render(
        request,
        "billing/qr_payment.html",
        order=order,
        plan=PLANS[plan_id],
        payment=payment,
    )


@app.post("/billing/pay/{payment_id}/submit")
async def billing_qr_submit(
    request: Request,
    payment_id: str,
    user: dict = Depends(require_user),
    payer_name: str = Form(...),
    phone: str = Form(...),
    transaction_id: str = Form(...),
    screenshot: UploadFile = File(...),
) -> RedirectResponse:
    payment = qr_payments.get_checkout(user["id"], payment_id)
    if not payment:
        flash(request, "Payment session not found.")
        return RedirectResponse("/billing", status_code=303)
    content = await screenshot.read()
    if not content:
        flash(request, "Please upload a payment screenshot.")
        return RedirectResponse(f"/billing/pay/{payment_id}", status_code=303)
    try:
        path = qr_payments.save_screenshot(
            user["id"], payment_id, screenshot.filename or "screenshot.jpg", content
        )
        qr_payments.submit_payment(
            user["id"],
            payment_id,
            payer_name=payer_name,
            phone=phone,
            transaction_id=transaction_id,
            screenshot_path=path,
        )
        flash(request, "Payment proof submitted. We will verify and activate your pass shortly.")
    except qr_payments.DuplicateTransactionError:
        flash(request, "This transaction ID was already used. Check the ID or contact support.")
    except Exception as exc:
        flash(request, str(exc))
    return RedirectResponse(f"/billing/pay/{payment_id}", status_code=303)


@app.get("/billing/pay/{payment_id}", response_class=HTMLResponse)
def billing_qr_page(request: Request, payment_id: str, user: dict = Depends(require_user)) -> HTMLResponse:
    payment = qr_payments.get_checkout(user["id"], payment_id)
    if not payment:
        flash(request, "Payment session not found.")
        return RedirectResponse("/billing", status_code=303)
    plan_id = payment["plan_id"]
    breakdown = razorpay_billing.plan_price_breakdown(plan_id)
    order = {
        "payment_id": payment_id,
        "plan_id": plan_id,
        "plan_name": PLANS[plan_id]["name"],
        "amount": payment["amount_paise"],
        "breakdown": breakdown,
    }
    return render(
        request,
        "billing/qr_payment.html",
        order=order,
        plan=PLANS[plan_id],
        payment=payment,
    )


@app.post("/billing/verify")
def billing_verify(
    request: Request,
    user: dict = Depends(require_user),
    payment_id: str = Form(...),
    razorpay_order_id: str = Form(...),
    razorpay_payment_id: str = Form(...),
    razorpay_signature: str = Form(...),
) -> RedirectResponse:
    try:
        razorpay_billing.verify_and_activate(
            user_id=user["id"],
            payment_id=payment_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        flash(request, "Payment successful. Pass activated.")
    except Exception as exc:
        flash(request, str(exc))
        return RedirectResponse("/billing", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/billing/webhook")
async def billing_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        result = razorpay_billing.handle_webhook(body, signature)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result)


@app.post("/billing/beta-grant")
def billing_beta_grant(request: Request, user: dict = Depends(require_user)) -> RedirectResponse:
    """Dev/beta helper — grant 30-day Pro without payment. Disabled unless SAAS_ALLOW_BETA_GRANT=1."""
    if not ALLOW_BETA_GRANT:
        raise HTTPException(status_code=403, detail="Beta grant is disabled")
    grant_beta_pass(user["id"], days=30)
    flash(request, "Beta Pro pass granted for 30 days.")
    return RedirectResponse("/billing", status_code=303)


# ── Admin panel ────────────────────────────────────────────────


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request) -> HTMLResponse:
    email = admin_auth.admin_from_session(request)
    if not email:
        return RedirectResponse("/admin/login", status_code=303)
    return render_admin(
        request,
        "admin/dashboard.html",
        stats=qr_payments.revenue_stats(),
        pending=qr_payments.list_submissions(status="pending"),
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request) -> HTMLResponse:
    if admin_auth.admin_from_session(request):
        return RedirectResponse("/admin", status_code=303)
    return render_admin(request, "admin/login.html", oauth_ready=admin_auth.admin_oauth_configured())


@app.get("/admin/login/google")
def admin_login_google(request: Request) -> RedirectResponse:
    if not admin_auth.admin_oauth_configured():
        admin_flash(request, "Google OAuth is not configured.")
        return RedirectResponse("/admin/login", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["admin_oauth_state"] = state
    url = admin_auth.build_admin_auth_url(state=state)
    return RedirectResponse(url, status_code=303)


@app.get("/admin/callback")
def admin_oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not code or state != request.session.get("admin_oauth_state"):
        admin_flash(request, "OAuth state mismatch.")
        return RedirectResponse("/admin/login", status_code=303)
    try:
        admin_auth.login_admin(request, code=code)
    except PermissionError as exc:
        admin_flash(request, str(exc))
        return RedirectResponse("/admin/login", status_code=303)
    except Exception:
        admin_flash(request, "Google sign-in failed.")
        return RedirectResponse("/admin/login", status_code=303)
    admin_flash(request, "Signed in.")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/logout")
def admin_logout(request: Request) -> RedirectResponse:
    admin_auth.logout_admin(request)
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin/payments", response_class=HTMLResponse)
def admin_payments_list(request: Request, admin_email: str = Depends(require_admin)) -> HTMLResponse:
    return render_admin(
        request,
        "admin/payments.html",
        submissions=qr_payments.list_submissions(),
    )


@app.get("/admin/payments/{submission_id}", response_class=HTMLResponse)
def admin_payment_detail(
    request: Request, submission_id: str, admin_email: str = Depends(require_admin)
) -> HTMLResponse:
    sub = qr_payments.get_submission(submission_id)
    if not sub:
        raise HTTPException(404)
    return render_admin(
        request,
        "admin/payment_detail.html",
        sub=sub,
        screenshot_url=f"/admin/payments/{submission_id}/screenshot",
    )


@app.get("/admin/payments/{submission_id}/screenshot")
def admin_payment_screenshot(
    submission_id: str, admin_email: str = Depends(require_admin)
) -> FileResponse:
    path = qr_payments.screenshot_file(submission_id)
    if not path:
        raise HTTPException(404)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg"
    return FileResponse(path, media_type=media)


@app.post("/admin/payments/{submission_id}/approve")
def admin_payment_approve(
    request: Request,
    submission_id: str,
    admin_email: str = Depends(require_admin),
    notes: str = Form(""),
) -> RedirectResponse:
    try:
        result = qr_payments.approve_submission(submission_id, admin_email=admin_email, notes=notes)
        if result and result.get("email_sent"):
            admin_flash(request, "Payment approved and plan activated. Approval email sent.")
        else:
            admin_flash(request, "Payment approved and plan activated. Approval email could not be sent.")
    except Exception as exc:
        admin_flash(request, str(exc))
    return RedirectResponse(f"/admin/payments/{submission_id}", status_code=303)


@app.post("/admin/payments/{submission_id}/reject")
def admin_payment_reject(
    request: Request,
    submission_id: str,
    admin_email: str = Depends(require_admin),
    notes: str = Form(""),
) -> RedirectResponse:
    try:
        qr_payments.reject_submission(submission_id, admin_email=admin_email, notes=notes)
        admin_flash(request, "Payment rejected.")
    except Exception as exc:
        admin_flash(request, str(exc))
    return RedirectResponse(f"/admin/payments/{submission_id}", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_list(request: Request, admin_email: str = Depends(require_admin)) -> HTMLResponse:
    return render_admin(request, "admin/users.html", users=qr_payments.list_users())


# ── Health ─────────────────────────────────────────────────────


# ── Companion API (Electron / local agent) ─────────────────────


@app.post("/api/companion/login")
def api_companion_login(body: CompanionLoginBody) -> JSONResponse:
    try:
        result = companion_svc.issue_companion_token(
            email=body.email,
            password=body.password,
            device_id=body.device_id,
            device_name=body.device_name,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/companion/me")
def api_companion_me(user: dict = Depends(require_companion_user)) -> JSONResponse:
    plan = active_plan(user["id"])
    status = companion_svc.get_status(user["id"]) or {}
    prefs = get_search_prefs(user["id"])
    return JSONResponse(
        {
            "user": {"id": user["id"], "email": user["email"], "full_name": user["full_name"]},
            "device_id": user["device_id"],
            "plan": plan,
            "status": status,
            "prefs": prefs,
            "dashboard_url": f"{BASE_URL}/dashboard",
        }
    )


@app.post("/api/companion/posts")
def api_companion_posts(
    body: CompanionPostsBody,
    user: dict = Depends(require_companion_user),
) -> JSONResponse:
    if not body.posts:
        raise HTTPException(status_code=400, detail="No posts provided")
    result = companion_svc.ingest_companion_posts(user["id"], body.posts)
    if result["accepted"] == 0 and result.get("blocked_reason"):
        raise HTTPException(status_code=402, detail=result["blocked_reason"])
    return JSONResponse(result)


@app.post("/api/companion/status")
def api_companion_status(
    body: CompanionStatusBody,
    user: dict = Depends(require_companion_user),
) -> JSONResponse:
    status = companion_svc.update_status(
        user["id"],
        device_id=user.get("device_id") or "",
        device_name=body.device_name or user.get("device_name") or "",
        linkedin_connected=body.linkedin_connected,
        last_error=body.last_error,
    )
    return JSONResponse(status)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": APP_NAME, "base_url": BASE_URL}


# FastAPI dependency workaround: redirect-style auth
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def _redirect_auth(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return await http_exception_handler(request, exc)
