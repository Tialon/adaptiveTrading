"""V11.2 P1-5 + V11.4 P0-7 数据库迁移审计(schema 稳定性锚点)。

现状: 项目无迁移框架(Alembic), `init_db` 用 `Base.metadata.create_all` 建表 —— 只创建
**缺失**的表, 不会对既有表做 ALTER(新增列/改列/索引都不会传播到已存在的生产库)。
升级靠 runbook 手动 `ALTER`, 靠本测试钉死 schema 防漂移。

V11.4 P0-7 审计结论: 原测试只钉「表名 + 资金守恒关键列子集」, 无法捕获「给某张既有表
新增一列但忘记写 runbook ALTER」这类最危险的漂移(create_all 静默忽略新列, 生产库缺列)。
故升级为**全量列清单锚点**: 25 张表每一列都显式钉死, 任何增删改列/改名都让本测试变红,
迫使开发者同步完成 runbook 三件事(登记 ALTER + 递增 SCHEMA_VERSION + 更新本清单)。

本测试作为「schema 稳定性锚点」:
1. 钉死表清单(任何增删表/改表名都在测试里显式暴露);
2. 钉死**全量列清单**(25 张表每一列), 防记账链路及任意列被误删/误加;
3. 钉死资金守恒关键表的**关键列**(语义冗余锚点, 单独成测便于读懂为何关键);
4. 钉死 `SCHEMA_VERSION` 精确值(结构变更时必须同步递增, 否则红)。

生产迁移指引见 runbook: 结构变更必须显式写迁移(或重建表)后同步更新本测试 + SCHEMA_VERSION。
"""

import at01_common.database as db
import at01_common.models  # noqa: F401  确保所有模型注册进 metadata

# 资金守恒链路的关键表 -> 关键列(误删这些列会破坏对账/账务不变量)。
# 注意: 这是「语义冗余」锚点, 全量列清单(_EXPECTED_COLUMNS)已覆盖, 此处仅为可读性。
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
    "account_ledger", "kill_switch_state", "execution_events", "hodl_benchmark",
}

