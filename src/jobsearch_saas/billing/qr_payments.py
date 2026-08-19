"""Manual UPI/QR payment submissions with admin approval."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from jobsearch_saas import db
from jobsearch_saas.billing.razorpay_billing import plan_price_breakdown
from jobsearch_saas.config import PLANS, UPLOAD_DIR
from jobsearch_saas.email.transactional import send_payment_approved_email
from jobsearch_saas.entitlements import activate_plan

log = logging.getLogger(__name__)


class DuplicateTransactionError(Exception):
    pass


def _normalize_transaction_id(transaction_id: str) -> str:
    return re.sub(r"\s+", "", transaction_id.strip().upper())


def start_checkout(user_id: str, plan_id: str) -> dict[str, Any]:
    if plan_id not in PLANS or plan_id == "free":
        raise ValueError("Choose a paid pass")
    breakdown = plan_price_breakdown(plan_id)
    payment_id = str(uuid.uuid4())
    now = db.utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO payments (
                id, user_id, plan_id, razorpay_order_id, amount_paise, gst_paise,
                currency, status, invoice_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'INR', 'pending_qr', ?, ?)
            """,
            (
                payment_id,
                user_id,
                plan_id,
                f"qr_{payment_id[:12]}",
                breakdown["total_paise"],
                breakdown["gst_paise"],
                db.dumps(
                    {
                        "plan_id": plan_id,
                        "plan_name": PLANS[plan_id]["name"],
                        "days": PLANS[plan_id]["days"],
                        "method": "qr_upi",
                        **breakdown,
                    }
                ),
                now,
            ),
        )
        db.audit(
            conn,
            user_id=user_id,
            action="billing.qr_checkout_started",
            entity_type="payment",
            entity_id=payment_id,
            detail={"plan_id": plan_id},
        )
    return {
        "payment_id": payment_id,
        "plan_id": plan_id,
        "plan_name": PLANS[plan_id]["name"],
        "plan": PLANS[plan_id],
        "amount": breakdown["total_paise"],
        "breakdown": breakdown,
    }


