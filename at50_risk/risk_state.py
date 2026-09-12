"""
风险状态机(V10.5 / V10.6)

显式风险状态 NORMAL(正常) / REDUCE_ONLY(仅减仓) / PAUSED(暂停) / KILLED(急停),
取代 RiskManager 里隐式的时间阈值暂停(`_anomaly_until` / `_anomaly_reason` /
`_last_pause_reason`)。状态迁移集中一处, 每次进入 PAUSED/REDUCE_ONLY 由调用方落
RiskEvent 审计。

方向闸门:
  can_buy  = NORMAL(仅正常态可开新仓)
  can_sell = NORMAL 或 REDUCE_ONLY(仅减仓态仍可卖出减仓)

迁移规则:
  NORMAL      -> PAUSED       异常触发(时间窗自动恢复)
  PAUSED      -> PAUSED       异常续期(延长时间窗, 同因不重复告警)
  PAUSED      -> NORMAL       时间窗到期自动恢复
  NORMAL/PAUSED -> REDUCE_ONLY 进入仅减仓(不自动恢复, recover/reset 退出)
  *           -> KILLED       急停(不自动恢复)
  KILLED      -> RECOVERY_CHECK   reset 进入恢复核验(仍不可交易, 禁止裸 reset 到 NORMAL)
  RECOVERY_CHECK -> NORMAL    confirm_recovered(对账确认一致后的人工确认)
  RECOVERY_CHECK 冻结: pause/reduce_only 不降级(与 KILLED 同, 须人工 confirm)
"""

import time
from enum import Enum
from typing import Any

from at01_common.logger import LoggerMixin


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    PAUSED = "PAUSED"
    KILLED = "KILLED"
    RECOVERY_CHECK = "RECOVERY_CHECK"


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
        if self._state in (RiskState.KILLED, RiskState.RECOVERY_CHECK):
            return False  # 急停/恢复核验优先, 不再降级(冻结须人工确认)
        changed = self._state is not RiskState.PAUSED
        new_reason = self._reason != reason
        self._state = RiskState.PAUSED
        self._paused_until = time.time() + self.pause_seconds
        self._reason = reason
        return changed or new_reason

    def reduce_only(self, reason: str) -> bool:
        """进入仅减仓态(NORMAL/PAUSED -> REDUCE_ONLY, 不自动恢复)。

        返回 True 表示发生状态切换(供告警去重)。KILLED / RECOVERY_CHECK 优先不降级。
        """
        self._tick()
        if self._state in (RiskState.KILLED, RiskState.RECOVERY_CHECK):
            return False
        changed = self._state is not RiskState.REDUCE_ONLY
        self._state = RiskState.REDUCE_ONLY
        self._paused_until = 0.0
        self._reason = reason
        return changed

    def recover(self) -> None:
        """从 PAUSED / REDUCE_ONLY 手动恢复(不覆盖 KILLED)"""
        if self._state in (RiskState.PAUSED, RiskState.REDUCE_ONLY):
            self._state = RiskState.NORMAL
            self._paused_until = 0.0
            self._reason = ""

    def kill(self, reason: str) -> None:
        """任何状态 -> KILLED(不自动恢复)"""
        self._state = RiskState.KILLED
        self._reason = reason
        self._paused_until = 0.0

    def reset(self) -> None:
        """KILLED -> RECOVERY_CHECK(禁止裸 reset 到 NORMAL)

        V10.7: 急停解除需两步 —— 先 reset 进入恢复核验(仍不可交易),
        待对账确认一致后再 confirm_recovered() 回到 NORMAL。非 KILLED 态调用无副作用。
        """
        if self._state is RiskState.KILLED:
            self._state = RiskState.RECOVERY_CHECK
            self._paused_until = 0.0
            # reason 保留, 供核验阶段审计追溯(不清空)

    def confirm_recovered(self) -> None:
        """RECOVERY_CHECK -> NORMAL(对账确认一致后的人工确认)"""
        if self._state is RiskState.RECOVERY_CHECK:
            self._state = RiskState.NORMAL
            self._reason = ""
            self._paused_until = 0.0

    # ---------- 闸门 ----------

    def is_paused(self) -> bool:
        self._tick()
        return self._state is RiskState.PAUSED

    def is_reduce_only(self) -> bool:
        return self._state is RiskState.REDUCE_ONLY

    def is_killed(self) -> bool:
        return self._state is RiskState.KILLED

    def is_recovery_check(self) -> bool:
        return self._state is RiskState.RECOVERY_CHECK

    def can_trade(self) -> bool:
        """完全可交易(开新仓 + 减仓)"""
        return self.state is RiskState.NORMAL

    def can_buy(self) -> bool:
        """可开新仓: 仅 NORMAL"""
        return self.state is RiskState.NORMAL

    def can_sell(self) -> bool:
        """可卖出(减仓): NORMAL 或 REDUCE_ONLY"""
        return self.state in (RiskState.NORMAL, RiskState.REDUCE_ONLY)

    def block_reason(self) -> str:
        self._tick()
        if self._state is RiskState.KILLED:
            return f"急停中: {self._reason}"
        if self._state is RiskState.RECOVERY_CHECK:
            return f"恢复核验中: {self._reason}"
        if self._state is RiskState.PAUSED:
            return f"异常保护: {self._reason}"
        if self._state is RiskState.REDUCE_ONLY:
            return f"仅减仓: {self._reason}"
        return ""

    def status(self) -> dict[str, Any]:
        return {"state": self.current, "reason": self._reason}
