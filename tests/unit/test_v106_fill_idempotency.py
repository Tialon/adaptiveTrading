"""V10.6: OrderFill 内部幂等键 fill_idempotency_key 测试

验证:
- 成交ID缺失(exchange_trade_id=NULL)的重复成交不再被重复摄入 —— 修复原
  (exchange_order_id, exchange_trade_id) 两列皆可空、NULL 不参与唯一判定的漏洞;
- 幂等键格式(订单ID:成交ID, 缺失成交ID落 na);
- 非空唯一约束兜底(同键直接插入抛 IntegrityError)。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from at01_common.database import AsyncSessionLocal
from at01_common.models import OrderFill
from at60_execution.execution_executor import ExecutionEngine
from at50_risk.risk_manager import RiskManager


class TestFillIdempotency:
    async def test_record_fills_dedups_missing_trade_id(self, db_tables):
        """成交ID缺失的成交重复摄入应被去重(修复 NULL 唯一键漏洞)"""
        engine = ExecutionEngine(risk_manager=RiskManager())
        fill = {"orderId": "100", "price": "99.5", "qty": "0.5",
                "quoteQty": "49.75", "commission": "0.05", "commissionAsset": "USDT",
                "time": 1700000000000}  # 无 id -> 成交ID缺失
        args = dict(order_id=None, client_order_id="cid-1", exchange_order_id="100",
                    symbol="SOLUSDT", side="BUY")
        assert await engine._record_fills(fills=[fill], **args) == 1
        # 重复摄入同一缺失成交ID的成交 -> 键同为 100:na, 不再落库
        assert await engine._record_fills(fills=[fill], **args) == 0

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(OrderFill))).scalars().all()
            assert len(rows) == 1
            assert rows[0].exchange_trade_id is None
            assert rows[0].fill_idempotency_key == "100:na"

    async def test_fill_idempotency_key_format(self, db_tables):
        engine = ExecutionEngine(risk_manager=RiskManager())
        fills = [{"id": 42, "orderId": "100", "price": "99.5", "qty": "0.5",
                  "quoteQty": "49.75", "commission": "0", "commissionAsset": "USDT",
                  "time": 1700000000000}]
        args = dict(order_id=None, client_order_id="cid-1", exchange_order_id="100",
                    symbol="SOLUSDT", side="BUY")
        assert await engine._record_fills(fills=fills, **args) == 1

        async with AsyncSessionLocal() as session:
            row = (await session.execute(select(OrderFill))).scalar_one()
            assert row.fill_idempotency_key == "100:42"

    async def test_unique_constraint_rejects_duplicate_key(self, db_tables):
        """非空唯一约束兜底: 同键直接插入抛 IntegrityError"""
        async with AsyncSessionLocal() as session:
            session.add(OrderFill(
                client_order_id="cid-1", symbol="SOLUSDT", side="BUY",
                exchange_order_id="100", exchange_trade_id=7,
                fill_idempotency_key="100:7",
                price=100.0, quantity=1.0, quote_quantity=100.0,
            ))
            await session.commit()
        async with AsyncSessionLocal() as session:
            session.add(OrderFill(
                client_order_id="cid-2", symbol="SOLUSDT", side="BUY",
                exchange_order_id="100", exchange_trade_id=7,
                fill_idempotency_key="100:7",
                price=100.0, quantity=1.0, quote_quantity=100.0,
            ))
            with pytest.raises(IntegrityError):
                await session.commit()