def get_checkout(user_id: str, payment_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE id = ? AND user_id = ?",
            (payment_id, user_id),
        ).fetchone()
        if not row:
            return None
        payment = dict(row)
        sub = conn.execute(
            "SELECT * FROM qr_payment_submissions WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        payment["submission"] = dict(sub) if sub else None
        return payment


def save_screenshot(user_id: str, payment_id: str, filename: str, content: bytes) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "screenshot.jpg"
    folder = UPLOAD_DIR / "payment_screenshots" / user_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{payment_id}_{safe}"
    path.write_bytes(content)
    return str(path)


def submit_payment(
    user_id: str,
    payment_id: str,
    *,
    payer_name: str,
    phone: str,
    transaction_id: str,
    screenshot_path: str,
) -> dict[str, Any]:
    txn = _normalize_transaction_id(transaction_id)
    if len(txn) < 4:
        raise ValueError("Enter a valid transaction ID")
    if not payer_name.strip():
        raise ValueError("Name is required")
    if not phone.strip():
        raise ValueError("Phone number is required")

    with db.connect() as conn:
        payment_row = conn.execute(
            "SELECT * FROM payments WHERE id = ? AND user_id = ?",
            (payment_id, user_id),
        ).fetchone()
        if not payment_row:
            raise RuntimeError("Payment not found")
        payment = dict(payment_row)
        if payment["status"] == "paid":
            raise RuntimeError("This payment is already approved")
        existing_sub = conn.execute(
            "SELECT * FROM qr_payment_submissions WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        if existing_sub and existing_sub["status"] == "pending":
            raise RuntimeError("Payment proof already submitted — awaiting admin approval")
        if existing_sub and existing_sub["status"] == "approved":
            raise RuntimeError("This payment is already approved")

        dup = conn.execute(
            "SELECT id FROM qr_payment_submissions WHERE transaction_id = ?",
            (txn,),
        ).fetchone()
        if dup:
            raise DuplicateTransactionError("This transaction ID was already used")

        submission_id = str(uuid.uuid4())
        now = db.utc_now()
        try:
            conn.execute(
                """
                INSERT INTO qr_payment_submissions (
                    id, user_id, payment_id, plan_id, payer_name, phone,
                    transaction_id, screenshot_path, amount_paise, gst_paise,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    submission_id,
                    user_id,
                    payment_id,
                    payment["plan_id"],
                    payer_name.strip(),
                    phone.strip(),
                    txn,
                    screenshot_path,
                    payment["amount_paise"],
                    payment["gst_paise"],
                    now,
                ),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                raise DuplicateTransactionError("This transaction ID was already used") from exc
            raise

        conn.execute(
            "UPDATE payments SET status = 'pending_review' WHERE id = ?",
            (payment_id,),
        )
        db.audit(
            conn,
            user_id=user_id,
            action="billing.qr_submitted",
            entity_type="qr_payment_submission",
            entity_id=submission_id,
            detail={"payment_id": payment_id, "transaction_id": txn},
        )

    return get_submission(submission_id)  # type: ignore[return-value]


def get_submission(submission_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, u.email AS user_email, u.full_name AS user_full_name
            FROM qr_payment_submissions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (submission_id,),
        ).fetchone()
        return dict(row) if row else None


def list_user_transactions(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """All payment attempts for a user — reflects live admin approval status."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS payment_id,
                p.plan_id,
                p.amount_paise,
                p.gst_paise,
                p.status AS payment_status,
                p.created_at AS initiated_at,
                p.paid_at,
                s.id AS submission_id,
                s.transaction_id,
                s.status AS submission_status,
                s.reviewed_at,
                s.reviewed_by,
                s.admin_notes,
                s.created_at AS submitted_at
            FROM payments p
            LEFT JOIN qr_payment_submissions s ON s.payment_id = p.id
            WHERE p.user_id = ?
            ORDER BY COALESCE(s.created_at, p.created_at) DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        plan = PLANS.get(r["plan_id"], {})
        status_key, status_label = _user_facing_status(r)
        items.append(
            {
                "payment_id": r["payment_id"],
                "submission_id": r.get("submission_id"),
                "plan_id": r["plan_id"],
                "plan_name": plan.get("name", r["plan_id"]),
                "amount_paise": int(r["amount_paise"]),
                "amount_display": f"₹{int(r['amount_paise']) / 100:.0f}",
                "transaction_id": r.get("transaction_id") or "—",
                "status": status_key,
                "status_label": status_label,
                "initiated_at": r["initiated_at"],
                "submitted_at": r.get("submitted_at"),
                "reviewed_at": r.get("reviewed_at"),
                "admin_notes": (r.get("admin_notes") or "").strip(),
                "pay_url": f"/billing/pay/{r['payment_id']}"
                if status_key in ("awaiting_payment", "awaiting_proof")
                else None,
            }
        )
    return items


def _user_facing_status(row: dict[str, Any]) -> tuple[str, str]:
    sub_status = row.get("submission_status")
    pay_status = row.get("payment_status") or ""
    if sub_status == "approved" or pay_status == "paid":
        return "approved", "Approved"
    if sub_status == "rejected" or pay_status == "rejected":
        return "rejected", "Rejected"
    if sub_status == "pending" or pay_status == "pending_review":
        return "pending", "Pending admin review"
    if pay_status == "pending_qr":
        return "awaiting_payment", "Awaiting payment"
    if pay_status == "created":
        return "awaiting_payment", "Checkout started"
    return pay_status or "unknown", (pay_status or "Unknown").replace("_", " ").title()


def list_submissions(*, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with db.connect() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT s.*, u.email AS user_email, u.full_name AS user_full_name
                FROM qr_payment_submissions s
                JOIN users u ON u.id = s.user_id
                WHERE s.status = ?
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.*, u.email AS user_email, u.full_name AS user_full_name
                FROM qr_payment_submissions s
                JOIN users u ON u.id = s.user_id
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def approve_submission(submission_id: str, *, admin_email: str, notes: str = "") -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM qr_payment_submissions WHERE id = ?",
            (submission_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("Submission not found")
        sub = dict(row)
        if sub["status"] == "approved":
            raise RuntimeError("Already approved")
        if sub["status"] == "rejected":
            raise RuntimeError("Cannot approve a rejected submission")

        now = db.utc_now()
        conn.execute(
            """
            UPDATE qr_payment_submissions
            SET status = 'approved', reviewed_by = ?, reviewed_at = ?, admin_notes = ?
            WHERE id = ?
            """,
            (admin_email, now, notes.strip(), submission_id),
        )
        conn.execute(
            """
            UPDATE payments
            SET status = 'paid', razorpay_payment_id = ?, paid_at = ?
            WHERE id = ?
            """,
            (sub["transaction_id"], now, sub["payment_id"]),
        )
        db.audit(
            conn,
            user_id=sub["user_id"],
            action="billing.qr_approved",
            entity_type="qr_payment_submission",
            entity_id=submission_id,
            detail={"admin": admin_email, "transaction_id": sub["transaction_id"]},
        )

    activate_plan(sub["user_id"], sub["plan_id"])
    approved = get_submission(submission_id)
    email_sent = False
    if approved:
        try:
            email_sent = bool(send_payment_approved_email(approved))
        except Exception as exc:
            log.warning("Payment approval email failed for %s: %s", approved.get("user_email"), exc)
            email_sent = False
        approved["email_sent"] = email_sent
    return approved  # type: ignore[return-value]


def reject_submission(submission_id: str, *, admin_email: str, notes: str = "") -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM qr_payment_submissions WHERE id = ?",
            (submission_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("Submission not found")
        sub = dict(row)
        if sub["status"] != "pending":
            raise RuntimeError("Only pending submissions can be rejected")

        now = db.utc_now()
        conn.execute(
            """
            UPDATE qr_payment_submissions
            SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, admin_notes = ?
            WHERE id = ?
            """,
            (admin_email, now, notes.strip(), submission_id),
        )
        conn.execute(
            "UPDATE payments SET status = 'rejected' WHERE id = ?",
            (sub["payment_id"],),
        )
        db.audit(
            conn,
            user_id=sub["user_id"],
            action="billing.qr_rejected",
            entity_type="qr_payment_submission",
            entity_id=submission_id,
            detail={"admin": admin_email},
        )
    return get_submission(submission_id)  # type: ignore[return-value]


def revenue_stats() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT amount_paise, reviewed_at, created_at
            FROM qr_payment_submissions
            WHERE status = 'approved'
            ORDER BY reviewed_at ASC
            """
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM qr_payment_submissions WHERE status = 'pending'"
        ).fetchone()["c"]
        approved = conn.execute(
            "SELECT COUNT(*) AS c FROM qr_payment_submissions WHERE status = 'approved'"
        ).fetchone()["c"]
        rejected = conn.execute(
            "SELECT COUNT(*) AS c FROM qr_payment_submissions WHERE status = 'rejected'"
        ).fetchone()["c"]
        total_users = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE deleted_at IS NULL"
        ).fetchone()["c"]

    total_paise = sum(int(r["amount_paise"]) for r in rows)
    by_day: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for r in rows:
        ts = (r["reviewed_at"] or r["created_at"] or "")[:10]
        if not ts:
            continue
        by_day[ts] = by_day.get(ts, 0) + int(r["amount_paise"])
        month = ts[:7]
        by_month[month] = by_month.get(month, 0) + int(r["amount_paise"])

    return {
        "total_revenue_paise": total_paise,
        "pending_count": int(pending),
        "approved_count": int(approved),
        "rejected_count": int(rejected),
        "total_users": int(total_users),
        "revenue_by_day": sorted(by_day.items(), reverse=True),
        "revenue_by_month": sorted(by_month.items(), reverse=True),
    }


def list_users(limit: int = 500) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.email, u.full_name, u.phone, u.created_at,
                   e.plan_id, e.valid_until
            FROM users u
            LEFT JOIN entitlements e ON e.user_id = u.id
            WHERE u.deleted_at IS NULL
            ORDER BY u.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def screenshot_file(submission_id: str) -> Path | None:
    sub = get_submission(submission_id)
    if not sub:
        return None
    path = Path(sub["screenshot_path"])
    return path if path.is_file() else None
