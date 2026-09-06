"""
分级回撤风险模型(V4.0)

2 万本金长期持有导向, 允许最大回撤 50%, 但不是"到 50% 一次熔断",
而是逐级响应:

    level1  10%  reduce_trade   缩减交易仓活动(网格降频)
    level2  20%  reduce_exposure 敞口目标下调一档
    level3  30%  stop_add       停止一切加仓
    level4  40%  defensive      只保留核心仓下限
    level5  50%  emergency      清交易仓+核心仓降至最低, 熔断冷却

每级响应同时收紧 PositionSize 与 Allocation 的可用系数。
"""

from dataclasses import dataclass
from typing import Optional

from at01_common.logger import LoggerMixin


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
    DrawdownTier(1, 0.10, "reduce_trade", "缩减交易仓活动", size_factor=0.7, grid_enabled=True),
    DrawdownTier(2, 0.20, "reduce_exposure", "敞口下调一档", size_factor=0.5, exposure_factor=0.7),
    DrawdownTier(3, 0.30, "stop_add", "停止加仓", size_factor=0.0, exposure_factor=0.5, add_position=False),
    DrawdownTier(4, 0.40, "defensive", "防御(核心仓下限)", size_factor=0.0, exposure_factor=0.3, add_position=False, grid_enabled=False),
    DrawdownTier(5, 0.50, "emergency", "紧急(熔断)", size_factor=0.0, exposure_factor=0.1, add_position=False, grid_enabled=False),
]


class TieredDrawdownManager(LoggerMixin):
    """分级回撤管理"""

    def __init__(self, hard_breaker=None):
        self.hard_breaker = hard_breaker  # V2 CircuitBreaker(50% 触发)
        self.current_level: int = 0

    def evaluate(self, drawdown: float) -> Optional[DrawdownTier]:
        """按回撤取最高命中档位(升档触发动作, 降档只恢复到当前)"""
        tier = None
        for t in TIERS:
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
            if tier.level == 5 and self.hard_breaker is not None:
                self.hard_breaker.manual_trip(f"回撤{drawdown:.1%} 达紧急档")
        elif tier.level < self.current_level:
            # 部分恢复(只降不升的滞后, 避免在阈值附近抖动: 需低于该档 2% 才降)
            relaxed = TIERS[self.current_level - 1]
            if drawdown < relaxed.threshold - 0.02:
                self.logger.info(
                    "回撤降档", level=tier.level, drawdown=f"{drawdown:.1%}"
                )
                self.current_level = tier.level

        return TIERS[self.current_level - 1] if self.current_level > 0 else None

    @property
    def active_tier(self) -> Optional[DrawdownTier]:
        return TIERS[self.current_level - 1] if self.current_level > 0 else None

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
