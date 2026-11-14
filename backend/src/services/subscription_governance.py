"""订阅版本治理核心（纯函数，无 IO、无 ORM、无随机）。

与算法 A/B 同级的可恢复治理口径：

1. 快照：把一组 source_tables 压成只含治理字段的 ``subscription_snapshot``，
   运行中的批次钉住某一版快照后，后续任何变更都不影响它。
2. 待生效版本：PATCH 不直接改表，先产出 draft；同一时刻只允许一条 draft，
   连续变更通过 :func:`merge_proposal` 合并进同一条，门禁按当前 active 基线重算。
3. 门禁重算：结构变化复用算法 B（schema_gate），订阅参数/启停变化判 forward；
   多表变更取最严格的一档；只要存在 breaking / needs_mapping，复核状态停在待复核。
4. 复核指纹：输入（基线版本号 + 快照）未变时，重试复核得到同一指纹，调用方据此
   不新增版本、不新增审计（见 subscription_version_service）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from src.constants.enums import Compat, GateAction, Severity
from src.services import schema_gate

# 进入版本快照、随版本一起被钉住的治理字段（与 SourceTable ORM 字段对应）
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "table_id",
    "code",
    "name",
    "database_name",
    "table_name",
    "pk_column",
    "schema_columns",
    "binlog_file",
    "binlog_position",
    "allowed_lateness_seconds",
    "owner",
    "note",
    "enabled",
)

# PATCH 允许进入版本治理的字段（其余字段在服务层拦截）
GOVERNED_FIELDS: tuple[str, ...] = (
    "name",
    "allowed_lateness_seconds",
    "binlog_file",
    "binlog_position",
    "schema_columns",
    "owner",
    "note",
    "enabled",
)

# 不涉及列结构的纯订阅参数变更
PARAM_CHANGE_KIND = "subscription_param"

COMPAT_RANK = {Compat.BREAKING.value: 0, Compat.NEEDS_MAPPING.value: 1, Compat.FORWARD.value: 2}
COMPAT_SEVERITY = {
    Compat.BREAKING.value: Severity.HIGH.value,
    Compat.NEEDS_MAPPING.value: Severity.MEDIUM.value,
    Compat.FORWARD.value: Severity.LOW.value,
}
COMPAT_ACTION = {
    Compat.BREAKING.value: GateAction.BLOCK.value,
    Compat.NEEDS_MAPPING.value: GateAction.BACKFILL.value,
    Compat.FORWARD.value: GateAction.APPLY.value,
}


def snapshot_table(table: Any) -> dict:
    """ORM 对象或 dict -> 一条不可变快照（深拷贝，避免外部再改影响版本）。"""
    getter = table.get if isinstance(table, dict) else lambda key, default=None: getattr(table, key, default)
    snap = {}
    for field in SNAPSHOT_FIELDS:
        value = getter(field)
        if field == "table_id" and value is None:
            value = getter("id")
        snap[field] = copy.deepcopy(value)
    snap["schema_columns"] = list(snap.get("schema_columns") or [])
    return snap


def snapshot_tables(tables: list[Any]) -> list[dict]:
    return [snapshot_table(table) for table in tables]


def index_snapshot(snapshot: list[dict]) -> dict[int, dict]:
    return {int(item["table_id"]): item for item in snapshot}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(version_no: int | None, snapshot: list[dict]) -> str:
    """复核/批次指纹：同样的基线版本号 + 快照逐字一致。"""
    payload = stable_json([version_no, snapshot])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def changed_fields(before: dict, after: dict) -> list[str]:
    """逐治理字段比较，返回实际发生变化的字段名（按 GOVERNED_FIELDS 固定顺序）。"""
    return [
        field
        for field in GOVERNED_FIELDS
        if field in after and stable_json(before.get(field)) != stable_json(after.get(field))
    ]


def _param_finding(message: str, *, detail: dict) -> dict:
    return {
        "kind": PARAM_CHANGE_KIND,
        "compat": Compat.FORWARD.value,
        "severity": Severity.LOW.value,
        "action": GateAction.APPLY.value,
        "column": None,
        "type": None,
        "detail": detail,
        "message": message,
    }


def evaluate_table_change(before: dict, after: dict) -> dict:
    """对单张表的 before/after 快照做一次门禁判定（结构变化走算法 B）。

    返回 {"fields","change_kind","compat","severity","action","findings",
          "rollback_sql","summary","before","after"}。无实质变化抛 ValueError。
    """
    fields = changed_fields(before, after)
    if not fields:
        raise ValueError("subscription change is a no-op")
    full_after = {**before, **{field: after[field] for field in fields}}
    findings: list[dict] = []
    rollback_sql = ""
    change_kind = PARAM_CHANGE_KIND

    if "schema_columns" in fields:
        result = schema_gate.evaluate_schema_change(
            before.get("schema_columns") or [],
            full_after.get("schema_columns") or [],
            table_name=before.get("table_name") or "ods_table",
            rename_hints=after.get("rename_hints") or {},
        )
        findings.extend(result["findings"])
        rollback_sql = result["rollback_sql"]
        change_kind = result["change_kind"]
        compat = result["compat"]
        severity = result["severity"]
        action = result["action"]
        summary = result["summary"]
    else:
        compat = Compat.FORWARD.value
        severity = Severity.LOW.value
        action = GateAction.APPLY.value
        if "enabled" in fields:
            verb = "暂停采集" if full_after.get("enabled") is False else "恢复采集"
            findings.append(
                _param_finding(
                    f"订阅启停调整：{verb}（全库合并范围同步变化，不影响在途批次）",
                    detail={"field": "enabled", "from": before.get("enabled"), "to": full_after.get("enabled")},
                )
            )
        if "allowed_lateness_seconds" in fields:
            findings.append(
                _param_finding(
                    f"水位线参数 {before.get('allowed_lateness_seconds')}s → "
                    f"{full_after.get('allowed_lateness_seconds')}s，仅影响启用后的新批次",
                    detail={
                        "field": "allowed_lateness_seconds",
                        "from": before.get("allowed_lateness_seconds"),
                        "to": full_after.get("allowed_lateness_seconds"),
                    },
                )
            )
        other = [field for field in fields if field not in {"enabled", "allowed_lateness_seconds"}]
        for field in other:
            findings.append(
                _param_finding(
                    f"订阅参数 `{field}` 调整，不改变列结构兼容性",
                    detail={"field": field, "from": before.get(field), "to": full_after.get(field)},
                )
            )
        summary = (
            f"{before.get('table_name')}: 订阅参数调整（{', '.join(fields)}）；"
            f"判 {compat} / 动作 {action}，待版本启用后生效"
        )

    return {
        "fields": fields,
        "change_kind": change_kind,
        "compat": compat,
        "severity": severity,
        "action": action,
        "findings": findings,
        "rollback_sql": rollback_sql,
        "summary": summary,
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(full_after),
    }


def merge_snapshot(baseline_snapshot: list[dict], changes: dict[str, dict]) -> list[dict]:
    """把逐表变更（key=table code）合并进基线快照，产生 draft 的新快照。"""
    merged = copy.deepcopy(baseline_snapshot)
    by_code = {item["code"]: item for item in merged}
    for code, entry in changes.items():
        target = by_code.get(code)
        if target is None:
            raise ValueError(f"unknown table in subscription change: {code}")
        for field in GOVERNED_FIELDS:
            if field in entry["after"]:
                target[field] = copy.deepcopy(entry["after"][field])
    return merged


def merge_proposal(
    baseline_snapshot: list[dict],
    existing_changes: dict[str, dict],
    *,
    table_code: str,
    patch: dict,
) -> tuple[dict[str, dict], list[dict]]:
    """把一次 PATCH 合并进（可能已存在的）draft 变更集。

    连续变更合并检查：始终以当前 active 基线为起点，先回放历史 draft 变更，
    再叠加本次 patch，逐表重算门禁。若该表被改回与基线完全一致（撤销变更），
    则从变更集中剔除，不再占用 draft。返回 (新变更集, 新快照)。
    """
    base_by_code = {item["code"]: item for item in baseline_snapshot}
    base_entry = base_by_code.get(table_code)
    if base_entry is None:
        raise ValueError(f"unknown table in subscription change: {table_code}")

    working_snapshot = merge_snapshot(baseline_snapshot, existing_changes)
    working_by_code = {item["code"]: item for item in working_snapshot}
    current_entry = working_by_code[table_code]

    incoming = {field: patch[field] for field in GOVERNED_FIELDS if field in patch}
    new_changes = copy.deepcopy(existing_changes)
    try:
        judged = evaluate_table_change(current_entry, incoming)
    except ValueError:
        # 本次提交相对当前工作快照无变化：保持原变更集与快照
        return new_changes, working_snapshot

    new_changes[table_code] = {
        "table_id": base_entry["table_id"],
        "code": table_code,
        "fields": judged["fields"],
        "change_kind": judged["change_kind"],
        "compat": judged["compat"],
        "severity": judged["severity"],
        "action": judged["action"],
        "findings": judged["findings"],
        "rollback_sql": judged["rollback_sql"],
        "summary": judged["summary"],
        "before": judged["before"],
        "after": judged["after"],
    }
    new_snapshot = merge_snapshot(baseline_snapshot, new_changes)
    # 撤销到与基线逐字段一致：该表不再属于本次待生效变更
    refreshed_base = {item["code"]: item for item in baseline_snapshot}[table_code]
    if not changed_fields(refreshed_base, new_changes[table_code]["after"]):
        new_changes.pop(table_code, None)
        new_snapshot = merge_snapshot(baseline_snapshot, new_changes)
    return new_changes, new_snapshot


def aggregate_gate(changes: dict[str, dict]) -> dict:
    """多表变更取最严格的一档（breaking > needs_mapping > forward）。"""
    if not changes:
        return {"compat": Compat.FORWARD.value, "severity": Severity.LOW.value, "action": GateAction.APPLY.value}
    strictest = min(changes.values(), key=lambda item: COMPAT_RANK.get(item["compat"], 99))
    return {
        "compat": strictest["compat"],
        "severity": COMPAT_SEVERITY[strictest["compat"]],
        "action": COMPAT_ACTION[strictest["compat"]],
    }


def auto_review_status(compat: str) -> str:
    """全量向前兼容才自动复核通过；影响不明（needs_mapping）/破坏性一律停在待复核。"""
    from src.constants.enums import ReviewStatus

    return ReviewStatus.APPROVED.value if compat == Compat.FORWARD.value else ReviewStatus.PENDING_REVIEW.value


def rebase_changes(baseline_snapshot: list[dict], changes: dict[str, dict]) -> dict[str, dict]:
    """基线切换后，按新基线重算 draft 里每张表的门禁（历史审批不被沿用）。"""
    base_by_code = {item["code"]: item for item in baseline_snapshot}
    rebased: dict[str, dict] = {}
    for code, entry in changes.items():
        base_entry = base_by_code.get(code)
        if base_entry is None:
            continue
        incoming = {field: entry["after"][field] for field in GOVERNED_FIELDS if field in entry.get("after", {})}
        judged = evaluate_table_change(base_entry, incoming)
        rebased[code] = {
            "table_id": base_entry["table_id"],
            "code": code,
            "fields": judged["fields"],
            "change_kind": judged["change_kind"],
            "compat": judged["compat"],
            "severity": judged["severity"],
            "action": judged["action"],
            "findings": judged["findings"],
            "rollback_sql": judged["rollback_sql"],
            "summary": judged["summary"],
            "before": judged["before"],
            "after": judged["after"],
        }
    return rebased


def activation_feasibility(version: Any, active_version_no: int | None) -> dict:
    """启用前检查：复核通过、基线未漂移。不满足时返回阻断原因，不抛异常。"""
    from src.constants.enums import ReviewStatus, VersionStatus

    problems: list[str] = []
    if version.status != VersionStatus.DRAFT.value:
        problems.append(f"版本 v{version.version_no} 不是待生效版本（当前状态 {version.status}）")
    if version.review_status != ReviewStatus.APPROVED.value:
        problems.append(f"复核状态为 {version.review_status}，影响不明或未通过的版本不能全量切换")
    if version.base_version_no != active_version_no:
        problems.append(
            f"基线已漂移：该版本基于 v{version.base_version_no}，当前基线为 v{active_version_no}，需重新复核"
        )
    if not (version.subscription_snapshot or []):
        problems.append("版本快照为空，无法切换")
    return {"feasible": not problems, "problems": problems}


def snapshot_diff(baseline_snapshot: list[dict], candidate_snapshot: list[dict]) -> list[dict]:
    """供页面/审计核对的逐表差异摘要。"""
    base_by_id = index_snapshot(baseline_snapshot)
    diffs: list[dict] = []
    for item in candidate_snapshot:
        before = base_by_id.get(int(item["table_id"]))
        if before is None:
            continue
        fields = changed_fields(before, item)
        if fields:
            diffs.append(
                {
                    "table_id": item["table_id"],
                    "code": item["code"],
                    "fields": fields,
                    "enabled_before": before.get("enabled"),
                    "enabled_after": item.get("enabled"),
                }
            )
    return diffs
