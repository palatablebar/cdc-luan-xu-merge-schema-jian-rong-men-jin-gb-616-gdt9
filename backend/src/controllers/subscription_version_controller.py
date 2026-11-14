"""订阅版本控制器：复核重算（不沿用历史审批）与全量切换。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, subscription_version_service as svc
from src.utils.timeutil import iso_utc, utcnow


def overview(db: Session) -> dict:
    return svc.governance_overview(db)


def list_versions(db: Session, **filters) -> dict:
    return svc.list_versions(db, **filters)


def get_version(db: Session, version_id: int) -> dict:
    return svc.serialize(svc.get_version(db, version_id), db)


def reevaluate(db: Session, version_id: int, principal: dict, request: Request) -> dict:
    """按当前基线重算门禁。重试（输入未变）不新增版本、不新增审计。"""
    version = svc.get_version(db, version_id)
    draft, changed = svc.reevaluate_draft(db, version)
    if changed:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="subscription.reevaluate",
            entity="subscription_version",
            entity_id=draft.id,
            detail={"version_no": draft.version_no, "compat": draft.compat, "review_status": draft.review_status},
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return svc.serialize(draft, db)


def review(db: Session, version_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """人工复核：先基于当前基线重算，再落结论；影响不明的版本放行会被拒绝。

    重试审批（结论/输入未变）幂等：不新增版本、不新增审计。
    """
    version = svc.get_version(db, version_id)
    draft, audit_worthy = svc.review_draft(
        db,
        version,
        decision=payload["decision"],
        note=payload.get("note"),
        reviewer=principal["email"],
    )
    if audit_worthy:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="subscription.review",
            entity="subscription_version",
            entity_id=draft.id,
            detail={
                "version_no": draft.version_no,
                "decision": payload["decision"],
                "compat": draft.compat,
                "review_status": draft.review_status,
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return svc.serialize(draft, db)


def activate(db: Session, version_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """全量切换：采集、全库合并范围、门禁审计在同一事务共同生效，失败整体回滚。"""
    version = svc.get_version(db, version_id)
    draft = svc.activate_draft(db, version, actor=principal["email"], request_ip=request.client.host if request.client else None)
    if payload.get("note"):
        log = list(draft.change_log or [])
        log.append(
            {"at": iso_utc(utcnow()), "actor": principal["email"], "event": "activation_note", "note": payload["note"]}
        )
        draft.change_log = log
    db.commit()
    return svc.serialize(draft, db)
