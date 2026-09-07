"""
交易所真相对账(V10.7, P0-d)

周期核对「本地订单成交维度」与「交易所真相(myTrades)」是否一致:
- fill_truth: 本地 FILLED/PARTIALLY_FILLED 订单的 filled_quantity 应与交易所 myTrades
  该订单成交额合计一致(容差); 不一致 -> fill_truth_mismatch; 交易所无成交 -> fill_truth_missing。
- orphan_trade: 交易所 myTrades 里存在本地无记录的成交(orderId 不在本地订单) ->
  交易所单侧成交(本地漏记真实成交), 严重漂移。

与其它对账器的分工(不重叠):
- PositionReconciler: 持仓数量/权益维度(本地 vs 交易所余额)。
- CrossReconciler: 本地 DB 内部一致性(Order/Fill/Ledger/Lot 互恰)。
- OrderRecoveryEngine: 非终态订单收敛(UNKNOWN/RECOVERY_REQUIRED -> 真实态)。
- 本对账器: 成交维度(本地 filled_quantity vs 交易所逐笔成交) + 孤儿成交检测。

纯交易所 + DB 读, 不改记账逻辑。V11.1(P0-1): 分页拉全并检测完整性; 成交历史不完整时
只降级(truth_incomplete / pagination_exhausted), 不误判 fill_truth_missing/mismatch。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from at01_common.logger import LoggerMixin


def _naive_utc(dt: datetime) -> datetime:
    """去掉 tzinfo(SQLite 回读为 naive datetime, 与 cutoff 比较前统一口径)"""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class ExchangeTruthReconciler(LoggerMixin):
    """订单/成交维度交易所真相对账器"""

    def __init__(self, rest_client: Any = None):
        self.rest = rest_client

    async def reconcile(
        self,
        symbol: str,
        window_seconds: float = 900.0,
        tolerance: float = 1e-6,
    ) -> list[dict[str, Any]]:
        """核对近期实盘订单的成交维度 vs 交易所真相, 返回差异列表(空 = 自洽)"""
        if self.rest is None:
            return []

        # V11.1(P0-1): 真相对账窗口从本地待核订单的最小创建时间推导(去硬编码「15min/100 笔」);
        # 无待核订单时回退 now-window_seconds(仅用于孤儿成交检测)。
        local_orders = await self._load_recent_orders(symbol, window_seconds)
        start_ms = self._derive_start_ms(local_orders, window_seconds)

        try:
            get_all = getattr(self.rest, "get_my_trades_all", None)
            if get_all is not None:
                result = await get_all(symbol, start_time=start_ms)
                (my_trades, complete, exhausted, duplicate_ids, gaps, page_count) = \
                    self._unwrap_result(result)
            else:
                # 测试/降级 rest 无 get_my_trades_all: 回退单页, 无法判定完整性, 保守按完整
                my_trades = await self.rest.get_my_trades(symbol, limit=100)
                complete, exhausted, duplicate_ids, gaps, page_count = True, False, [], [], 1
        except Exception as e:
            self.logger.warning("交易所真相对账获取成交历史失败", error=str(e))
            return [{"type": "api_error", "symbol": symbol, "detail": str(e)}]

        mismatches: list[dict[str, Any]] = []

        # 数据完整性信号: 分页耗尽/重复/跳号。这些不是资金错误, 只降级不冻结(由上层处置)。
        if exhausted:
            mismatches.append({
                "type": "pagination_exhausted", "symbol": symbol, "pages": page_count,
            })
        if duplicate_ids:
            mismatches.append({
                "type": "trade_duplicate", "symbol": symbol, "duplicate_ids": duplicate_ids,
            })
        if gaps:
            mismatches.append({"type": "trade_id_gap", "symbol": symbol, "gaps": gaps})

        if not complete:
            # 数据被 max_pages 截断: 逐订单成交核对与孤儿检测都不可靠(会误判 missing/mismatch),
            # 直接返回完整性信号, 不再做 fill_truth 判定。
            mismatches.append({
                "type": "truth_incomplete", "symbol": symbol, "reason": "pagination_exhausted",
            })
            return mismatches

        # 交易所维度: orderId -> 成交额合计(逐笔 qty 累加; 已去重, 重复不重复计数)
        exchange_fill: dict[str, float] = {}
        for t in my_trades:
            oid = str(t.get("orderId", ""))
            qty = float(t.get("qty", 0) or 0)
            exchange_fill[oid] = exchange_fill.get(oid, 0.0) + qty

        all_eids = await self._load_all_eids(symbol)

        for o in local_orders:
            eid = o.get("exchange_order_id")
            filled = float(o.get("filled_quantity") or 0.0)
            if filled <= 0 or not eid:
                # 无成交或无交易所ID -> 交叉对账兜底, 本对账器不重复
                continue
            ex_qty = exchange_fill.get(eid)
            if ex_qty is None:
                mismatches.append({
                    "type": "fill_truth_missing", "symbol": symbol,
                    "client_order_id": o["client_order_id"],
                    "exchange_order_id": eid, "expected": filled, "actual": 0.0,
                })
            elif abs(ex_qty - filled) > tolerance:
                mismatches.append({
                    "type": "fill_truth_mismatch", "symbol": symbol,
                    "client_order_id": o["client_order_id"],
                    "exchange_order_id": eid, "expected": filled, "actual": ex_qty,
                })

        # 交易所单侧成交(本地无对应订单)
        for oid, qty in exchange_fill.items():
            if oid not in all_eids:
                mismatches.append({
                    "type": "orphan_trade", "symbol": symbol,
                    "exchange_order_id": oid, "quantity": qty,
                })

        return mismatches

    # ---------- 内部 ----------

    def _unwrap_result(self, result: Any) -> tuple[list, bool, bool, list, list, int]:
        """兼容 get_my_trades_all 返回 MyTradesResult(新) 与旧 list(测试/降级)"""
        if hasattr(result, "trades"):
            return (
                result.trades,
                result.complete,
                result.pagination_exhausted,
                list(result.duplicate_ids),
                list(result.gaps),
                int(result.page_count),
            )
        return (result, True, False, [], [], 1)

    def _derive_start_ms(
        self,
        local_orders: list[dict[str, Any]],
        window_seconds: float,
        buffer_seconds: float = 60.0,
    ) -> int:
        """真相对账窗口起点: 取本地待核订单最早 created_at(减 buffer 覆盖成交滞后);
        无待核订单则回退 now - window_seconds(仅孤儿检测用)。"""
        times = [o.get("created_at") for o in local_orders if o.get("created_at") is not None]
        if times:
            earliest = min(times)
            earliest_utc = _naive_utc(earliest).replace(tzinfo=timezone.utc)
            return int(earliest_utc.timestamp() * 1000) - int(buffer_seconds * 1000)
        return int((datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).timestamp() * 1000)

    # ---------- 内部 ----------

    async def _load_recent_orders(self, symbol: str, window_seconds: float) -> list[dict[str, Any]]:
        """窗口内实盘订单(client_order_id / exchange_order_id / filled_quantity / created_at)

        V11.1(P0-1): 由「取最近 200 行再在 Python 里按时间过滤」改为 SQL 直接按
        created_at >= cutoff 过滤, 去掉硬编码 200 行上限(高频场景下不再漏掉窗口内更早订单)。
        """
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        cutoff = _naive_utc(datetime.now(timezone.utc)) - timedelta(seconds=window_seconds)
        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(Order)
                        .where(
                            Order.symbol == symbol,
                            Order.is_paper.is_(False),
                            Order.created_at >= cutoff,
                        )
                        .order_by(Order.id.desc())
                    )
                ).scalars().all()
                return [{
                    "client_order_id": r.client_order_id,
                    "exchange_order_id": r.exchange_order_id,
                    "filled_quantity": r.filled_quantity,
                    "created_at": r.created_at,
                } for r in rows]
        except Exception:
            self.logger.exception("交易所真相对账加载订单失败")
            return []

    async def _load_all_eids(self, symbol: str) -> set[str]:
        """全部实盘订单的交易所订单ID(孤儿成交检测用)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(Order.exchange_order_id).where(
                            Order.symbol == symbol,
                            Order.is_paper.is_(False),
                            Order.exchange_order_id.is_not(None),
                        )
                    )
                ).scalars().all()
                return {r for r in rows if r}
        except Exception:
            self.logger.exception("交易所真相对账加载订单ID失败")
            return set()
