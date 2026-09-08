# 数据库迁移手册

> V11.5 P0-4 交付。本文是「数据库结构变更 / 迁移」的单一入口文档, 取代此前分散在
> `docs/runbook.md` 的零散段落。不引入 Alembic —— 项目刻意保持零迁移框架依赖,
> 靠「create_all + 手动 ALTER + 双锚点测试 + 差异检查」闭环。

## 1. 现状与边界(审查结论)

| 组件 | 现状 |
|------|------|
| 建表 | `at01_common/database.py::init_db` → `Base.metadata.create_all` |
| `create_all` 语义 | **只建缺失表, 不对既有表做 ALTER**(加列/改列/索引都不传播到已存在的库) |
| `SCHEMA_VERSION` | `at01_common/database.py` = `V11.2`, 纯标记(非迁移框架), 结构变更须同步递增 |
| `at90_deploy/init.sql` | 仅 `CREATE DATABASE`(utf8mb4), **不手写表 DDL**(表结构统一由 ORM 负责, 避免与 models.py 漂移) |
| 迁移框架 | **最小前向迁移框架(V11.6 P1-4)**: `at01_common/migrations.py::upgrade_schema` + `migrations/*.sql` + `schema_version` 簿记表(不引入 Alembic, 见 §4) |
| 锚点测试 | `tests/unit/test_v129_schema_audit.py` 钉死 25 表全列清单 + SCHEMA_VERSION + create_all 幂等 |
| 差异检查 | `at01_common/schema_check.py`(本片新增)探测「实际库 vs ORM 元数据」漂移 |

**核心风险**: 给既有表新增一列后, `create_all` 会静默忽略 —— 新库有该列、存量生产库缺列,
且 `test_v129_schema_audit.py` 只比对「ORM 元数据 vs 硬编码清单」, **捕获不到生产库缺列**。
故本片新增 `schema_check`(实际库 vs ORM)补齐该盲区。

## 2. 结构变更协议(必须同步三件事)

任何「增删表 / 增删列 / 改列名 / 改索引」都须同步:

1. **登记迁移**: 在本文「§3 迁移历史」追加一条, 并**新增 `migrations/NNN_xxx.sql` 迁移脚本**
   (见 §4, 存量库由 `upgrade_schema` 自动执行; 含建唯一索引时先去重)。
2. **递增版本**: `at01_common/database.py::SCHEMA_VERSION` +1(如 `V11.2` → `V11.3`)。
3. **同步锚点**: 更新 `tests/unit/test_v129_schema_audit.py` 的表清单 / 全量列清单 /
   关键列(否则测试红)。

> 新表(全新 `__tablename__`)无需手动 ALTER, 由 `create_all` 自动创建; 但仍需同步
> `test_v129` 表清单 + 递增 SCHEMA_VERSION, 以便锚点覆盖新表、防止漏登记。

## 3. 迁移历史

> 逐版本登记, 完整还原 schema 演进。存量库升级时从「当前已执行到的版本」向后依次执行
> 未执行的 ALTER。

- **基表(V1.0)**: `klines` / `trades` / `signals` / `orders` / `positions` / `risk_events` /
  `ai_advices` / `position_snapshot` / `strategy_performance`。
- **V2.0**: `signals` 加 `indicators VARCHAR(2048) NULL`(存量库 `ALTER TABLE signals ADD COLUMN ...`)。
- **V3.0**: 新增 `signal_result`(create_all 自动)。
- **V4.0**: 新增 `position_bucket` / `decision_log`(create_all 自动)。
- **V5**: 新增 `ai_parameter_history`(create_all 自动)。
- **V8.0**: 新增 `trade_state` / `paper_state`(create_all 自动)。
- **V9.0**: 新增 `trade_records` / `strategy_versions`; `position_bucket` 加 `target_ratio` /
  `target_quantity` / `current_value`(存量库 ALTER)。
- **V9.0 M3**: 新增 `account_ledger`(create_all 自动)。
- **V10.0**: 新增 `kill_switch_state`(单行 id=1, create_all 自动)。
- **V10.1**: 新增 `order_intents` / `order_fills`(create_all 自动)。
- **V10.2**: 新增 `execution_attempts`(create_all 自动)。
- **V10.3**: 新增 `position_lots` / `sell_allocations`; `account_ledger` 加 `realized_pnl` /
  `matched_cost`(存量库 ALTER)。
- **V10.5**: `orders` 加 `reduce_only BOOLEAN DEFAULT 0`(存量库 ALTER)。
- **V10.6**: `orders` 加 `accounting_state VARCHAR(20) NOT NULL DEFAULT 'OK'`; `order_fills` 加
  非空唯一 `fill_idempotency_key`(存量库回填 + 建唯一索引, 见 runbook)。
- **V10.7**: 新增 `execution_events`(create_all 自动)。
- **V11.0 F12**: `position_lots.client_order_id` 加唯一约束(存量库先去重再建唯一索引)。
- **V11.1 P0-2**: `order_fills` 加 `fee_quote FLOAT DEFAULT 0` / `fee_valuation_status VARCHAR(16)
  DEFAULT 'zero'`(存量库 ALTER)。
