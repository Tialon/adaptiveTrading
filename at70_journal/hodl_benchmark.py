"""HODL 基准(V12 §24-25)

接管时刻冻结「初始权益 / 初始 SOL 数量 / 初始 SOL 价格」三条基线, 之后每日计算
Adaptive(实际)/ HODL(买入持有)/ Cash(全现金)三种策略的权益, 并给出 Alpha =
Adaptive - HODL, 作为「低买高卖策略是否跑赢简单持有」的对标。

基线只记一次、冻结不可覆盖(§24: 接管时刻记录); 由 §10 只读接管(账户快照)写入真实
交易所权益, 本模块只负责「记录 + 读取 + 计算」。计算为纯函数, 不查交易所、不落库。

数学定义(接管时刻 U0 现金 + Q0 SOL, 价 P0):
    equity0      = U0 + Q0*P0
    hodl_equity  = U0 + Q0*P(t) = equity0 + Q0*(P(t) - P0)   # 原仓不动
    cash_equity  = equity0                                    # 全部持现金
    adaptive     = 当前真实权益
    alpha        = adaptive - hodl_equity
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass(frozen=True)
class BenchmarkResult:
    """一次基准计算结果(只读)。"""

    initial_equity: float
    initial_sol_qty: float
    initial_sol_price: float
    current_price: float
    adaptive_equity: float
    hodl_equity: float
    cash_equity: float
    alpha: float

    @property
    def adaptive_pnl(self) -> float:
        return self.adaptive_equity - self.initial_equity

    @property
    def hodl_pnl(self) -> float:
        return self.hodl_equity - self.initial_equity

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_equity": self.initial_equity,
            "initial_sol_qty": self.initial_sol_qty,
            "initial_sol_price": self.initial_sol_price,
            "current_price": self.current_price,
            "adaptive_equity": self.adaptive_equity,
            "hodl_equity": self.hodl_equity,
            "cash_equity": self.cash_equity,
            "alpha": self.alpha,
            "adaptive_pnl": self.adaptive_pnl,
            "hodl_pnl": self.hodl_pnl,
        }


def compute_benchmark(
    baseline: dict[str, Any], current_equity: float, current_price: float
) -> Optional[BenchmarkResult]:
    """纯函数: 由基线 + 当前真实权益 + 当前价, 计算三策略权益与 Alpha。

    基线 equity0 <= 0 或 SOL 数量 < 0 视为无效, 返回 None(不抛)。
    """
    initial_equity = float(baseline.get("initial_equity", 0.0))
    initial_sol_qty = float(baseline.get("initial_sol_qty", 0.0))
    initial_sol_price = float(baseline.get("initial_sol_price", 0.0))
    if initial_equity <= 0 or initial_sol_qty < 0:
        return None

    initial_cash = initial_equity - initial_sol_qty * initial_sol_price
    hodl_equity = initial_cash + initial_sol_qty * current_price
    return BenchmarkResult(
        initial_equity=initial_equity,
        initial_sol_qty=initial_sol_qty,
        initial_sol_price=initial_sol_price,
        current_price=current_price,
        adaptive_equity=current_equity,
        hodl_equity=hodl_equity,
        cash_equity=initial_equity,
        alpha=current_equity - hodl_equity,
    )


class HodlBenchmark(LoggerMixin):
    """HODL 基准记录器 + 计算器(基线单行持久化, 冻结不可覆盖)。"""

    def __init__(self, symbol: str = "SOLUSDT"):
        self.symbol = symbol
        self._baseline: Optional[dict[str, Any]] = None

    # ---------- 基线持久化 ----------

    async def record_baseline(self, equity: float, sol_qty: float, sol_price: float) -> bool:
        """记录基线(只在尚未记录时写入, 冻结不可覆盖)。

        返回 True 表示本次已写入; False 表示已有基线、拒绝覆盖(幂等冻结)。
        数据异常(equity<=0 / sol_qty<0 / sol_price<=0)返回 False 并告警。
        """
        if equity <= 0 or sol_qty < 0 or sol_price <= 0:
            self.logger.error(
                "HODL 基线数据异常, 拒绝写入",
                symbol=self.symbol, equity=equity, sol_qty=sol_qty, sol_price=sol_price,
            )
            return False

        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import HodlBenchmarkState

        try:
            async with AsyncSessionLocal() as session:
                row = (await session.execute(select(HodlBenchmarkState))).scalars().first()
                if row is not None:
                    self.logger.warning(
                        "HODL 基线已存在, 冻结不可覆盖",
                        symbol=row.symbol, initial_equity=row.initial_equity,
                    )
                    return False
                session.add(
                    HodlBenchmarkState(
                        symbol=self.symbol,
                        initial_equity=equity,
                        initial_sol_qty=sol_qty,
                        initial_sol_price=sol_price,
                    )
                )
                await session.commit()
            self._baseline = {
                "initial_equity": equity,
                "initial_sol_qty": sol_qty,
                "initial_sol_price": sol_price,
            }
            self.logger.info(
                "HODL 基线已冻结记录",
                symbol=self.symbol, initial_equity=equity,
                initial_sol_qty=sol_qty, initial_sol_price=sol_price,
            )
            return True
        except Exception:
            self.logger.exception("HODL 基线记录失败")
            return False

    async def load_baseline(self) -> Optional[dict[str, Any]]:
        """读取已记录的基线(缓存; 无基线返回 None)。"""
        if self._baseline is not None:
            return self._baseline

        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import HodlBenchmarkState

        try:
            async with AsyncSessionLocal() as session:
                row = (await session.execute(select(HodlBenchmarkState))).scalars().first()
        except Exception:
            self.logger.exception("HODL 基线读取失败")
            return None
        if row is None:
            return None
        self._baseline = {
            "initial_equity": row.initial_equity,
            "initial_sol_qty": row.initial_sol_qty,
            "initial_sol_price": row.initial_sol_price,
        }
        return self._baseline

    async def has_baseline(self) -> bool:
        """是否已有基线(未记录则 False)。"""
        return (await self.load_baseline()) is not None

    # ---------- 计算 ----------

    async def evaluate(self, current_equity: float, current_price: float) -> Optional[BenchmarkResult]:
        """加载基线并计算当前基准(无基线返回 None)。"""
        baseline = await self.load_baseline()
        if baseline is None:
            return None
        return compute_benchmark(baseline, current_equity, current_price)
