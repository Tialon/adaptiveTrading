"""
网格策略:区间内低买高卖

- 以启动时价格为中枢,按上下边界与网格数划分价位
- 价格下穿未持仓网格线 -> 买入该层
- 价格上穿已持仓网格线 -> 卖出该层(赚层差)
- 峰值跟踪价格突破边界后自动重设网格(可选)
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from at20_analytics.engine import MarketAnalytics
from at01_common.settings import get_settings
from at30_strategy.strategy_base import BaseStrategy, Signal, SignalSide


@dataclass
class GridLevel:
    """网格层"""

    index: int
    price: float
    held: bool = False
    buy_price: float = 0.0


@dataclass
class GridState:
    """网格状态"""

    symbol: str
    upper: float
    lower: float
    count: int
    per_level_quote: float  # 每格金额
    levels: list[GridLevel] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def step(self) -> float:
        return (self.upper - self.lower) / self.count if self.count > 0 else 0.0


class GridStrategy(BaseStrategy):
    """网格策略"""

    name = "grid"

    def __init__(self, symbols: Optional[list[str]] = None):
        super().__init__(symbols)
        settings = get_settings()
        self.upper_pct = settings.grid_upper_pct
        self.lower_pct = settings.grid_lower_pct
        self.count = settings.grid_count
        # V2.0: 单层金额 = 单笔限额(百分比计算)/层数,无配置时给保底值
        single_quote = settings.risk_max_single_order_quote
        if single_quote <= 0:
            single_quote = settings.risk_initial_equity * settings.risk_max_single_order_pct
        self.per_level_quote = single_quote / max(1, settings.grid_count) * 2
        self.signal_cooldown = 0.5  # 网格需要高频响应

        self._grids: dict[str, GridState] = {}

    # ---------- 网格管理 ----------

    def ensure_grid(self, symbol: str, price: float) -> GridState:
        """无网格时以当前价为中枢建立"""
        grid = self._grids.get(symbol)
        if grid is not None:
            return grid
        return self.reset_grid(symbol, price)

    def reset_grid(self, symbol: str, price: float) -> GridState:
        """重设网格"""
        # V8: AI 审批通过的网格间距覆盖上下边界(对称网格)
        from at30_strategy.ai_parameter_guard import RuntimeParams

        spacing = RuntimeParams.get("grid_spacing")
        if spacing is not None:
            upper_pct = lower_pct = spacing
        else:
            upper_pct = self.upper_pct
            lower_pct = self.lower_pct
        upper = price * (1 + upper_pct)
        lower = price * (1 - lower_pct)
        step = (upper - lower) / self.count
        levels = [GridLevel(index=i, price=lower + step * i) for i in range(self.count + 1)]
        grid = GridState(
            symbol=symbol,
            upper=upper,
            lower=lower,
            count=self.count,
            per_level_quote=self.per_level_quote,
            levels=levels,
        )
        self._grids[symbol] = grid
        self.logger.info(
            "网格建立",
            symbol=symbol,
            lower=round(lower, 2),
            upper=round(upper, 2),
            count=self.count,
        )
        return grid

    def get_grid(self, symbol: str) -> Optional[GridState]:
        return self._grids.get(symbol)

    # ---------- 信号生成 ----------

    def on_market(self, a: MarketAnalytics) -> list[Signal]:
        if a.symbol not in self.symbols or a.price <= 0:
            return []

        grid = self.ensure_grid(a.symbol, a.price)

        # 价格越界重设(超出边界一个网格步长才重设,避免底边买入区间被误杀)
        step0 = grid.step
        if a.price > grid.upper + step0 or a.price < grid.lower - step0:
            self.logger.info(
                "价格越界,重设网格", symbol=a.symbol, price=a.price,
                upper=grid.upper, lower=grid.lower,
            )
            grid = self.reset_grid(a.symbol, a.price)

        signals: list[Signal] = []
        step = grid.step
        if step <= 0:
            return signals

        for level in grid.levels:
            if level.price <= 0:
                continue
            # 下穿买入:价格低于层价一个步长以内
            if not level.held and a.price < level.price and a.price > level.price - step:
                level.held = True
                level.buy_price = a.price
                signals.append(
                    Signal(
                        symbol=a.symbol,
                        strategy=self.name,
                        side=SignalSide.BUY,
                        price=a.price,
                        quote_amount=grid.per_level_quote,
                        reason=[f"网格L{level.index}买入: 价格下穿{level.price:.2f}"],
                        score=60.0,
                        indicators={
                            "grid_lower": grid.lower, "grid_upper": grid.upper,
                            "grid_step": step, "level": level.index, "level_price": level.price,
                        },
                    )
                )
            # 上穿卖出:持有且价格高于层价(回到该层上方)
            elif level.held and a.price > level.price + step * 0.5:
                level.held = False
                signals.append(
                    Signal(
                        symbol=a.symbol,
                        strategy=self.name,
                        side=SignalSide.SELL,
                        price=a.price,
                        quote_amount=grid.per_level_quote,
                        reason=[f"网格L{level.index}卖出: 价格回升{level.price:.2f}上方"],
                        score=60.0,
                        indicators={
                            "grid_lower": grid.lower, "grid_upper": grid.upper,
                            "grid_step": step, "level": level.index, "level_price": level.price,
                        },
                    )
                )

        return signals

    # ---------- 持仓对账 ----------

    def sync_position(self, symbol: str, quantity: float, avg_price: float) -> None:
        """根据实际持仓修正网格持有标记"""
        grid = self._grids.get(symbol)
        if grid is None:
            return
        total_levels = sum(1 for lv in grid.levels if lv.held)
        # 简单对账:持仓量为0时清空所有标记
        if quantity <= 0 and total_levels > 0:
            for lv in grid.levels:
                lv.held = False

    def status(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "grids": {
                s: {
                    "lower": g.lower,
                    "upper": g.upper,
                    "count": g.count,
                    "step": g.step,
                    "held_levels": sum(1 for lv in g.levels if lv.held),
                }
                for s, g in self._grids.items()
            },
        }
