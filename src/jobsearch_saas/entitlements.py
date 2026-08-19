"""Entitlements, quotas, and plan activation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jobsearch_saas import db
from jobsearch_saas.auth import month_key, week_key
from jobsearch_saas.config import PLANS


def get_entitlement(user_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM entitlements WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            now = db.utc_now()
            conn.execute(
                """
                INSERT INTO entitlements (user_id, plan_id, updated_at, week_key, month_key)
                VALUES (?, 'free', ?, ?, ?)
                """,
                (user_id, now, week_key(), month_key()),
            )
            row = conn.execute(
                "SELECT * FROM entitlements WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        ent = dict(row)
        _rollover(conn, ent)
        return dict(
            conn.execute(
                "SELECT * FROM entitlements WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        )


def _rollover(conn: Any, ent: dict[str, Any]) -> None:
    wk, mk = week_key(), month_key()
    if ent.get("week_key") != wk:
        conn.execute(
            """
            UPDATE entitlements
            SET matches_used_week = 0, companion_uploads_used_week = 0,
                week_key = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (wk, db.utc_now(), ent["user_id"]),
        )
    if ent.get("month_key") != mk:
        conn.execute(
            "UPDATE entitlements SET applications_used_month = 0, month_key = ?, updated_at = ? WHERE user_id = ?",
            (mk, db.utc_now(), ent["user_id"]),
        )
    valid_until = ent.get("valid_until")
    if valid_until and datetime.fromisoformat(valid_until) < datetime.now(timezone.utc):
        if ent.get("plan_id") != "free":
            conn.execute(
                "UPDATE entitlements SET plan_id = 'free', valid_until = NULL, updated_at = ? WHERE user_id = ?",
                (db.utc_now(), ent["user_id"]),
            )


def _quota_remaining(cap: int | None, used: int) -> int | None:
    if cap is None:
        return None
    return max(0, cap - used)


def active_plan(user_id: str) -> dict[str, Any]:
    ent = get_entitlement(user_id)
    plan = dict(PLANS.get(ent["plan_id"], PLANS["free"]))
    plan["plan_id"] = ent["plan_id"]
    plan["valid_until"] = ent.get("valid_until")
    plan["applications_used_month"] = ent["applications_used_month"]
    plan["matches_used_week"] = ent["matches_used_week"]
    plan["companion_uploads_used_week"] = ent.get("companion_uploads_used_week") or 0
    plan["applications_unlimited"] = plan.get("applications_per_month") is None
    plan["matches_unlimited"] = plan.get("matches_per_week") is None
    plan["applications_remaining"] = _quota_remaining(
        plan.get("applications_per_month"), ent["applications_used_month"]
    )
    plan["matches_remaining"] = _quota_remaining(plan.get("matches_per_week"), ent["matches_used_week"])
    cap = int(plan.get("companion_uploads_per_week") or 0)
    used = int(plan["companion_uploads_used_week"])
    plan["companion_uploads_remaining"] = max(0, cap - used)
    return plan


def has_approved_access(user_id: str) -> bool:
    """True when the user has an active paid pass (admin-approved payment or equivalent)."""
    plan = active_plan(user_id)
    plan_id = str(plan.get("plan_id") or "free")
    return plan_id != "free" and plan_id in PLANS


def can_show_match(user_id: str) -> tuple[bool, str]:
    plan = active_plan(user_id)
    if plan.get("matches_unlimited"):
        return True, ""
    if (plan.get("matches_remaining") or 0) <= 0:
        return False, "Weekly match quota reached. Upgrade your pass."
    return True, ""


def can_create_draft(user_id: str) -> tuple[bool, str]:
    plan = active_plan(user_id)
    if plan.get("applications_unlimited"):
        return True, ""
    if (plan.get("applications_remaining") or 0) <= 0:
        return False, "Monthly application/draft quota reached. Upgrade or wait for reset."
    return True, ""


def can_companion_upload(user_id: str) -> tuple[bool, str]:
    plan = active_plan(user_id)
    if plan["companion_uploads_remaining"] <= 0:
        return False, "Weekly companion upload quota reached. Upgrade your pass."
    return True, ""


def consume_match(user_id: str) -> None:
    with db.connect() as conn:
        get_entitlement(user_id)
        conn.execute(
            """
            UPDATE entitlements
            SET matches_used_week = matches_used_week + 1, updated_at = ?
            WHERE user_id = ?
            """,
            (db.utc_now(), user_id),
        )


def consume_application(user_id: str) -> None:
    with db.connect() as conn:
        get_entitlement(user_id)
        conn.execute(
            """
            UPDATE entitlements
            SET applications_used_month = applications_used_month + 1, updated_at = ?
            WHERE user_id = ?
            """,
            (db.utc_now(), user_id),
        )


def consume_companion_upload(user_id: str) -> None:
    with db.connect() as conn:
        get_entitlement(user_id)
        conn.execute(
            """
            UPDATE entitlements
            SET companion_uploads_used_week = companion_uploads_used_week + 1, updated_at = ?
            WHERE user_id = ?
            """,
            (db.utc_now(), user_id),
        )


def activate_plan(user_id: str, plan_id: str) -> dict[str, Any]:
    if plan_id not in PLANS or plan_id == "free":
        raise ValueError("Invalid paid plan")
    days = int(PLANS[plan_id]["days"])
    valid_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE entitlements
            SET plan_id = ?, valid_until = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (plan_id, valid_until, db.utc_now(), user_id),
        )
        db.audit(
            conn,
            user_id=user_id,
            action="billing.activate_plan",
            entity_type="entitlement",
            entity_id=user_id,
            detail={"plan_id": plan_id, "valid_until": valid_until},
        )
    return active_plan(user_id)


def grant_beta_pass(user_id: str, days: int = 30) -> dict[str, Any]:
    """Internal: activate Pro for private beta without payment."""
    valid_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE entitlements
            SET plan_id = 'pass_249', valid_until = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (valid_until, db.utc_now(), user_id),
        )
        db.audit(
            conn,
            user_id=user_id,
            action="billing.beta_grant",
            entity_type="entitlement",
            entity_id=user_id,
            detail={"days": days},
        )
    return active_plan(user_id)
