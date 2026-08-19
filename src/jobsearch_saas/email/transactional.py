"""Product emails sent from the LetItApply mailbox (payment approved, etc.)."""

from __future__ import annotations

import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from jobsearch_saas.config import (
    APP_NAME,
    BASE_URL,
    MAIL_FROM,
    MAIL_FROM_NAME,
    PLANS,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    SUPPORT_EMAIL,
)
from jobsearch_saas.download_gate import download_url_for_user

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(SMTP_USERNAME and SMTP_PASSWORD and MAIL_FROM)


def send_payment_approved_email(submission: dict[str, Any] | None) -> bool:
    if not submission:
        return False
    to_email = (submission.get("user_email") or "").strip()
    if not to_email:
        log.warning("Payment approved email skipped: no user email")
        return False
    if not smtp_configured():
        log.warning("Payment approved email skipped: SMTP is not configured")
        return False

    plan_id = submission.get("plan_id") or ""
    plan = PLANS.get(plan_id, {})
    plan_name = str(plan.get("name") or plan_id or "paid pass")
    amount_paise = int(submission.get("amount_paise") or 0)
    amount_display = f"₹{amount_paise / 100:.0f}"
    days = int(plan.get("days") or 0)
    first_name = (submission.get("user_full_name") or "").strip().split(" ")[0] or "there"
    download_url = download_url_for_user(str(submission["user_id"]))
    dashboard_url = f"{BASE_URL.rstrip('/')}/dashboard"

    subject = f"Payment approved — your {plan_name} pass is active"
    text_body = _plain_body(
        first_name=first_name,
        plan_name=plan_name,
        amount_display=amount_display,
        days=days,
        download_url=download_url,
        dashboard_url=dashboard_url,
    )
    html_body = _html_body(
        first_name=first_name,
        plan_name=plan_name,
        amount_display=amount_display,
        days=days,
        download_url=download_url,
        dashboard_url=dashboard_url,
    )
    _send_html_email(to_email, subject, text_body, html_body)
    return True


def _send_html_email(to_email: str, subject: str, text_body: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((MAIL_FROM_NAME or APP_NAME, MAIL_FROM))
    msg["To"] = to_email
    msg["Subject"] = subject
    if SUPPORT_EMAIL:
        msg["Reply-To"] = SUPPORT_EMAIL
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(MAIL_FROM, [to_email], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"Mail login failed for {MAIL_FROM}. Use a Google App Password, not the account password."
        ) from exc


def _plain_body(
    *,
    first_name: str,
    plan_name: str,
    amount_display: str,
    days: int,
    download_url: str,
    dashboard_url: str,
) -> str:
    duration = f"{days}-day " if days else ""
    return (
        f"Hi {first_name},\n\n"
        f"Your LetItApply payment is approved. Your {duration}{plan_name} pass ({amount_display}) is now active.\n\n"
        f"Download Companion (sign in with this same account — the link will not work for anyone else):\n"
        f"{download_url}\n\n"
        f"Dashboard: {dashboard_url}\n\n"
        f"Questions? {SUPPORT_EMAIL}\n\n"
        f"— {APP_NAME}\n"
    )


def _html_body(
    *,
    first_name: str,
    plan_name: str,
    amount_display: str,
    days: int,
    download_url: str,
    dashboard_url: str,
) -> str:
    safe_name = html.escape(first_name)
    safe_plan = html.escape(plan_name)
    safe_amount = html.escape(amount_display)
    safe_download = html.escape(download_url, quote=True)
    safe_dashboard = html.escape(dashboard_url, quote=True)
    safe_support = html.escape(SUPPORT_EMAIL)
    duration = f"{days}-day " if days else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Payment approved</title>
</head>
<body style="margin:0;padding:0;background:#f4f7fb;color:#0b1220;font-family:'DM Sans',Helvetica,Arial,sans-serif;line-height:1.55;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fb;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:14px;border:1px solid rgba(15,23,42,0.08);overflow:hidden;">
          <tr>
            <td style="padding:22px 32px;border-bottom:1px solid rgba(15,23,42,0.08);">
              <span style="font-family:Syne,Helvetica,Arial,sans-serif;font-weight:800;font-size:22px;letter-spacing:-0.04em;color:#0b1220;">Let<span style="color:#2563eb;">It</span>Apply</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 8px;color:#2563eb;font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">Payment approved</p>
              <h1 style="font-family:Syne,Helvetica,Arial,sans-serif;font-size:28px;line-height:1.15;letter-spacing:-0.035em;margin:0 0 12px;color:#0b1220;">You're in</h1>
              <p style="margin:0 0 16px;color:#64748b;font-size:16px;">
                Hi {safe_name}, your payment for the <strong style="color:#0b1220;">{duration}{safe_plan}</strong> pass
                ({safe_amount}) is approved. Your plan is active — you can download Companion the usual way.
              </p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px;width:100%;background:#ecfdf5;border:1px solid #6ee7b7;border-radius:12px;">
                <tr>
                  <td style="padding:14px 16px;color:#0f766e;font-size:14px;font-weight:600;">
                    Payment verified · pass activated
                  </td>
                </tr>
              </table>
              <p style="text-align:center;margin:0 0 12px;">
                <a href="{safe_download}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:999px;font-weight:600;font-size:15px;">Download Companion</a>
              </p>
              <p style="text-align:center;margin:0 0 20px;">
                <a href="{safe_dashboard}" style="color:#1d4ed8;font-size:14px;text-decoration:none;">Open your dashboard</a>
              </p>
              <p style="margin:0;color:#64748b;font-size:13px;">
                This download only works when you are signed in to <strong>this</strong> LetItApply account with an approved payment.
                Forwarding the link will not give anyone else access.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 32px;background:#f8fafc;border-top:1px solid rgba(15,23,42,0.08);color:#64748b;font-size:12px;">
              Questions? {safe_support}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
