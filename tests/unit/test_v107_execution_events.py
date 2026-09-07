"""V10.7(P0-a): 订单执行事件日志(append-only 审计)测试

验证:
- ExecutionEventLogger.log 落库一条事件(event_id 非空唯一 / event_type / source / sequence);
- 同订单 sequence 单调递增, 不同订单独立;
- ExecutionEngine._status_event 状态映射;
- _log_order_event 落事件并带 exchange_order_id / order_id。
"""

from sqlalchemy import select

from at01_common.database import AsyncSessionLocal
from at01_common.models import ExecutionEvent
from at50_execution.execution_events import ExecutionEventLogger
from at50_execution.execution_executor import ExecutionEngine
from at60_risk.risk_manager import RiskManager


class TestExecutionEventLogger:
    async def test_log_writes_event(self, db_tables):
        logger = ExecutionEventLogger()
        await logger.log(event_type="ORDER_CREATED", client_order_id="cid-1", order_id=1)

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(ExecutionEvent))).scalars().all()
            assert len(rows) == 1
            e = rows[0]
            assert e.client_order_id == "cid-1"
            assert e.order_id == 1
            assert e.event_type == "ORDER_CREATED"
            assert e.source == "execution"
            assert e.event_id  # 非空
            assert e.sequence == 1
            assert e.payload == "{}"

    async def test_sequence_increments_per_order(self, db_tables):
        logger = ExecutionEventLogger()
        await logger.log(event_type="ORDER_CREATED", client_order_id="cid-1")
        await logger.log(event_type="FILL", client_order_id="cid-1")
        await logger.log(event_type="ORDER_CREATED", client_order_id="cid-2")

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(ExecutionEvent).order_by(ExecutionEvent.id)
            )).scalars().all()
            seq_c1 = [r.sequence for r in rows if r.client_order_id == "cid-1"]
            seq_c2 = [r.sequence for r in rows if r.client_order_id == "cid-2"]
            assert seq_c1 == [1, 2]
            assert seq_c2 == [1]

    async def test_event_id_unique(self, db_tables):
        logger = ExecutionEventLogger()
        await logger.log(event_type="ORDER_CREATED", client_order_id="cid-1")
        await logger.log(event_type="FILL", client_order_id="cid-1")

        async with AsyncSessionLocal() as session:
            ids = (await session.execute(select(ExecutionEvent.event_id))).scalars().all()
            assert len(ids) == len(set(ids))

    async def test_payload_serialized(self, db_tables):
        logger = ExecutionEventLogger()
        await logger.log(
            event_type="RECOVERY", client_order_id="cid-1",
            payload={"accounting_state": "RECOVERY_REQUIRED"}, source="recovery",
        )

        async with AsyncSessionLocal() as session:
            e = (await session.execute(select(ExecutionEvent))).scalar_one()
            assert e.source == "recovery"
            assert "RECOVERY_REQUIRED" in e.payload


class TestStatusEventMapping:
    def test_status_maps(self):
        m = ExecutionEngine._status_event
        assert m("FILLED") == "FILL"
        assert m("PARTIALLY_FILLED") == "PARTIAL_FILL"
        assert m("CANCELED") == "CANCELED"
        assert m("REJECTED") == "REJECTED"
        assert m("EXPIRED") == "EXPIRED"
        assert m("NEW") == "ORDER_CREATED"
        assert m("SUBMITTING") == "SUBMITTING"
        assert m("UNKNOWN") == "UNKNOWN"
        assert m("SOMETHING_ELSE") == "SOMETHING_ELSE"

    async def test_log_order_event(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        await engine._log_order_event("cid-1", "FILLED", exchange_order_id="100", order_id=1)

        async with AsyncSessionLocal() as session:
            e = (await session.execute(select(ExecutionEvent))).scalar_one()
            assert e.event_type == "FILL"
            assert e.client_order_id == "cid-1"
            assert e.exchange_order_id == "100"
            assert e.order_id == 1
