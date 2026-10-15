"""Alert rule and trigger endpoints."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alert.engine import AlertEngine
from app.alert.rules import RULE_TYPES, describe_rule, normalize_params
from app.api.deps import db_session
from app.core.errors import DataError
from app.core.logging import get_logger
from app.db.models import AlertRule, AlertTrigger
from app.schemas import (
    AlertRuleIn,
    AlertRuleOut,
    AlertTriggerOut,
    RuleTypeInfo,
    TriggerStatusUpdate,
)

log = get_logger(__name__)
router = APIRouter(tags=["alerts"])


def _rule_out(row: AlertRule) -> AlertRuleOut:
    return AlertRuleOut(
        id=row.id,
        name=row.name,
        rule_type=row.rule_type,
        params=json.loads(row.params_json or "{}"),
        scope_type=row.scope_type,
        scope_value=row.scope_value,
        cooldown_seconds=row.cooldown_seconds,
        once_per_day=row.once_per_day,
        exclude_st=row.exclude_st,
        min_amount=row.min_amount,
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _trigger_out(row: AlertTrigger) -> AlertTriggerOut:
    return AlertTriggerOut(
        id=row.id,
        rule_id=row.rule_id,
        code=row.code,
        name=row.name,
        trade_date=row.trade_date,
        trigger_at=row.trigger_at,
        rule_type=row.rule_type,
        message=row.message,
        snapshot=json.loads(row.snapshot_json or "{}"),
        status=row.status,
    )


@router.get("/alerts/types", response_model=list[RuleTypeInfo])
def list_rule_types():
    """Available rule types and their parameter schemas.

    Exposed so the frontend can build a rule editor from the backend contract
    instead of hard-coding a second, drifting copy of it.
    """
    return [
        RuleTypeInfo(rule_type=name, description=spec["description"], params=spec["params"])
        for name, spec in RULE_TYPES.items()
    ]


@router.get("/alerts/rules", response_model=list[AlertRuleOut])
def list_rules(session: Session = Depends(db_session)):
    """All configured alert rules."""
    rows = session.execute(select(AlertRule).order_by(AlertRule.id)).scalars()
    return [_rule_out(row) for row in rows]


@router.post("/alerts/rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(payload: AlertRuleIn, session: Session = Depends(db_session)):
    """Create an alert rule (parameters are validated against the rule schema)."""
    try:
        normalized = normalize_params(payload.rule_type, payload.params)
    except DataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    rule = AlertRule(
        name=payload.name,
        rule_type=payload.rule_type,
        params_json=json.dumps(normalized),
        scope_type=payload.scope_type,
        scope_value=payload.scope_value,
        cooldown_seconds=payload.cooldown_seconds,
        once_per_day=payload.once_per_day,
        exclude_st=payload.exclude_st,
        min_amount=payload.min_amount,
        enabled=payload.enabled,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return _rule_out(rule)


@router.patch("/alerts/rules/{rule_id}", response_model=AlertRuleOut)
def update_rule(rule_id: int, payload: AlertRuleIn, session: Session = Depends(db_session)):
    """Replace a rule's definition."""
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    try:
        normalized = normalize_params(payload.rule_type, payload.params)
    except DataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    rule.name = payload.name
    rule.rule_type = payload.rule_type
    rule.params_json = json.dumps(normalized)
    rule.scope_type = payload.scope_type
    rule.scope_value = payload.scope_value
    rule.cooldown_seconds = payload.cooldown_seconds
    rule.once_per_day = payload.once_per_day
    rule.exclude_st = payload.exclude_st
    rule.min_amount = payload.min_amount
    rule.enabled = payload.enabled
    session.commit()
    session.refresh(rule)
    return _rule_out(rule)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, session: Session = Depends(db_session)):
    """Delete a rule and its triggers."""
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    session.delete(rule)
    session.commit()
    return None


@router.get("/alerts/triggers", response_model=list[AlertTriggerOut])
def list_triggers(
    status_filter: str | None = Query(default=None, alias="status"),
    code: str | None = None,
    trade_date: date | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(db_session),
):
    """Trigger history, newest first."""
    stmt = select(AlertTrigger).order_by(AlertTrigger.trigger_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(AlertTrigger.status == status_filter)
    if code:
        stmt = stmt.where(AlertTrigger.code == code)
    if trade_date:
        stmt = stmt.where(AlertTrigger.trade_date == trade_date)
    return [_trigger_out(row) for row in session.execute(stmt).scalars()]


@router.patch("/alerts/triggers/{trigger_id}", response_model=AlertTriggerOut)
def update_trigger(
    trigger_id: int, payload: TriggerStatusUpdate, session: Session = Depends(db_session)
):
    """Mark a trigger read or muted."""
    row = session.get(AlertTrigger, trigger_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trigger {trigger_id} not found",
        )
    row.status = payload.status
    session.commit()
    session.refresh(row)
    return _trigger_out(row)


@router.post("/alerts/scan")
def scan_now():
    """Evaluate all rules immediately.

    Safe to call repeatedly: triggers are de-duplicated by
    ``(rule_id, dedup_key)``, so the response reports only genuinely new alerts.
    """
    candidates = AlertEngine().evaluate_all()
    return {
        "triggered": len(candidates),
        "items": [
            {
                "rule_id": c.rule_id,
                "code": c.code,
                "name": c.name,
                "trade_date": c.trade_date,
                "rule_type": c.rule_type,
                "message": c.message,
                "snapshot": c.snapshot,
            }
            for c in candidates
        ],
    }


@router.post("/alerts/preview")
def preview_rule(payload: AlertRuleIn, session: Session = Depends(db_session)):
    """Evaluate a rule definition without saving it (used by the rule editor)."""
    try:
        normalized = normalize_params(payload.rule_type, payload.params)
    except DataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    engine = AlertEngine()
    probe = AlertRule(
        id=-1,
        name=payload.name,
        rule_type=payload.rule_type,
        params_json=json.dumps(normalized),
        scope_type=payload.scope_type,
        scope_value=payload.scope_value,
        cooldown_seconds=payload.cooldown_seconds,
        once_per_day=payload.once_per_day,
        exclude_st=payload.exclude_st,
        min_amount=payload.min_amount,
        enabled=True,
    )

    from app.alert.engine import evaluate_rule

    codes = engine._resolve_scope(session, probe)
    matches = []
    for code in codes[:200]:
        ctx = engine._build_context(session, code)
        if ctx is None:
            continue
        if payload.exclude_st and ctx.is_st:
            continue
        if payload.min_amount and (ctx.amount or 0) < payload.min_amount:
            continue
        fired, message, snapshot = evaluate_rule(ctx, payload.rule_type, normalized)
        if fired:
            matches.append(
                {"code": code, "name": ctx.name, "message": message, "snapshot": snapshot}
            )
        if len(matches) >= 50:
            break

    return {
        "rule": describe_rule(payload.rule_type, normalized),
        "matched": len(matches),
        "scope_size": len(codes),
        "items": matches,
    }
