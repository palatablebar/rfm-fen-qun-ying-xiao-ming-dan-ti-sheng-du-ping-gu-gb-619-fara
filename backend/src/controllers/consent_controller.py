"""授权事件控制器：导入落审计，查询走 service。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, consent_service


def import_events(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    counters = consent_service.import_events(
        db,
        batch_no=payload["batch_no"],
        events=payload["events"],
        operator_email=principal["email"],
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="consent.import",
        entity="consent",
        entity_id=None,
        detail={
            "batch_no": counters["batch_no"],
            "total": counters["total"],
            "applied": counters["applied"],
            "late_ignored": counters["late_ignored"],
            "duplicates": counters["duplicates"],
            "unknown_members": counters["unknown_members"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return counters


def summary(db: Session) -> dict:
    return consent_service.summary(db)


def list_events(db: Session, **filters) -> dict:
    return consent_service.list_events(db, **filters)


def list_consents(db: Session, **filters) -> dict:
    return consent_service.list_consents(db, **filters)
