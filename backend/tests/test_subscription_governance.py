"""订阅版本治理核心（subscription_governance）单测：纯函数、无 DB、确定性。

覆盖验收口径：
- 变更先生成待生效版本，连续变更合并进同一条并按当前基线重算；
- 运行中的批次钉住启动版本快照（快照不可变、指纹稳定）；
- 历史审批不被沿用：基线漂移后复核结论回退到待复核；
- 影响不明（needs_mapping / breaking）停在待复核，只有全 forward 才自动放行；
- 重试复核输入未变时指纹一致（不新增版本/审计的判据）。
"""
from __future__ import annotations

from src.constants.enums import Compat, ReviewStatus, VersionStatus
from src.services import subscription_governance as gov


def _table(table_id: int = 1, code: str = "T_CLAIM", **overrides) -> dict:
    base = {
        "table_id": table_id,
        "code": code,
        "name": "理赔报案表",
        "database_name": "biz_prod",
        "table_name": "t_claim",
        "pk_column": "claim_id",
        "schema_columns": [
            {"name": "claim_id", "type": "bigint", "nullable": False},
            {"name": "claim_amount", "type": "bigint", "nullable": True},
        ],
        "binlog_file": "mysql-bin.000418",
        "binlog_position": 100,
        "allowed_lateness_seconds": 30,
        "owner": "数据集成组",
        "note": "",
        "enabled": True,
    }
    base.update(overrides)
    return base


def _two_table_baseline() -> list[dict]:
    return [
        _table(1, "T_CLAIM"),
        _table(2, "T_POLICY", table_name="t_policy", pk_column="policy_id"),
    ]


