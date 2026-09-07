"""
风险状态机(V10.5)

显式风险状态 NORMAL(正常) / PAUSED(暂停) / KILLED(急停), 取代 RiskManager 里
隐式的时间阈值暂停(`_anomaly_until` / `_anomaly_reason` / `_last_pause_reason`)。
状态迁移集中一处, 每次进入 PAUSED 由调用方落 RiskEvent 审计。

迁移规则:
  NORMAL -> PAUSED   异常触发(时间窗自动恢复)
  PAUSED -> PAUSED   异常续期(延长时间窗, 同因不重复告警)
  PAUSED -> NORMAL   时间窗到期自动恢复
  *      -> KILLED   急停(不自动恢复)
  KILLED -> NORMAL   人工 reset
"""

import time
from enum import Enum
from typing import Any

from at01_common.logger import LoggerMixin


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    PAUSED = "PAUSED"
    KILLED = "KILLED"


class RiskStateMachine(LoggerMixin):
    """显式风险状态机(纯内存, 无持久化; 急停持久化由 KillSwitch 负责)。"""

    def __init__(self, pause_seconds: float = 60.0):
        self.pause_seconds = pause_seconds
        self._state: RiskState = RiskState.NORMAL
        self._paused_until: float = 0.0
        self._reason: str = ""

    # ---------- 读取 ----------

    @property
    def state(self) -> RiskState:
        """当前状态(读前先结算时间窗到期)"""
        self._tick()
        return self._state

    @property
    def current(self) -> str:
        return self.state.value

    @property
    def reason(self) -> str:
        self._tick()
        return self._reason

    def _tick(self) -> None:
        """PAUSED 时间窗到期自动恢复 NORMAL(幂等, 记一次解除日志)"""
        if (
            self._state is RiskState.PAUSED
            and self._paused_until > 0
            and time.time() >= self._paused_until
        ):
            self.logger.info("异常保护解除", reason=self._reason)
            self._state = RiskState.NORMAL
            self._paused_until = 0.0
            self._reason = ""

    # ---------- 操作 ----------

    def pause(self, reason: str) -> bool:
        """进入/续期暂停。返回 True 表示发生状态切换或原因变化(供告警去重)。"""
        self._tick()
        if self._state is RiskState.KILLED:
            return False  # 急停优先, 不再降级
        changed = self._state is not RiskState.PAUSED
        new_reason = self._reason != reason
        self._state = RiskState.PAUSED
        self._paused_until = time.time() + self.pause_seconds
        self._reason = reason
        return changed or new_reason

    def recover(self) -> None:
        """从 PAUSED 手动恢复(不覆盖 KILLED)"""
        if self._state is RiskState.PAUSED:
            self._state = RiskState.NORMAL
            self._paused_until = 0.0
            self._reason = ""

    def kill(self, reason: str) -> None:
        """任何状态 -> KILLED(不自动恢复)"""
        self._state = RiskState.KILLED
        self._reason = reason
        self._paused_until = 0.0

    def reset(self) -> None:
        """KILLED -> NORMAL(人工恢复)"""
        self._state = RiskState.NORMAL
        self._reason = ""
        self._paused_until = 0.0

    # ---------- 闸门 ----------

    def is_paused(self) -> bool:
        self._tick()
        return self._state is RiskState.PAUSED

    def is_killed(self) -> bool:
        return self._state is RiskState.KILLED

    def can_trade(self) -> bool:
        return self.state is RiskState.NORMAL

    def block_reason(self) -> str:
        self._tick()
        if self._state is RiskState.KILLED:
            return f"急停中: {self._reason}"
        if self._state is RiskState.PAUSED:
            return f"异常保护: {self._reason}"
        return ""

    def status(self) -> dict[str, Any]:
        return {"state": self.current, "reason": self._reason}
