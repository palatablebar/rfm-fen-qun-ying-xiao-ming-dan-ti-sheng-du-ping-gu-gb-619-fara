from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import COMPLIANCE_PUBLISH_ROLES, COMPLIANCE_REVIEW_ROLES
from src.controllers import compliance_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.compliance import CompliancePublishIn, ComplianceReviewIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/audiences", tags=["compliance"])


@router.get("/{audience_id}/compliance")
def get_compliance(
    audience_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return compliance_controller.get_compliance(db, audience_id)


@router.get("/{audience_id}/compliance/members")
def list_compliance_members(
    audience_id: int,
    status: str | None = Query(default=None, max_length=12),
    source: str | None = Query(default=None, max_length=12),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return compliance_controller.list_members(
        db, audience_id, principal, status=status, source=source, limit=page.limit, offset=page.offset
    )


@router.post("/{audience_id}/compliance/review")
def review_compliance(
    audience_id: int,
    payload: ComplianceReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*COMPLIANCE_REVIEW_ROLES)),
) -> dict:
    return compliance_controller.review(db, audience_id, payload.model_dump(), principal, request)


@router.post("/{audience_id}/compliance/publish")
def publish_compliance(
    audience_id: int,
    payload: CompliancePublishIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*COMPLIANCE_PUBLISH_ROLES)),
) -> dict:
    return compliance_controller.publish(db, audience_id, payload.model_dump(), principal, request)
