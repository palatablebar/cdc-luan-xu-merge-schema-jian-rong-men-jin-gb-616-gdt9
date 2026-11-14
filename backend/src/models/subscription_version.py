"""订阅版本：源表结构 / 订阅参数变更的可恢复版本化治理载体。

治理口径
--------
- 变更不直接改 source_tables，先落一条 ``draft``（待生效）版本；运行中的批次按
  启动时钉住的 ``subscription_snapshot`` 继续跑，不受待生效变更影响。
- 同一时刻全局至多一条 draft：连续变更合并进同一条，门禁结论按当前 active 基线重算。
- 全量切换在一个事务里完成：采集(enabled)、全库合并范围、门禁审计同时生效；
  事务失败整体回滚，上一版 active 与已确认结果原样保留，draft 仍可再次启用。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SubscriptionVersion(Base):
    __tablename__ = "subscription_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 展示用单调版本号：v1、v2……；同一时刻至多一条 active、至多一条 draft
    version_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # 基线版本号：本 draft 基于哪一版 active 算出；active 行记录自己的版本号
    base_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 全量订阅快照：[{"table_id","code","name","table_name","pk_column","schema_columns",
    #                "allowed_lateness_seconds","enabled", ...}]
    subscription_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    # 合并后的逐表变更：{table_code: {"fields":[...], "change_kind", "compat", "severity",
    #   "action", "findings":[...], "rollback_sql", "summary"}}
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    # 门禁汇总：compat/severity/action 取所有受影响表中最严格的一档
    compat: Mapped[str] = mapped_column(String(16), default="forward", index=True)
    severity: Mapped[str] = mapped_column(String(12), default="low")
    action: Mapped[str] = mapped_column(String(12), default="apply")
    review_status: Mapped[str] = mapped_column(String(16), default="pending_review", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 复核指纹：本次复核时输入（base 版本号 + 快照）的稳定哈希；输入未变时重试不重复审计
    review_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # 变更来源的合并摘要（每次提交追加一行 {at, actor, fields, reason}）
    change_log: Mapped[list] = mapped_column(JSON, default=list)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    activation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
