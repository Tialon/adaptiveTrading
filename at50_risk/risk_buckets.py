"""
Bucket Position Manager(V4.0)

核心仓(core) / 交易仓(trade) 双仓管理:

- 核心仓: 长期持有, 只随 regime 敞口调整(牛市 70%/熊市压缩), 日常交易不触碰
- 交易仓: 网格/评分策略的高抛低吸区间, 卖出只动交易仓
- 跨仓约束: 交易仓不足时才允许(经风控批准)借用核心仓, 默认禁止

总账(持仓/已实现盈亏)由 ExecutionEngine 统一经 PortfolioEngine 记账;
本模块仅维护双仓拆分与落库(position_bucket 表), 不再同步总账(避免重复记账)。
"""

from typing import Any

from at01_common.logger import LoggerMixin
from at50_risk.risk_position import PositionManager

CORE = "core"
TRADE = "trade"


class BucketPositionManager(LoggerMixin):
    """双仓管理器"""

    def __init__(self, positions: PositionManager):
        self.positions = positions  # 总账
        self.buckets: dict[str, dict[str, float]] = {}  # symbol -> {core: qty, trade: qty}
        self.avg_cost: dict[str, dict[str, float]] = {}  # symbol -> {core: cost, trade: cost}

    # ---------- 查询 ----------

    def get_bucket(self, symbol: str, bucket_type: str) -> float:
        return self.buckets.get(symbol, {}).get(bucket_type, 0.0)

    def total(self, symbol: str) -> float:
        b = self.buckets.get(symbol, {})
        return b.get(CORE, 0.0) + b.get(TRADE, 0.0)

    def core(self, symbol: str) -> float:
        return self.get_bucket(symbol, CORE)

    def trade(self, symbol: str) -> float:
        return self.get_bucket(symbol, TRADE)

    def view(self, symbol: str) -> dict[str, Any]:
        b = self.buckets.get(symbol, {})
        c = self.avg_cost.get(symbol, {})
        return {
            "symbol": symbol,
            "core_qty": b.get(CORE, 0.0),
            "trade_qty": b.get(TRADE, 0.0),
            "total_qty": self.total(symbol),
            "core_avg_cost": c.get(CORE, 0.0),
            "trade_avg_cost": c.get(TRADE, 0.0),
        }

    # ---------- 记账钩子(执行引擎成交后调用) ----------

    def on_buy_fill(self, symbol: str, qty: float, price: float, bucket: str = TRADE) -> bool:
        """买入成交入指定仓(默认交易仓; 核心仓只经 allocation 引导)"""
        if bucket not in (CORE, TRADE):
            return False
        # 仅维护双仓拆分(总账由 ExecutionEngine 统一记账)
        self.buckets.setdefault(symbol, {CORE: 0.0, TRADE: 0.0})
        old_qty = self.buckets[symbol][bucket]
        old_cost = self.avg_cost.setdefault(symbol, {CORE: 0.0, TRADE: 0.0})[bucket]
        new_cost = (old_qty * old_cost + qty * price) / (old_qty + qty) if (old_qty + qty) > 0 else 0.0
        self.buckets[symbol][bucket] = old_qty + qty
        self.avg_cost[symbol][bucket] = new_cost
        self.logger.info("双仓买入", symbol=symbol, bucket=bucket, qty=qty, price=price)
        return True

    def on_sell_fill(self, symbol: str, qty: float, price: float, bucket: str = TRADE) -> tuple[float, str]:
        """卖出成交: 优先交易仓; 交易仓不足 -> (可选)动用核心仓

        返回 (已实现盈亏, 实际动用的仓)
        """
        # 计算两仓可卖
        trade_qty = self.get_bucket(symbol, TRADE)
        core_qty = self.get_bucket(symbol, CORE)

        used_bucket = bucket
        if bucket == TRADE:
            if qty <= trade_qty:
                sell_trade = qty
                sell_core = 0.0
            else:
                # 交易仓不足: 默认禁止动核心仓(返回失败让上层决策)
                self.logger.warning(
                    "交易仓不足,拒绝跨仓卖出", symbol=symbol,
                    need=qty, trade_qty=trade_qty, core_qty=core_qty,
                )
                return 0.0, "REJECTED"
        else:  # 显式卖核心仓(只允许 allocation 减仓)
            sell_trade = 0.0
            sell_core = min(qty, core_qty)
            used_bucket = CORE

        if bucket == TRADE:
            sell_trade = qty
            sell_core = 0.0

        # 成本
        costs = self.avg_cost.setdefault(symbol, {CORE: 0.0, TRADE: 0.0})
        realized = 0.0
        if sell_trade > 0:
            cost = costs[TRADE]
            realized += (price - cost) * sell_trade
            self.buckets[symbol][TRADE] = trade_qty - sell_trade
        if sell_core > 0:
            cost = costs[CORE]
            realized += (price - cost) * sell_core
            self.buckets[symbol][CORE] = core_qty - sell_core

        # 仅维护双仓拆分(总账由 ExecutionEngine 统一记账)
        return realized, used_bucket

    # ---------- 持久化 ----------

    async def load_from_db(self) -> None:
        """启动加载双仓"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionBucket

        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(select(PositionBucket))).scalars().all()
                for r in rows:
                    self.buckets.setdefault(r.symbol, {CORE: 0.0, TRADE: 0.0})
                    self.buckets[r.symbol][r.bucket_type] = r.quantity
                    self.avg_cost.setdefault(r.symbol, {CORE: 0.0, TRADE: 0.0})
                    self.avg_cost[r.symbol][r.bucket_type] = r.avg_cost
            self.logger.info("双仓已加载", symbols=list(self.buckets.keys()))
        except Exception:
            self.logger.exception("双仓加载失败")

    async def persist(self, symbol: str) -> None:
        """落库双仓"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionBucket

        b = self.buckets.get(symbol)
        if b is None:
            return
        try:
            async with AsyncSessionLocal() as session:
                for bucket_type, qty in b.items():
                    row = (
                        await session.execute(
                            select(PositionBucket).where(
                                PositionBucket.symbol == symbol,
                                PositionBucket.bucket_type == bucket_type,
                            )
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = PositionBucket(symbol=symbol, bucket_type=bucket_type)
                        session.add(row)
                    row.quantity = qty
                    row.avg_cost = self.avg_cost.get(symbol, {}).get(bucket_type, 0.0)
                await session.commit()
        except Exception:
            self.logger.exception("双仓持久化失败", symbol=symbol)
