"""审计动作 -> 中文标签。"""
from __future__ import annotations

ACTIONS: dict[str, str] = {
    "auth.login": "登录",
    "table.create": "登记源表订阅",
    "table.update": "修改源表配置",
    "subscription.propose": "提交订阅待生效版本",
    "subscription.review": "订阅版本复核",
    "subscription.reevaluate": "订阅版本基线重算",
    "subscription.activate": "订阅版本全量切换",
    "event.ingest": "注入乱序事件",
    "run.create": "发起合并批次",
    "reject.replay": "处置拒绝行",
    "schema.gate": "门禁复核",
    "schema.reevaluate": "重算兼容性",
    "export.merge_runs": "导出合并批次",
    "export.rejects": "导出拒绝行",
    "export.schema_changes": "导出 Schema 门禁",
    "http.mutation": "写请求",
}
