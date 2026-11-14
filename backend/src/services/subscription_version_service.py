"""订阅版本服务：把纯治理核心接到 ORM，负责 draft 合并、复核重算与全量切换。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import REVIEW_STATUS_LABELS, VERSION_STATUS_LABELS, ReviewStatus, VersionStatus
from src.models.source_table import SourceTable
from src.models.subscription_version import SubscriptionVersion
from src.services import subscription_governance as gov
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, utcnow


def _next_version_no(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.max(SubscriptionVersion.version_no), 0))) or 0) + 1


def get_active(db: Session) -> SubscriptionVersion | None:
    return db.scalar(select(SubscriptionVersion).where(SubscriptionVersion.status == VersionStatus.ACTIVE.value))


def get_draft(db: Session) -> SubscriptionVersion | None:
    return db.scalar(select(SubscriptionVersion).where(SubscriptionVersion.status == VersionStatus.DRAFT.value))


def list_versions(db: Session, *, limit: int = 20, offset: int = 0) -> dict:
    count_stmt = select(func.count()).select_from(SubscriptionVersion)
    total = db.scalar(count_stmt) or 0
    rows = (
        db.scalars(
            select(SubscriptionVersion)
            .order_by(SubscriptionVersion.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
        .all()
    )
    return {"items": [serialize(row, db) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_version(db: Session, version_id: int) -> SubscriptionVersion:
    version = db.get(SubscriptionVersion, version_id)
    if version is None:
        raise not_found("subscription_version", version_id)
    return version


def baseline_snapshot(db: Session) -> list[dict]:
    tables = db.scalars(select(SourceTable).order_by(SourceTable.id)).all()
    return gov.snapshot_tables(tables)


def ensure_baseline(db: Session, *, actor: str = "system") -> SubscriptionVersion:
    """无版本时用当前 source_tables 建立 v1 当前基线（幂等）。"""
    active = get_active(db)
    if active is not None:
        return active
    snapshot = baseline_snapshot(db)
    version_no = _next_version_no(db)
    version = SubscriptionVersion(
        version_no=version_no,
        status=VersionStatus.ACTIVE.value,
        base_version_no=None,
        subscription_snapshot=snapshot,
        changes={},
        compat="forward",
        severity="low",
        action="apply",
        review_status=ReviewStatus.APPROVED.value,
        reviewer=actor,
        review_note="基线版本：登记订阅时建立",
        review_fingerprint=gov.fingerprint(version_no, snapshot),
        change_log=[],
        activated_at=utcnow(),
        created_by=actor,
    )
    db.add(version)
    db.flush()
    return version


def _refresh_gate(version: SubscriptionVersion, changes: dict, snapshot: list, *, baseline_no: int) -> None:
    gate = gov.aggregate_gate(changes)
    version.changes = changes
    version.subscription_snapshot = snapshot
    version.compat = gate["compat"]
    version.severity = gate["severity"]
    version.action = gate["action"]
    version.base_version_no = baseline_no
    version.review_fingerprint = gov.fingerprint(baseline_no, snapshot)
    # 基线重算后历史审批不沿用：非 forward 一律回到待复核；全 forward 才自动放行
    version.review_status = gov.auto_review_status(gate["compat"])
    if version.review_status != ReviewStatus.APPROVED.value:
        version.reviewer = None
        version.review_note = None
    version.updated_at = utcnow()


def submit_table_change(
    db: Session,
    table: SourceTable,
    patch: dict,
    *,
    actor: str,
    reason: str | None = None,
) -> tuple[SubscriptionVersion, list[str]]:
    """把一次订阅调整并入 draft（不存在则新建），返回 (draft, 本次实际变更字段)。"""
    ensure_baseline(db)
    active = get_active(db)
    draft = get_draft(db)

    incoming = {field: patch[field] for field in gov.GOVERNED_FIELDS if field in patch and patch[field] is not None}
    if not incoming:
        raise AppError("VALIDATION", 400, {"fields": list(patch.keys())})

    if draft is None:
        # 先算提案再决定是否建 draft：no-op（撤销到与基线一致）不产生空版本
        changes, snapshot = gov.merge_proposal(
            active.subscription_snapshot or [], {}, table_code=table.code, patch=incoming
        )
        entry = changes.get(table.code)
        if entry is None or not entry.get("fields"):
            raise AppError("CONFLICT", 409, {"reason": "no_effective_change", "table": table.code})
        draft = SubscriptionVersion(
            version_no=_next_version_no(db),
            status=VersionStatus.DRAFT.value,
            base_version_no=active.version_no,
            subscription_snapshot=snapshot,
            created_by=actor,
            change_log=[],
        )
        db.add(draft)
        db.flush()
    else:
        changes, snapshot = gov.merge_proposal(
            active.subscription_snapshot or [], draft.changes or {}, table_code=table.code, patch=incoming
        )

    entry = changes.get(table.code)
    if entry is None or not entry.get("fields"):
        # 该表被撤销回基线（无待生效差异）。先持久化剔除后的其余变更，再告知调用方无有效变更
        if not changes:
            db.delete(draft)
            db.flush()
        else:
            _refresh_gate(draft, changes, snapshot, baseline_no=active.version_no)
            db.flush()
        raise AppError("CONFLICT", 409, {"reason": "no_effective_change", "table": table.code})

    _refresh_gate(draft, changes, snapshot, baseline_no=active.version_no)
    log = list(draft.change_log or [])
    log.append(
        {
            "at": iso_utc(utcnow()),
            "actor": actor,
            "table": table.code,
            "fields": entry["fields"],
            "reason": reason or "",
            "compat": entry["compat"],
        }
    )
    draft.change_log = log
    db.flush()
    return draft, entry["fields"]


def reevaluate_draft(db: Session, draft: SubscriptionVersion) -> tuple[SubscriptionVersion, bool]:
    """按当前 active 基线重算 draft 门禁。返回 (draft, 是否产生了新结论)。

    重试（输入未变、结论未变）不新增审计：调用方凭 changed=False 跳过 audit 记录。
    """
    active = get_active(db)
    if active is None:
        raise AppError("VALIDATION", 400, {"reason": "no_active_baseline"})
    previous_fp = draft.review_fingerprint
    previous_review = draft.review_status
    rebased = gov.rebase_changes(active.subscription_snapshot or [], draft.changes or {})
    snapshot = gov.merge_snapshot(active.subscription_snapshot or [], rebased)
    _refresh_gate(draft, rebased, snapshot, baseline_no=active.version_no)
    changed = draft.review_fingerprint != previous_fp or draft.review_status != previous_review
    db.flush()
    return draft, changed


def review_draft(
    db: Session, draft: SubscriptionVersion, *, decision: str, note: str | None, reviewer: str
) -> tuple[SubscriptionVersion, bool]:
    """人工复核：先按当前基线重算，再落复核结论。

    返回 (draft, audit_worthy)。重试审批（同一复核结论、同一指纹、同一备注与复核人）
    是幂等操作，audit_worthy=False，调用方不新增版本、不新增审计。
    """
    prior = {
        "fingerprint": draft.review_fingerprint,
        "review_status": draft.review_status,
        "reviewer": draft.reviewer,
        "review_note": draft.review_note,
    }
    draft, recompute_changed = reevaluate_draft(db, draft)
    if decision == ReviewStatus.BLOCKED.value:
        draft.review_status = ReviewStatus.BLOCKED.value
    elif decision == ReviewStatus.APPROVED.value:
        if draft.compat != "forward":
            raise AppError(
                "CONFLICT",
                409,
                {
                    "reason": "impact_unresolved",
                    "compat": draft.compat,
                    "review_status": draft.review_status,
                    "hint": "破坏性或影响不明的变更不能放行，版本停在待复核",
                },
            )
        draft.review_status = ReviewStatus.APPROVED.value
    else:
        # pending_review：仅触发一次重算，不沿用任何历史审批；输入未变则不产生审计
        return draft, recompute_changed
    draft.reviewer = reviewer
    draft.review_note = note
    draft.updated_at = utcnow()
    db.flush()
    # 与上一次已确认结果逐字一致 -> 重试审批，不新增审计
    audit_worthy = (
        recompute_changed
        or prior["fingerprint"] != draft.review_fingerprint
        or prior["review_status"] != draft.review_status
        or prior["reviewer"] != reviewer
        or prior["review_note"] != note
    )
    return draft, audit_worthy


def activate_draft(db: Session, draft: SubscriptionVersion, *, actor: str, request_ip: str | None = None) -> SubscriptionVersion:
    """一次全量切换：采集范围 / 全库合并 / 门禁审计在同一事务共同生效。

    任何一步失败由外层请求事务整体回滚：上一版 active 与已确认结果保留，
    draft 维持待生效（review_status / 指纹不变），可修正后再次启用。
    调用方（controller）负责在同事务写审计并 commit。
    """
    from src.services import audit_service

    active = get_active(db)
    active_no = active.version_no if active else None
    feasibility = gov.activation_feasibility(draft, active_no)
    if not feasibility["feasible"]:
        raise AppError("CONFLICT", 409, {"reason": "activation_blocked", "problems": feasibility["problems"]})

    snapshot_by_id = gov.index_snapshot(draft.subscription_snapshot or [])
    tables = db.scalars(select(SourceTable).order_by(SourceTable.id)).all()
    for table in tables:
        snap = snapshot_by_id.get(int(table.id))
        if snap is None:
            # draft 创建后才登记的新表：快照里没有它，按当前实值补进切换后的版本，不阻断切换
            snapshot_by_id[int(table.id)] = gov.snapshot_table(table)
            continue
        for field in gov.GOVERNED_FIELDS:
            setattr(table, field, snap[field])
    # 把后登记的表并入版本快照，保证 active 版本覆盖全量订阅
    if len(snapshot_by_id) != len(draft.subscription_snapshot or []):
        merged_snapshot = [snapshot_by_id[int(t.id)] for t in tables]
        draft.subscription_snapshot = merged_snapshot
        draft.review_fingerprint = gov.fingerprint(active_no, merged_snapshot)
    # 旧基线置为 superseded，draft 提升为 active
    if active is not None:
        active.status = VersionStatus.SUPERSEDED.value
        active.updated_at = utcnow()
    draft.status = VersionStatus.ACTIVE.value
    draft.activated_at = utcnow()
    draft.activation_error = None
    draft.updated_at = utcnow()
    log = list(draft.change_log or [])
    log.append(
        {
            "at": iso_utc(utcnow()),
            "actor": actor,
            "event": "activated",
            "from_version": active_no,
            "to_version": draft.version_no,
            "affected_tables": len(tables),
        }
    )
    draft.change_log = log
    audit_service.record(
        db,
        actor_email=actor,
        actor_role="admin",
        action="subscription.activate",
        entity="subscription_version",
        entity_id=draft.id,
        detail={
            "version_no": draft.version_no,
            "base_version_no": active_no,
            "compat": draft.compat,
            "review_status": draft.review_status,
            "diff": gov.snapshot_diff(active.subscription_snapshot or [] if active else [], draft.subscription_snapshot or []),
        },
        ip=request_ip,
    )
    db.flush()
    return draft


def serialize(version: SubscriptionVersion, db: Session | None = None) -> dict:
    active = get_active(db) if db is not None else None
    active_no = active.version_no if active else None
    changes = version.changes or {}
    affected = [
        {
            "code": code,
            "table_id": entry.get("table_id"),
            "fields": entry.get("fields", []),
            "compat": entry.get("compat"),
            "change_kind": entry.get("change_kind"),
            "summary": entry.get("summary"),
        }
        for code, entry in changes.items()
    ]
    baseline_stale = (
        version.status == VersionStatus.DRAFT.value
        and active_no is not None
        and version.base_version_no != active_no
    )
    return {
        "id": version.id,
        "version_no": version.version_no,
        "version_label": f"v{version.version_no}",
        "status": version.status,
        "status_label": VERSION_STATUS_LABELS.get(version.status, version.status),
        "base_version_no": version.base_version_no,
        "base_version_label": f"v{version.base_version_no}" if version.base_version_no else None,
        "active_version_no": active_no,
        "baseline_stale": baseline_stale,
        "subscription_snapshot": version.subscription_snapshot or [],
        "changes": changes,
        "affected_tables": affected,
        "affected_count": len(affected),
        "compat": version.compat,
        "severity": version.severity,
        "action": version.action,
        "review_status": version.review_status,
        "review_status_label": REVIEW_STATUS_LABELS.get(version.review_status, version.review_status),
        "review_note": version.review_note,
        "reviewer": version.reviewer,
        "review_fingerprint": version.review_fingerprint,
        "change_log": version.change_log or [],
        "activation_error": version.activation_error,
        "activated_at": iso_utc(version.activated_at),
        "created_by": version.created_by,
        "created_at": iso_utc(version.created_at),
        "updated_at": iso_utc(version.updated_at),
    }


def governance_overview(db: Session) -> dict:
    """页面核对用：当前版本 + 待生效变更 + 版本数量。"""
    active = get_active(db)
    draft = get_draft(db)
    total = db.scalar(select(func.count()).select_from(SubscriptionVersion)) or 0
    return {
        "current": serialize(active, db) if active else None,
        "pending": serialize(draft, db) if draft else None,
        "version_total": total,
        "has_pending_change": draft is not None,
    }
