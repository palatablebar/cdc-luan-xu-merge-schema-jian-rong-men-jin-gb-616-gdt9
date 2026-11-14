"""源表控制器：登记直接建立基线版本；订阅调整一律进待生效版本治理。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, subscription_version_service, table_service
from src.utils.timeutil import iso_utc, utcnow


def list_tables(db: Session, **filters) -> dict:
    return table_service.list_tables(db, **filters)


def get_table(db: Session, table_id: int) -> dict:
    table = table_service.get_table(db, table_id)
    active = subscription_version_service.get_active(db)
    return table_service.serialize(table, version_no=active.version_no if active else None)


def create_table(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    # 登记新订阅不影响任何在途批次：直接落表并把它并入当前基线版本
    active = subscription_version_service.ensure_baseline(db)
    table = table_service.create_table(db, payload)
    new_snapshot = subscription_version_service.baseline_snapshot(db)
    active.subscription_snapshot = new_snapshot
    # 基线内容变化但仍是同一当前版本：刷新指纹，已完成批次仍保留各自钉住的指纹
    active.review_fingerprint = subscription_version_service.gov.fingerprint(active.version_no, new_snapshot)
    log = list(active.change_log or [])
    log.append(
        {
            "at": iso_utc(utcnow()),
            "actor": principal["email"],
            "event": "table_registered",
            "table": table.code,
        }
    )
    active.change_log = log
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="table.create",
        entity="source_table",
        entity_id=table.id,
        detail={"code": table.code, "table": table.table_name, "baseline_version": active.version_no},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = table_service.serialize(table, version_no=active.version_no)
    return data


def update_table(db: Session, table_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """订阅调整不直接改 source_tables：生成/合并待生效版本，等全量切换后才生效。"""
    table = table_service.get_table(db, table_id)
    draft, fields = subscription_version_service.submit_table_change(
        db,
        table,
        payload,
        actor=principal["email"],
        reason=payload.get("note") if isinstance(payload, dict) else None,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="subscription.propose",
        entity="subscription_version",
        entity_id=draft.id,
        detail={
            "version_no": draft.version_no,
            "table": table.code,
            "fields": fields,
            "compat": draft.compat,
            "review_status": draft.review_status,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    # 返回的仍是当前基线里的表（运行口径未变），并附上待生效版本供页面核对
    active = subscription_version_service.get_active(db)
    data = table_service.serialize(table, version_no=active.version_no if active else None)
    data["pending_version"] = subscription_version_service.serialize(draft, db)
    return data
