"""合规复核控制器：复核 / 发布都落审计；发布失败回滚保留旧版。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.constants.enums import Role
from src.services import audit_service, audience_service, compliance_service


def get_compliance(db: Session, audience_id: int) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    return compliance_service.get_compliance(db, audience)


def list_members(db: Session, audience_id: int, principal: dict, **filters) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    return compliance_service.list_members(
        db, audience, mask=principal.get("role") == Role.VIEWER.value, **filters
    )


def review(db: Session, audience_id: int, payload: dict, principal: dict, request: Request) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    result = compliance_service.review_audience(
        db,
        audience=audience,
        send_channel=payload["send_channel"],
        reimport=payload.get("reimport", False),
        operator_email=principal["email"],
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="compliance.review",
        entity="audience",
        entity_id=audience.id,
        detail={
            "audience_no": audience.audience_no,
            "round_no": result["round_no"],
            "send_channel": result["send_channel"],
            "sendable": result["counts"]["sendable"],
            "excluded": result["counts"]["excluded"],
            "backfill": result["counts"]["backfill"],
            "shortage": result["counts"]["shortage"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


def publish(db: Session, audience_id: int, payload: dict, principal: dict, request: Request) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    try:
        result = compliance_service.publish_audience(
            db,
            audience=audience,
            operator_email=principal["email"],
            note=payload.get("note"),
        )
        current = result["current_version"]
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="compliance.publish",
            entity="audience",
            entity_id=audience.id,
            detail={
                "audience_no": audience.audience_no,
                "version_no": current["version_no"],
                "sendable": current["sendable_count"],
                "excluded": current["excluded_count"],
                "backfill": current["backfill_count"],
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
    except Exception:
        db.rollback()  # 失败保留旧版：is_current 的旧版本随回滚恢复原状
        raise
    return result
