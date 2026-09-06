"""
熔断器

触发条件(任一):
- 最大回撤超限
- 日内亏损超限

触发后进入冷却期,期间拒绝所有开仓信号;冷却结束后恢复。
"""

import time

from common.config.settings import get_settings
from common.utils.logger import LoggerMixin


class CircuitBreaker(LoggerMixin):
    """交易熔断器"""

    def __init__(self):
        settings = get_settings()
        self.daily_loss_limit = settings.risk_daily_loss_limit
        self.cooldown_seconds = settings.risk_cooldown_seconds
        self.initial_equity = settings.risk_initial_equity

        self._tripped: bool = False
        self._trip_reason: str = ""
        self._trip_time: float = 0.0
        self._cooldown_until: float = 0.0

        # 日内统计
        self.current_equity: float = self.initial_equity
        self._day_start: int = time.localtime().tm_yday
        self._day_start_equity: float = self.initial_equity

    # ---------- 状态 ----------

    @property
    def is_open(self) -> bool:
        """熔断是否生效中"""
        if not self._tripped:
            return False
        if time.time() >= self._cooldown_until:
            self.logger.info("熔断冷却结束,恢复交易", reason=self._trip_reason)
            self._tripped = False
            self._trip_reason = ""
            return False
        return True

    @property
    def reason(self) -> str:
        return self._trip_reason

    @property
    def remaining_seconds(self) -> int:
        if not self._tripped:
            return 0
        return max(0, int(self._cooldown_until - time.time()))

    # ---------- 检查 ----------

    def check_drawdown(self, drawdown: float) -> bool:
        """回撤超限检查,返回是否触发熔断"""
        settings = get_settings()
        if drawdown >= settings.risk_max_drawdown:
            self._trip(f"最大回撤 {drawdown:.2%} >= {settings.risk_max_drawdown:.2%}")
            return True
        return False

    def check_daily_loss(self, equity: float) -> bool:
        """日内亏损检查,返回是否触发熔断"""
        self._roll_day()
        day_pnl_ratio = (equity - self._day_start_equity) / self._day_start_equity
        if self._day_start_equity > 0 and day_pnl_ratio <= -self.daily_loss_limit:
            self._trip(f"日内亏损 {day_pnl_ratio:.2%} 超限 -{self.daily_loss_limit:.2%}")
            return True
        return False

    def manual_trip(self, reason: str = "manual") -> None:
        """手动熔断"""
        self._trip(f"手动熔断: {reason}")

    def reset(self) -> None:
        """手动解除"""
        self._tripped = False
        self._trip_reason = ""
        self._cooldown_until = 0.0
        self._day_start_equity = self.current_equity
        self.logger.info("熔断器已手动重置")

    # ---------- 内部 ----------

    def _trip(self, reason: str) -> None:
        """触发熔断"""
        if self._tripped:
            return
        self._tripped = True
        self._trip_reason = reason
        self._trip_time = time.time()
        self._cooldown_until = time.time() + self.cooldown_seconds
        self.logger.error("熔断触发", reason=reason, cooldown=self.cooldown_seconds)

    def _roll_day(self) -> None:
        """跨日重置日内基准"""
        today = time.localtime().tm_yday
        if today != self._day_start:
            self._day_start = today
            self._day_start_equity = self.current_equity
            self.logger.info("新交易日,重置日内盈亏基准")

    def status(self) -> dict:
        return {
            "open": self.is_open,
            "reason": self._trip_reason,
            "remaining_seconds": self.remaining_seconds,
            "day_start_equity": self._day_start_equity,
            "daily_loss_limit": self.daily_loss_limit,
        }
