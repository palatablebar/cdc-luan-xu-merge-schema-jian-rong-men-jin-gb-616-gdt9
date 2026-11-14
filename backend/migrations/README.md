# 数据库迁移说明

本项目用 SQLAlchemy `Base.metadata.create_all()` 在 backend 启动时建表（见 `src/config/app.py`），
因此首次 `docker compose up` 会自动建好表，不需要额外执行迁移。

- `001_core_tables.sql`：核心表的人类可读 DDL 参考（MySQL 8.0 语法），用于 reviewer 对照字段；
  运行时不执行它。
- 后续如果要做字段变更，建议引入 Alembic，并在本目录按 `00x_<描述>.py` 递增。
- seed 数据只走 `backend/scripts/seed_demo.py`，不要写进 SQL。

## 订阅版本治理（本迭代新增，均为加列/加表）

新表 `subscription_versions` 会被 `create_all` 自动创建；但 `merge_runs` 的两个新列在**已存在的旧库**上
不会被 `create_all` 自动补出。全新数据卷（`docker compose down -v` 后再起）无需任何操作；
若要在保留数据的旧卷上升级，手动执行以下幂等性较弱的加列 DDL（MySQL 8.0）：

```sql
ALTER TABLE merge_runs
  ADD COLUMN subscription_version_no INT NULL,
  ADD COLUMN subscription_fingerprint VARCHAR(80) NULL;
```

`subscription_versions` 的完整结构见 `001_core_tables.sql`。
