"""V11.2 P1-5 数据库迁移审计(schema 稳定性锚点)。

现状: 项目无迁移框架(Alembic), `init_db` 用 `Base.metadata.create_all` 建表 —— 只创建
**缺失**的表, 不会对既有表做 ALTER(新增列/改列/索引都不会传播到已存在的生产库)。

本测试作为「schema 稳定性锚点」:
1. 钉死表清单(任何增删表/改表名都会在测试里显式暴露, 需自觉同步);
2. 钉死资金守恒关键表的**关键列**(orders/order_fills/account_ledger/position_lots/
   sell_allocations/positions), 防止记账链路列被误删;
3. 校验 `SCHEMA_VERSION` 标记存在(结构变更时需同步递增)。

生产迁移指引见 runbook: 结构变更必须显式写迁移(或重建表)后同步更新本测试 + SCHEMA_VERSION。
"""

import at01_common.database as db
import at01_common.models  # noqa: F401  确保所有模型注册进 metadata

# 资金守恒链路的关键表 -> 关键列(误删这些列会破坏对账/账务不变量)
_CRITICAL_COLUMNS = {
    "orders": {"client_order_id", "symbol", "side", "filled_quantity", "quantity", "status"},
    "order_fills": {"client_order_id", "symbol", "side", "quantity", "quote_quantity", "fee_quote"},
    "account_ledger": {"related_order_id", "symbol", "side", "asset", "change_amount"},
    "position_lots": {"client_order_id", "symbol", "quantity", "status"},
    "sell_allocations": {"lot_id", "sell_client_order_id", "quantity"},
    "positions": {"symbol", "quantity", "avg_price", "realized_pnl"},
}

# 当前完整表清单(结构变更时需自觉同步此清单)
_EXPECTED_TABLES = {
    "klines", "trades", "signals", "orders", "order_intents", "order_fills",
    "execution_attempts", "position_lots", "sell_allocations", "positions",
    "risk_events", "ai_advices", "position_snapshot", "strategy_performance",
    "signal_result", "position_bucket", "trade_records", "strategy_versions",
    "decision_log", "ai_parameter_history", "trade_state", "paper_state",
    "account_ledger", "kill_switch_state", "execution_events",
}


def test_schema_version_defined():
    assert isinstance(db.SCHEMA_VERSION, str) and db.SCHEMA_VERSION


def test_table_inventory_pinned():
    actual = set(db.Base.metadata.tables.keys())
    assert actual == _EXPECTED_TABLES, (
        f"表清单漂移: 新增 {actual - _EXPECTED_TABLES}, 缺失 {_EXPECTED_TABLES - actual}"
    )


def test_critical_columns_pinned():
    tables = db.Base.metadata.tables
    for table_name, required_cols in _CRITICAL_COLUMNS.items():
        assert table_name in tables, f"缺失关键表 {table_name}"
        actual_cols = set(tables[table_name].columns.keys())
        missing = required_cols - actual_cols
        assert not missing, f"表 {table_name} 缺失关键列 {missing}"


async def test_create_all_idempotent_smoke():
    """create_all 幂等: 重复建表不报错(即便在内存库上)。"""
    await db.init_db()
    await db.init_db()  # 二次调用不报错