# 当前完整列清单(V11.4 P0-7: 逐表逐列钉死)。任何加列/删列/改列名都需同步:
# (1) runbook 登记 ALTER;(2) 递增 SCHEMA_VERSION;(3) 更新本清单。
_EXPECTED_COLUMNS = {
    "klines": {"id", "symbol", "interval", "open_time", "open", "high", "low", "close",
               "volume", "quote_volume", "trade_count", "closed", "created_at"},
    "trades": {"id", "symbol", "trade_id", "price", "quantity", "quote_quantity",
               "is_buyer_maker", "trade_time", "created_at"},
    "signals": {"id", "symbol", "strategy", "side", "price", "quantity", "quote_amount",
                "reason", "score", "indicators", "status", "created_at"},
    "orders": {"id", "client_order_id", "exchange_order_id", "symbol", "side", "order_type",
               "price", "quantity", "filled_quantity", "avg_fill_price", "status", "strategy",
               "signal_id", "is_paper", "reduce_only", "accounting_state", "error_msg",
               "created_at", "updated_at"},
    "order_intents": {"id", "idempotency_key", "signal_id", "symbol", "side", "quantity",
                      "price", "status", "client_order_id", "created_at"},
    "order_fills": {"id", "order_id", "client_order_id", "exchange_order_id", "exchange_trade_id",
                    "fill_idempotency_key", "symbol", "side", "price", "quantity", "quote_quantity",
                    "commission", "commission_asset", "fee_quote", "fee_valuation_status",
                    "trade_time", "created_at"},
    "execution_attempts": {"id", "order_id", "client_order_id", "attempt_no", "action", "symbol",
                           "side", "request", "response", "outcome", "exchange_order_id", "created_at"},
    "position_lots": {"id", "symbol", "quantity", "price", "fee_quote", "client_order_id",
                      "exchange_order_id", "status", "created_at"},
    "sell_allocations": {"id", "symbol", "sell_client_order_id", "sell_exchange_order_id", "lot_id",
                         "quantity", "lot_price", "sell_price", "realized_pnl", "created_at"},
    "positions": {"id", "symbol", "quantity", "avg_price", "realized_pnl", "peak_price", "updated_at"},
    "risk_events": {"id", "event_type", "symbol", "detail", "equity", "created_at"},
    "ai_advices": {"id", "symbol", "advice", "confidence", "summary", "raw_response", "created_at"},
    "position_snapshot": {"id", "symbol", "quantity", "avg_cost", "market_price",
                          "unrealized_profit", "realized_profit", "equity", "timestamp"},
    "strategy_performance": {"id", "strategy", "symbol", "trade_count", "win_count", "win_rate",
                             "profit", "max_drawdown", "period_start", "updated_at"},
    "signal_result": {"id", "signal_id", "symbol", "strategy", "side", "entry_price",
                      "future_profit", "max_profit", "max_drawdown", "window_seconds", "final",
                      "updated_at"},
    "position_bucket": {"id", "symbol", "bucket_type", "quantity", "avg_cost", "realized_pnl",
                        "target_ratio", "target_quantity", "current_value", "updated_at"},
    "trade_records": {"id", "symbol", "strategy", "bucket", "entry_ts", "exit_ts", "entry_price",
                      "exit_price", "quantity", "realized_pnl", "holding_seconds", "max_profit",
                      "max_drawdown", "mistake_reason", "regime", "created_at"},
    "strategy_versions": {"id", "version", "params", "note", "backtest_result", "live_result",
                          "active", "created_at"},
    "decision_log": {"id", "symbol", "action", "price", "quantity", "regime", "regime_confidence",
                     "alpha_score", "decision_score", "core_qty", "trade_qty", "cash", "equity",
                     "reason", "context", "created_at"},
    "ai_parameter_history": {"id", "symbol", "param_name", "old_value", "new_value", "reason",
                             "pnl_after_24h", "effective", "created_at"},
    "trade_state": {"id", "symbol", "state", "updated_at"},
    "paper_state": {"id", "cash", "updated_at"},
    "account_ledger": {"id", "ts", "symbol", "bucket", "side", "asset", "before_amount",
                       "change_amount", "after_amount", "commission", "commission_asset",
                       "realized_pnl", "matched_cost", "reason", "related_order_id", "created_at"},
    "kill_switch_state": {"id", "armed", "reason", "updated_at"},
    "execution_events": {"id", "event_id", "order_id", "client_order_id", "exchange_order_id",
                         "event_type", "event_time", "payload", "source", "sequence", "created_at"},
    "hodl_benchmark": {"id", "symbol", "initial_equity", "initial_sol_qty",
                       "initial_sol_price", "recorded_at"},
}


def test_schema_version_pinned():
    # 结构变更时必须同步递增(否则红), 防止「改了 schema 却忘记 bump 版本标记」。
    assert db.SCHEMA_VERSION == "V12.0", (
        f"SCHEMA_VERSION 漂移: 期望 V12.0, 实际 {db.SCHEMA_VERSION}。"
        "结构变更需同步递增版本并更新本测试。"
    )


def test_table_inventory_pinned():
    actual = set(db.Base.metadata.tables.keys())
    assert actual == _EXPECTED_TABLES, (
        f"表清单漂移: 新增 {actual - _EXPECTED_TABLES}, 缺失 {_EXPECTED_TABLES - actual}"
    )


def test_full_column_inventory_pinned():
    """全量列清单: 每张表每一列都钉死, 加列/删列/改列名都会暴露。

    这是迁移审计的核心锚点 —— create_all 对既有表不做 ALTER, 若给既有表新增列
    却未在 runbook 登记 ALTER, 生产库会静默缺列; 本测试强制显式同步。
    """
    tables = db.Base.metadata.tables
    assert set(tables.keys()) == set(_EXPECTED_COLUMNS.keys()), (
        f"_EXPECTED_COLUMNS 未覆盖所有表: "
        f"缺 {set(tables.keys()) - set(_EXPECTED_COLUMNS.keys())}, "
        f"多 {set(_EXPECTED_COLUMNS.keys()) - set(tables.keys())}"
    )
    for table_name, expected_cols in _EXPECTED_COLUMNS.items():
        actual_cols = set(tables[table_name].columns.keys())
        added = actual_cols - expected_cols
        removed = expected_cols - actual_cols
        assert not (added or removed), (
            f"表 {table_name} 列漂移: 新增 {added or '∅'}, 缺失 {removed or '∅'}。"
            "加列需 runbook 登记 ALTER + 递增 SCHEMA_VERSION + 更新本清单。"
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
