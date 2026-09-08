"""
分级回撤风险模型(V12 §19)

分级回撤响应(观察/降险/仅减仓/急停), 逐级收紧而非「一次性熔断」:

    level1   5%  observe      观察(记档, 不动敞口/仓位)
    level2   8%  reduce_risk  降低风险(定仓×0.5, 敞口目标下调)
    level3  12%  reduce_only  仅减仓(禁一切加仓, 保留卖出)
    level4  15%  kill         急停(人工检查; 由 RiskManager.update_equity 持久冻结)

每级响应同时收紧 PositionSize 与 Allocation 的可用系数。阈值与
settings.risk_drawdown_{observe,reduce,pause}_pct / risk_max_drawdown 对齐。
"""

from dataclasses import dataclass
from typing import Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings


@dataclass
class DrawdownTier:
    """回撤档位"""

    level: int
    threshold: float  # 回撤比例
    name: str
    action: str
    # 各模块收紧系数(0~1)
    size_factor: float = 1.0  # PositionSize 乘数
    exposure_factor: float = 1.0  # Allocation 敞口乘数
    grid_enabled: bool = True
    add_position: bool = True


TIERS: list[DrawdownTier] = [
    DrawdownTier(1, 0.05, "observe", "观察(记档不动仓)", size_factor=1.0, exposure_factor=1.0, grid_enabled=True),
    DrawdownTier(2, 0.08, "reduce_risk", "降低风险", size_factor=0.5, exposure_factor=0.7, grid_enabled=True),
    DrawdownTier(3, 0.12, "reduce_only", "仅减仓", size_factor=0.0, exposure_factor=0.5, add_position=False, grid_enabled=False),
    DrawdownTier(4, 0.15, "kill", "急停(人工检查)", size_factor=0.0, exposure_factor=0.1, add_position=False, grid_enabled=False),
]


def _tiers_from_settings() -> list[DrawdownTier]:
    """按 settings 阈值重建档位(名称/动作/系数沿用 TIERS, 阈值取自配置)。

    使 §19 的 5/8/12/15% 可由 .env 调整, 且 kill 档阈值与 risk_max_drawdown 一致,
    避免硬编码 TIERS 与配置漂移。
    """
    s = get_settings()
    thresholds = (
        s.risk_drawdown_observe_pct,
        s.risk_drawdown_reduce_pct,
        s.risk_drawdown_pause_pct,
        s.risk_max_drawdown,
    )
    return [
        DrawdownTier(
            level=t.level,
            threshold=thresholds[t.level - 1],
            name=t.name,
            action=t.action,
            size_factor=t.size_factor,
            exposure_factor=t.exposure_factor,
            grid_enabled=t.grid_enabled,
            add_position=t.add_position,
        )
        for t in TIERS
    ]


class TieredDrawdownManager(LoggerMixin):
    """分级回撤管理"""

    def __init__(self, hard_breaker=None):
        self.hard_breaker = hard_breaker  # V2 CircuitBreaker(50% 触发)
        self.tiers: list[DrawdownTier] = _tiers_from_settings()
        self.current_level: int = 0

    def evaluate(self, drawdown: float) -> Optional[DrawdownTier]:
        """按回撤取最高命中档位(升档触发动作, 降档只恢复到当前)"""
        tier = None
        for t in self.tiers:
            if drawdown >= t.threshold:
                tier = t

        if tier is None:
            if self.current_level > 0:
                self.logger.info("回撤恢复, 解除档位", previous_level=self.current_level, drawdown=f"{drawdown:.1%}")
                self.current_level = 0
            return None

        if tier.level > self.current_level:
            # 升档
            self.logger.warning(
                "回撤档位升级",
                level=tier.level, name=tier.name,
                drawdown=f"{drawdown:.1%}", action=tier.action,
            )
            self.current_level = tier.level
            if tier.level == 4 and self.hard_breaker is not None:
                self.hard_breaker.manual_trip(f"回撤{drawdown:.1%} 达急停档")
        elif tier.level < self.current_level:
            # 部分恢复(只降不升的滞后, 避免在阈值附近抖动: 需低于该档 2% 才降)
            relaxed = self.tiers[self.current_level - 1]
            if drawdown < relaxed.threshold - 0.02:
                self.logger.info(
                    "回撤降档", level=tier.level, drawdown=f"{drawdown:.1%}"
                )
                self.current_level = tier.level

        return self.tiers[self.current_level - 1] if self.current_level > 0 else None

    @property
    def active_tier(self) -> Optional[DrawdownTier]:
        return self.tiers[self.current_level - 1] if self.current_level > 0 else None

    @property
    def size_factor(self) -> float:
        """PositionSize 可用系数"""
        t = self.active_tier
        return t.size_factor if t else 1.0

    @property
    def exposure_factor(self) -> float:
        """Allocation 敞口系数"""
        t = self.active_tier
        return t.exposure_factor if t else 1.0

    def status(self) -> dict:
        t = self.active_tier
        if t is None:
            return {"level": 0, "name": "normal", "action": "-", "size_factor": 1.0, "exposure_factor": 1.0}
        return {
            "level": t.level,
            "name": t.name,
            "action": t.action,
            "threshold": t.threshold,
            "size_factor": t.size_factor,
            "exposure_factor": t.exposure_factor,
            "grid_enabled": t.grid_enabled,
            "add_position": t.add_position,
        }
