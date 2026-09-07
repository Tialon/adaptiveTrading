"""
Portfolio Manager(V9.0) — 组合编排薄层

SOL Adaptive Swing Trader 的组合单一入口: 核心 / 交易 / 现金 三桶模型。

职责(薄编排, 复用现有原语, 不重写账务):
- 目标仓位: 按总权益 × 三比例(portfolio_core_ratio / trading_ratio / cash_ratio)
- 再平衡建议: 目标 - 当前 = core_diff / trade_diff(供核心仓/交易仓调整)
- 目标落库: 写入 position_bucket 的 target_ratio / target_quantity / current_value

账务仍由 ExecutionEngine → PortfolioEngine 统一记账; BucketPositionManager 仍是纯拆分跟踪。
"""

from typing import Any

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings
from at60_risk.risk_buckets import BucketPositionManager, CORE, TRADE
from at60_risk.risk_position import PositionManager


class PortfolioManager(LoggerMixin):
    """组合编排层(核心/交易/现金三桶)"""

    def __init__(self, positions: PositionManager, buckets: BucketPositionManager):
        self.settings = get_settings()
        self.positions = positions
        self.buckets = buckets

    # ---------- 目标(按总权益三桶) ----------

    def target_cash(self, equity: float) -> float:
        """目标现金(USDT)"""
        return equity * self.settings.portfolio_cash_ratio

    def target_core_qty(self, equity: float, price: float) -> float:
        """核心仓目标数量"""
        if price <= 0:
            return 0.0
        return equity * self.settings.portfolio_core_ratio / price

    def target_trade_qty(self, equity: float, price: float) -> float:
        """交易仓目标数量"""
        if price <= 0:
            return 0.0
        return equity * self.settings.portfolio_trading_ratio / price

    # ---------- 再平衡评估 ----------

    def evaluate(self, symbol: str, equity: float, price: float) -> dict[str, Any]:
        """组合评估: 目标 vs 当前 -> 偏离量"""
        t_core = self.target_core_qty(equity, price)
        t_trade = self.target_trade_qty(equity, price)
        cur_core = self.buckets.core(symbol)
        cur_trade = self.buckets.trade(symbol)
        return {
            "symbol": symbol,
            "target_core_qty": t_core,
            "target_trade_qty": t_trade,
            "target_cash": self.target_cash(equity),
            "current_core_qty": cur_core,
            "current_trade_qty": cur_trade,
            "core_diff": t_core - cur_core,
            "trade_diff": t_trade - cur_trade,
        }

    # ---------- 落库 ----------

    async def persist_targets(self, symbol: str, equity: float, price: float) -> None:
        """把目标比例/数量/当前市值写入 position_bucket(观测用)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import PositionBucket

        ev = self.evaluate(symbol, equity, price)
        targets = {
            CORE: (self.settings.portfolio_core_ratio, ev["target_core_qty"]),
            TRADE: (self.settings.portfolio_trading_ratio, ev["target_trade_qty"]),
        }
        try:
            async with AsyncSessionLocal() as session:
                for bucket_type, (ratio, tqty) in targets.items():
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
                    row.target_ratio = ratio
                    row.target_quantity = tqty
                    row.current_value = self.buckets.get_bucket(symbol, bucket_type) * price
                await session.commit()
        except Exception:
            self.logger.exception("组合目标落库失败", symbol=symbol)
