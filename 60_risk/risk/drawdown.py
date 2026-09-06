"""
回撤控制

跟踪权益高点,计算当前回撤;超过阈值时请求熔断。
"""

from common.config.settings import get_settings
from common.utils.logger import LoggerMixin


class DrawdownController(LoggerMixin):
    """最大回撤控制器"""

    def __init__(self, initial_equity: float | None = None):
        settings = get_settings()
        self.max_drawdown = settings.risk_max_drawdown
        self.peak_equity: float = initial_equity or settings.risk_initial_equity
        self.current_equity: float = self.peak_equity

    def update(self, equity: float) -> tuple[float, bool]:
        """更新权益,返回 (回撤比例, 是否超限)"""
        if equity > self.peak_equity:
            self.peak_equity = equity
        self.current_equity = equity
        drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        return drawdown, drawdown >= self.max_drawdown

    @property
    def drawdown(self) -> float:
        """当前回撤"""
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity

    def status(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "current_equity": self.current_equity,
            "drawdown": round(self.drawdown, 4),
            "max_drawdown": self.max_drawdown,
            "breached": self.drawdown >= self.max_drawdown,
        }