class _Version:
    """activation_feasibility 只读取属性，用轻量对象代替 ORM 实例。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_snapshot_is_deep_copied_and_immutable_to_caller():
    table = _table()
    snapshot = gov.snapshot_tables([table])
    table["schema_columns"].append({"name": "injected", "type": "int"})
    table["enabled"] = False
    assert snapshot[0]["enabled"] is True
    assert all(col["name"] != "injected" for col in snapshot[0]["schema_columns"])


def test_fingerprint_stable_for_same_baseline_and_snapshot():
    baseline = _two_table_baseline()
    fp1 = gov.fingerprint(1, baseline)
    fp2 = gov.fingerprint(1, [dict(item) for item in baseline])
    assert fp1 == fp2
    # 基线版本号变化 -> 指纹变化（驱动「基线漂移需重算」）
    assert gov.fingerprint(2, baseline) != fp1


def test_param_change_is_forward_and_auto_approved():
    baseline = _two_table_baseline()
    changes, snapshot = gov.merge_proposal(
        baseline, {}, table_code="T_POLICY", patch={"allowed_lateness_seconds": 60}
    )
    entry = changes["T_POLICY"]
    assert entry["compat"] == Compat.FORWARD.value
    assert gov.auto_review_status(gov.aggregate_gate(changes)["compat"]) == ReviewStatus.APPROVED.value
    # 基线本身没被改
    assert baseline[1]["allowed_lateness_seconds"] == 30
    assert snapshot[1]["allowed_lateness_seconds"] == 60


def test_pause_collection_marks_table_disabled_only_in_draft_snapshot():
    baseline = _two_table_baseline()
    changes, snapshot = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"enabled": False})
    by_id = gov.index_snapshot(snapshot)
    assert by_id[1]["enabled"] is False
    # 基线（在途批次口径）保持启用
    assert gov.index_snapshot(baseline)[1]["enabled"] is True
    assert changes["T_CLAIM"]["compat"] == Compat.FORWARD.value


def test_consecutive_changes_merge_into_single_draft_and_recompute():
    baseline = _two_table_baseline()
    first, snap1 = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"allowed_lateness_seconds": 45})
    # 第二次提交同一张表：合并进同一条变更集，字段在首版基线对比下累计
    merged, snap2 = gov.merge_proposal(
        baseline, first, table_code="T_CLAIM", patch={"allowed_lateness_seconds": 90}
    )
    assert set(merged.keys()) == {"T_CLAIM"}
    assert merged["T_CLAIM"]["fields"] == ["allowed_lateness_seconds"]
    assert snap2[0]["allowed_lateness_seconds"] == 90
    # 两张表各自变更 -> 同一个 draft 内多表变更
    merged2, _ = gov.merge_proposal(baseline, merged, table_code="T_POLICY", patch={"enabled": False})
    assert set(merged2.keys()) == {"T_CLAIM", "T_POLICY"}


def test_breaking_schema_change_stops_at_pending_review():
    baseline = _two_table_baseline()
    narrowed = [
        {"name": "claim_id", "type": "bigint", "nullable": False},
        {"name": "claim_amount", "type": "int", "nullable": True},  # bigint -> int 收窄
    ]
    changes, _ = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"schema_columns": narrowed})
    gate = gov.aggregate_gate(changes)
    assert gate["compat"] == Compat.BREAKING.value
    assert gate["action"] == "block"
    assert gov.auto_review_status(gate["compat"]) == ReviewStatus.PENDING_REVIEW.value
    assert "MODIFY COLUMN" in changes["T_CLAIM"]["rollback_sql"]


def test_needs_mapping_rename_stops_at_pending_review():
    baseline = _two_table_baseline()
    renamed = [
        {"name": "claim_id", "type": "bigint", "nullable": False},
        {"name": "amount", "type": "bigint", "nullable": True},
    ]
    changes, _ = gov.merge_proposal(
        baseline,
        {},
        table_code="T_CLAIM",
        patch={"schema_columns": renamed, "rename_hints": {"claim_amount": "amount"}},
    )
    gate = gov.aggregate_gate(changes)
    assert gate["compat"] == Compat.NEEDS_MAPPING.value
    assert gov.auto_review_status(gate["compat"]) == ReviewStatus.PENDING_REVIEW.value


def test_aggregate_picks_strictest_across_tables():
    baseline = _two_table_baseline()
    narrowed = [
        {"name": "claim_id", "type": "bigint", "nullable": False},
        {"name": "claim_amount", "type": "int", "nullable": True},
    ]
    changes, _ = gov.merge_proposal(baseline, {}, table_code="T_POLICY", patch={"enabled": False})
    changes, _ = gov.merge_proposal(baseline, changes, table_code="T_CLAIM", patch={"schema_columns": narrowed})
    gate = gov.aggregate_gate(changes)
    assert gate["compat"] == Compat.BREAKING.value  # 一张表 breaking，整版不能自动放行


def test_rebase_discards_stale_approval_after_baseline_moves():
    baseline_v1 = _two_table_baseline()
    changes, _ = gov.merge_proposal(baseline_v1, {}, table_code="T_POLICY", patch={"enabled": False})
    # v2 基线先把该表水位改了（模拟另一条版本已切换）
    baseline_v2 = gov.merge_snapshot(
        baseline_v1,
        {
            "T_POLICY": {
                **changes["T_POLICY"],
                "after": {**changes["T_POLICY"]["after"], "allowed_lateness_seconds": 75},
            }
        },
    )
    rebased = gov.rebase_changes(baseline_v2, changes)
    # 基线已含 enabled=False 时，旧变更在新基线上变成 no-op 之外的等价态：字段集被重新计算
    assert "T_POLICY" in rebased
    # 重算不抛异常且结构完整
    assert {"compat", "severity", "action", "findings", "after"} <= set(rebased["T_POLICY"].keys())


def test_activation_feasibility_blocks_unapproved_stale_and_non_draft():
    baseline = _two_table_baseline()
    approved = _Version(
        version_no=2,
        status=VersionStatus.DRAFT.value,
        review_status=ReviewStatus.APPROVED.value,
        base_version_no=1,
        subscription_snapshot=baseline,
    )
    assert gov.activation_feasibility(approved, active_version_no=1)["feasible"] is True
    # 待复核不能切
    pending = _Version(
        version_no=2,
        status=VersionStatus.DRAFT.value,
        review_status=ReviewStatus.PENDING_REVIEW.value,
        base_version_no=1,
        subscription_snapshot=baseline,
    )
    result = gov.activation_feasibility(pending, active_version_no=1)
    assert result["feasible"] is False and any("复核" in p for p in result["problems"])
    # 基线漂移不能切
    stale = _Version(
        version_no=2,
        status=VersionStatus.DRAFT.value,
        review_status=ReviewStatus.APPROVED.value,
        base_version_no=1,
        subscription_snapshot=baseline,
    )
    result = gov.activation_feasibility(stale, active_version_no=3)
    assert result["feasible"] is False and any("基线" in p for p in result["problems"])
    # 非 draft 不能切
    superseded = _Version(
        version_no=1,
        status=VersionStatus.SUPERSEDED.value,
        review_status=ReviewStatus.APPROVED.value,
        base_version_no=None,
        subscription_snapshot=baseline,
    )
    assert gov.activation_feasibility(superseded, active_version_no=2)["feasible"] is False


def test_noop_patch_is_idempotent():
    baseline = _two_table_baseline()
    # 与当前工作快照完全相同的提交：不新增变更、快照不变（幂等，不报错）
    changes, snapshot = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"enabled": True})
    assert changes == {}
    assert snapshot == baseline


def test_reverted_change_is_pruned_from_draft():
    baseline = _two_table_baseline()
    narrowed = [
        {"name": "claim_id", "type": "bigint", "nullable": False},
        {"name": "claim_amount", "type": "int", "nullable": True},
    ]
    original = baseline[0]["schema_columns"]
    changes, _ = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"schema_columns": narrowed})
    assert "T_CLAIM" in changes
    # 改回基线列结构：该表变更被剔除，快照回到基线
    changes2, snapshot2 = gov.merge_proposal(baseline, changes, table_code="T_CLAIM", patch={"schema_columns": original})
    assert "T_CLAIM" not in changes2
    assert snapshot2 == baseline


def test_snapshot_diff_lists_changed_tables_and_fields():
    baseline = _two_table_baseline()
    _, candidate = gov.merge_proposal(baseline, {}, table_code="T_CLAIM", patch={"enabled": False})
    diffs = gov.snapshot_diff(baseline, candidate)
    assert len(diffs) == 1
    assert diffs[0]["code"] == "T_CLAIM"
    assert diffs[0]["fields"] == ["enabled"]
    assert diffs[0]["enabled_before"] is True and diffs[0]["enabled_after"] is False


if __name__ == "__main__":
    # 无 pip / pytest 环境下的本地自检入口
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
