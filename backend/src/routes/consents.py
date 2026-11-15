from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CONSENT_WRITE_ROLES
from src.controllers import consent_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.consent import ConsentBatchIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/consents", tags=["consents"])


@router.get("/summary")
def consent_summary(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return consent_controller.summary(db)


@router.get("/events")
def list_events(
    channel: str | None = Query(default=None, max_length=8),
    action: str | None = Query(default=None, max_length=16),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return consent_controller.list_events(db, channel=channel, action=action, limit=page.limit, offset=page.offset)


@router.get("")
def list_consents(
    channel: str | None = Query(default=None, max_length=8),
    status: str | None = Query(default=None, max_length=12),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return consent_controller.list_consents(db, channel=channel, status=status, limit=page.limit, offset=page.offset)


@router.post("/events", status_code=201)
def import_events(
    payload: ConsentBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CONSENT_WRITE_ROLES)),
) -> dict:
    return consent_controller.import_events(db, payload.model_dump(), principal, request)