- **V11.2**: 无新表新列(纯审计: `SCHEMA_VERSION` 标记 + `test_v129` 全量列清单锚点)。
- **V11.4 P0-7**: 无迁移(仅升级 `test_v129` 为全量列清单锚点)。
- **V11.5 P0-4**: 无新表新列(新增 `schema_check` 差异检查 + 本文档)。
- **V11.6 P1-4**: 无新表新列(引入最小迁移框架 `migrations/` + `schema_version` 簿记表, 见 §4;
  `001_baseline.sql` 记为 V11.2 基线锚点)。

## 4. 迁移框架(原型)

> V11.6 P1-4 交付。**诚实边界: 这是「迁移框架原型」, 非生产级迁移框架**。它解决
> 「存量库如何执行前向 DDL」, 不替代「schema 漂移防呆」(后者仍由 §2 三件事 + §5 差异检查兜底)。

**组件**:

| 组件 | 位置 | 作用 |
|------|------|------|
| `schema_version` 表 | 原始 SQL 建(非 ORM 领域表) | 记录已应用版本号(`version` PK / `applied_at` / `description`) |
| 迁移脚本 | `migrations/NNN_xxx.sql` | 手写前向 DDL, 按版本号升序应用 |
| 升级入口 | `at01_common/migrations.py::upgrade_schema` | 检测方言 → 建簿记表 → 应用未落库迁移 → 记版本 |
| 接线 | `init_db()`(create_all 之后) | 每次启动自动升级(幂等) |

**语义**:
- `init_db()` = `create_all`(建缺失表, 不 ALTER)+ `upgrade_schema()`(应用未落库的 `migrations/*.sql`)。
- `upgrade_schema()` 幂等: 已应用版本跳过, 重复调用/重复启动为 no-op。
- 方言检测: 从 `DATABASE_URL` 判断 SQLite / MySQL(`sqlite+aiosqlite` / `mysql+aiomysql`);
  其它方言记告警并跳过(本项目仅用这两种)。
- `schema_version` 被 `schema_check` 视为内部表过滤(与 `alembic_version` 同列), 不产生漂移;
  也不进入 `Base.metadata`(不参与 `test_v129` 表清单锚点)。

**原型边界(诚实披露)**:
- 仅**前向 DDL**, 无回滚(down)、无自动生成脚本(手写 SQL)、无 ORM 元数据 diff。
- 迁移 SQL 按 `;` 粗拆, 不做字符串字面量内分号/注释转义 —— 手写迁移须用单行 `--` 注释,
  且避免在字符串里裸写 `;`。
- 版本号必须唯一(同版本重复文件 → 唯一键冲突, 视为配置错误)。
- 结构变更**仍需**同步 §2 三件事(登记历史 + 递增 `SCHEMA_VERSION` + 更新 `test_v129` 锚点),
  迁移框架只负责「执行 DDL」, 不负责「提醒你改锚点」。

**测试**: `tests/unit/test_v164_db_migration.py`(方言检测 / 列举 / 拆分 / 幂等应用 /
基线记录 / init_db 集成无漂移)。

## 5. 差异检查(schema_check)

`at01_common/schema_check.py` 提供「实际库 schema vs ORM 元数据」的**名称级**检查(表名 + 列名),
探测生产库缺列/缺表/多列/多表漂移。

```python
from at01_common.schema_check import check_schema, format_drift

drift = await check_schema()          # 用当前 settings 的 engine
assert not drift, format_drift(drift)
```

**CLI(部署前对现有库做漂移检查)**:

```powershell
# 依赖 .env 的 DATABASE_URL 指向目标库; 有漂移则逐条打印并 exit 1
.venv\Scripts\python -m at01_common.schema_check
```

**测试**: `tests/unit/test_v153_schema_check.py`(create_all 后无漂移 + 缺列可被检测)。

### 边界(诚实披露)

- **名称级**检查(表名 + 列名), 不做列**类型 / 长度 / 默认值**级比对 —— 名称级已覆盖
  `create_all` 最危险坑(缺列); 类型级需 SQLite/MySQL 归一化, 复杂度和误报不划算。
- 对**空库**(从未 `init_db`)运行会报「缺 25 表」, 属预期(未初始化), 非漂移。
- 过滤 `sqlite_*` / `alembic_version` / `schema_version` 内部表, 不参与判定。
- 该检查是**离线探测工具**, 未接入 `run.py` 启动路径(避免误报阻断启动); 建议纳入部署流程
  在升级后手动执行。

## 6. 存量库升级实操(SQLite → 默认 / MySQL → 生产)

1. **备份**(务必): `cp adaptive.db adaptive.db.bak`(或 MySQL `mysqldump`)。
2. 从「§3 迁移历史」确定当前已执行到的版本, 依次执行其后未执行的 `ALTER`; 或直接依赖
   `migrations/*.sql`(见 §4)—— 重启后 `init_db` 会自动 `upgrade_schema` 应用未落库迁移。
3. 递增 `SCHEMA_VERSION` + 更新 `test_v129` 锚点(见 §2)。
4. 跑差异检查确认收敛:
   ```powershell
   .venv\Scripts\python -m at01_common.schema_check
   .venv\Scripts\python -m pytest tests/unit/test_v129_schema_audit.py tests/unit/test_v153_schema_check.py tests/unit/test_v164_db_migration.py -q
   ```
5. 重启 `run.py`(重启自动 `create_all` 补齐新表 + `upgrade_schema` 应用未落库迁移)。
