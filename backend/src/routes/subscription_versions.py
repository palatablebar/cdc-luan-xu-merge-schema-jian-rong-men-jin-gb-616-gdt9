from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import SUBSCRIPTION_WRITE_ROLES
from src.controllers import subscription_version_controller as controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.subscription_version import VersionActivateIn, VersionReviewIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/subscription-versions", tags=["subscription-versions"])


@router.get("/overview")
def overview(
    db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return controller.overview(db)


@router.get("")
def list_versions(
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return controller.list_versions(db, limit=page.limit, offset=page.offset)


@router.get("/{version_id}")
def get_version(
    version_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return controller.get_version(db, version_id)


@router.post("/{version_id}/reevaluate")
def reevaluate(
    version_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*SUBSCRIPTION_WRITE_ROLES)),
) -> dict:
    return controller.reevaluate(db, version_id, principal, request)


@router.patch("/{version_id}/review")
def review(
    version_id: int,
    payload: VersionReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*SUBSCRIPTION_WRITE_ROLES)),
) -> dict:
    return controller.review(db, version_id, payload.model_dump(), principal, request)


@router.post("/{version_id}/activate")
def activate(
    version_id: int,
    payload: VersionActivateIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*SUBSCRIPTION_WRITE_ROLES)),
) -> dict:
    return controller.activate(db, version_id, payload.model_dump(), principal, request)
