"""V11.3 P0-8 数据库一致性审计(运行时 schema 与元数据一致 + 唯一约束实际生效 + 单行表)。

V11.2 的 test_v129 已钉死表清单与关键列(静态锚点)。本片补「运行时」一致性:

1. `create_all` 之后, 实际 SQLite schema(经 sqlalchemy inspect 反射)与 ORM metadata
   一致 —— 表名集合、每表列名集合逐一对齐, 防止「ORM 声明了表/列但 create_all 没建」
   或「物理库多出孤儿表」这类漂移;
2. 承重唯一约束(幂等/防重复下单/成交幂等/事件去重等)在 **DB 层真实生效** —— 重复插入
   抛 IntegrityError, 而非仅应用层兜底(应用层兜底一旦被绕过就是重复下单/重复记账);
3. 单行表(kill_switch_state / paper_state)反复 upsert 仍单行, 不漂移出多行。

冻结不变: 纯审计 + 测试钉住, 不改记账逻辑、不加 FK(本项目软关联为既定设计)。
"""

import pytest
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError

import at01_common.database as db
import at01_common.models  # noqa: F401  确保所有模型注册进 metadata
from at01_common.database import AsyncSessionLocal
from at01_common.models import (
    ExecutionEvent,
    Kline,
    KillSwitchState,
    Order,
    OrderFill,
    OrderIntent,
    PaperState,
    Position,
    StrategyVersion,
    TradeRecord,
)


async def _reflect() -> dict:
    """反射实际 SQLite 库: {表名: 列名集合}。

    反射必须全程在 `run_sync` 的同步连接内完成 —— inspector 持有该连接引用,
    脱离 run_sync 后连接已关闭, 再调 get_table_names 会失败。
    """
    engine = db.get_engine()
    async with engine.connect() as conn:

        def _do(sync_conn):
            inspector = sa_inspect(sync_conn)
            return {
                t: {c["name"] for c in inspector.get_columns(t)}
                for t in inspector.get_table_names()
                # 过滤内部表: sqlite_* 系统表; schema_version 为迁移框架簿记表(V11.6 P1-4),
                # 与 alembic_version 同列, 非 ORM 领域表、非孤儿表, 不参与一致性别定。
                if not t.startswith("sqlite_") and t not in {"alembic_version", "schema_version"}
            }

        return await conn.run_sync(_do)


# ---------------------------------------------------------------------------
# 1. 运行时 schema 与 metadata 一致
# ---------------------------------------------------------------------------

async def test_runtime_schema_matches_metadata(db_tables):
    """create_all 后反射的表名集合、每表列名集合与 ORM metadata 完全一致。"""
    actual = await _reflect()
    metadata_tables = set(db.Base.metadata.tables.keys())

    # 表名集合: 既不多(孤儿物理表)也不少(ORM 声明未落库)
    assert set(actual.keys()) == metadata_tables, (
        f"表漂移: 物理多出 {set(actual) - metadata_tables}, "
        f"缺失 {metadata_tables - set(actual)}"
    )

    for table_name, table in db.Base.metadata.tables.items():
        expected_cols = set(table.columns.keys())
        actual_cols = actual[table_name]
        assert actual_cols == expected_cols, (
            f"表 {table_name} 列漂移: 实际多出 {actual_cols - expected_cols}, "
            f"缺失 {expected_cols - actual_cols}"
        )


# ---------------------------------------------------------------------------
# 2. 承重唯一约束 DB 层真实生效(重复插入抛 IntegrityError)
# ---------------------------------------------------------------------------

# 每项: 表模型 + 造一行的工厂(先插一行, 再插「唯一键相同」的第二行必须失败)
def _make_order() -> Order:
    return Order(client_order_id="dup-oid", symbol="SOLUSDT", side="BUY", quantity=1.0)


def _make_order_intent() -> OrderIntent:
    return OrderIntent(idempotency_key="dup-intent", symbol="SOLUSDT", side="BUY")


def _make_order_fill() -> OrderFill:
    return OrderFill(fill_idempotency_key="dup-fill", symbol="SOLUSDT", side="BUY")


def _make_execution_event() -> ExecutionEvent:
    return ExecutionEvent(event_id="dup-event", event_type="ORDER_CREATED", event_time=0)


def _make_kline() -> Kline:
    return Kline(
        symbol="SOLUSDT", interval="1h", open_time=1000,
        open=100.0, high=101.0, low=99.0, close=100.5,
        volume=10.0, quote_volume=1005.0,
    )


def _make_trade() -> TradeRecord:
    return TradeRecord(
        symbol="SOLUSDT", trade_id=1, price=100.0, quantity=1.0,
        quote_quantity=100.0, is_buyer_maker=False, trade_time=1_700_000_000_000,
    )


def _make_position() -> Position:
    return Position(symbol="SOLUSDT")


def _make_strategy_version() -> StrategyVersion:
    return StrategyVersion(version="dup-ver")


@pytest.mark.parametrize(
    "label,factory,unique_desc",
    [
        ("orders.client_order_id", _make_order, "client_order_id"),
        ("order_intents.idempotency_key", _make_order_intent, "idempotency_key"),
        ("order_fills.fill_idempotency_key", _make_order_fill, "fill_idempotency_key"),
        ("execution_events.event_id", _make_execution_event, "event_id"),
        ("klines(symbol,interval,open_time)", _make_kline, "symbol+interval+open_time"),
        ("trades(symbol,trade_id)", _make_trade, "symbol+trade_id"),
        ("positions.symbol", _make_position, "symbol"),
        ("strategy_versions.version", _make_strategy_version, "version"),
    ],
)
async def test_unique_constraint_enforced_at_db(db_tables, label, factory, unique_desc):
    """重复插入(唯一键相同)必须在 DB 层抛 IntegrityError, 而非静默落库重复行。"""
    async with AsyncSessionLocal() as session:
        session.add(factory())
        await session.commit()

        # 同唯一键再插一行 -> 必须 IntegrityError
        with pytest.raises(IntegrityError):
            session.add(factory())
            await session.commit()


# ---------------------------------------------------------------------------
# 3. 单行表反复 upsert 仍单行
# ---------------------------------------------------------------------------

async def test_kill_switch_single_row_after_repeated_persist(db_tables):
    """急停开关反复 persist(arm/disarm 多轮)后 kill_switch_state 仍仅一行 id=1。"""
    from at50_risk.risk_killswitch import KillSwitch

    ks = KillSwitch()
    for i in range(3):
        ks.arm(f"急停{i}")
        assert await ks.persist() is True
        ks.disarm()
        assert await ks.persist() is True

    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(select(KillSwitchState))).scalars())
        assert len(rows) == 1, f"kill_switch_state 应单行, 实得 {len(rows)}"
        assert rows[0].id == 1


async def test_paper_state_single_row_after_repeated_save(db_tables):
    """纸面现金反复 save_cash_to_db 后 paper_state 仍单行(不随保存次数累积)。"""
    from at60_execution.execution_paper_broker import PaperBroker

    broker = PaperBroker(initial_cash=10_000.0)
    for i in range(3):
        broker.cash = 10_000.0 - i * 100.0
        await broker.save_cash_to_db()

    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(select(PaperState))).scalars())
        assert len(rows) == 1, f"paper_state 应单行, 实得 {len(rows)}"
