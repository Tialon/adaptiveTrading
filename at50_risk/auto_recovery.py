"""自动恢复编排(V13 P0)。

**这个模块解决什么**: 系统此前只能**冻结**, 不能**自愈**。关键后台任务崩一次就要有人
去重启进程 —— 这正是「必须有人值守」的头号原因。任务书要求:

    KILL → 自动进入 RECOVERY_CHECK → 重新连接 → 重新获取账户 → 重新对账
         → 重新检查行情 → 重新检查风控 → 全部正常 → 自动恢复

**但不是所有冻结都该自愈**。任务书同时明确:

    对「人工主动 KILL」与「重大资金异常 KILL」保留人工恢复确认。

所以本模块的核心不是「自动解冻」, 而是**判断这一次冻结该不该由系统自己解开**:

| 来源 | 能否自愈 | 为什么 |
|---|---|---|
| `MANUAL`(人工急停 / 启动守卫拦截) | ❌ | 人按的按钮, 由人 unpin |
| `AUTO_EQUITY`(回撤/权益异常) | ❌ | 重大资金异常 —— 系统无法自证账本没错 |
| `AUTO_ACCOUNTING`(本地记账失败) | ❌ | 同上: 账本已经不可信 |
| `AUTO_RECONCILE`(对账矩阵 KILLED) | ❌ | 账户与账本对不上 = 金融状态不明 |
| `AUTO_TASK`(关键任务崩溃) | ✅ | 重启任务即可恢复, 任务跑起来就是自证 |
| `AUTO_DATA`(行情失真) | ✅ | 数据恢复可信即可, 校验通过就是自证 |
| 来源为空 / 无法识别 | ❌ | fail-closed |

**「无法自证」是这里的判据**: 自愈的前提是「条件恢复」这件事**能被系统自己证明**。
任务重启会留下运行中的证据, 行情恢复会留下可信的价格 —— 而「账本与交易所对上了」
在账本本身可疑时无法自证, 只能由人核对 Binance 账户。

**恢复策略统一表**(任务书 P1)也在这里: `classify_recovery()` 把「遇到什么错 →
采取什么策略」收口成一个函数, 免得各处各判一套。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from at01_common.logger import LoggerMixin
from at01_common.operator_events import KIND_ERROR, KIND_RECOVERY, operator_log
from at01_common.operator_narrative import (
    KILL_ORIGIN_MANUAL,
    SELF_HEALABLE_KILL_ORIGINS,
    kill_requires_human,
)
from at50_risk.recovery_flow import perform_recovery


class RecoveryPolicy(str, Enum):
    """遇到异常时采取什么策略(任务书 P1「自动恢复策略」逐条对应)。"""

    RETRY = "RETRY"                  # 瞬态错误 → 重试 + 退避
    DEGRADE = "DEGRADE"              # 反复出错 → 降级(停 BUY 保 SELL)
    PAUSE = "PAUSE"                  # 非致命但阻塞 → 暂停, 冷却后自动复检
    KILL = "KILL"                    # 状态不安全 → 自动急停
    AUTO_RECOVER = "AUTO_RECOVER"    # 条件已恢复 → 自动回到 READY(仅自动来源)
    FREEZE_HUMAN = "FREEZE_HUMAN"    # 金融状态不明 → 保持冻结 + 要人工


# 关键词 → 策略。逐条对应任务书的策略表; 按**具体优先**排列(先匹配到的赢)。
_POLICY_RULES: tuple[tuple[tuple[str, ...], RecoveryPolicy], ...] = (
    (("timeout", "timed out", "超时", "connection", "连接", "断线", "reconnect",
      "temporarily", "暂时", "503", "502"), RecoveryPolicy.RETRY),
    (("crash", "崩溃", "异常退出", "restart", "重启"), RecoveryPolicy.RETRY),
    (("accounting", "记账", "ledger", "账本不一致"), RecoveryPolicy.FREEZE_HUMAN),
    (("equity", "权益", "drift", "漂移", "drawdown", "回撤"), RecoveryPolicy.KILL),
    (("orphan", "exchange_only", "只在交易所", "truth", "真相"), RecoveryPolicy.KILL),
    (("data", "行情", "stale", "陈旧", "gap", "缺口", "invalid", "不可信"),
     RecoveryPolicy.KILL),
)

_DEFAULT_POLICY = RecoveryPolicy.PAUSE


def classify_recovery(error: Any) -> RecoveryPolicy:
    """把一次异常归类到恢复策略(纯函数)。

    **默认是 PAUSE 而不是 RETRY** —— 认不出来的错误先停下来冷却是安全的;
    无脑重试一个未知错误才是危险的。任务书的原则同此:

        系统宁可自己停, 也不要要求用户不断看守。
    """
    text = str(error or "").lower()
    if not text:
        return _DEFAULT_POLICY
    for keywords, policy in _POLICY_RULES:
        if any(k in text for k in keywords):
            return policy
    return _DEFAULT_POLICY


class AutoRecoveryCoordinator(LoggerMixin):
    """在冻结状态下由系统自己尝试解除 —— **仅限可自愈来源**。

    由 `_risk_loop` 每轮调用 `tick()`; 每个 tick 最多推进一次, 且带指数退避,
    避免冻结/解冻之间来回抖动(freeze-thaw thrash)。
    """

    def __init__(
        self,
        risk_manager: Any,
        lifecycle: Any,
        gate: Any,
        *,
        base_interval: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        self.risk_manager = risk_manager
        self.lifecycle = lifecycle
        self.gate = gate
        self.base_interval = base_interval
        self.max_attempts = max_attempts
        self.attempts = 0
        self._next_attempt_at = 0.0
        self._exhausted = False
        self._last_reported_origin = ""

    # ------------------------------------------------------------------ 判定

    def self_healable(self) -> tuple[bool, str]:
        """本次冻结是否允许系统自己解除。返回 `(允许?, 原因)`。"""
        kill = getattr(self.risk_manager, "kill_switch", None)
        if kill is None or not bool(getattr(kill, "is_armed", False)):
            return False, "当前没有被冻结"
        origin = str(getattr(kill, "origin", "") or "")
        if kill_requires_human(origin):
            if origin == KILL_ORIGIN_MANUAL:
                return False, "人工急停必须由人解除"
            return False, f"冻结来源 {origin or '未知'} 属重大资金异常, 需人工核对账户后解除"
        return True, f"冻结来源 {origin} 属系统可自愈类"

    def _backoff_seconds(self) -> float:
        """指数退避: 30s → 60s → 120s … 最多 5 次。"""
        return self.base_interval * (2 ** max(0, self.attempts - 1))

    # ------------------------------------------------------------------ 主循环

    async def tick(self) -> dict[str, Any]:
        """一个恢复周期。返回本次做了什么(供日志/事件流/测试)。"""
        allowed, why = self.self_healable()
        if not allowed:
            self._reset_if_healthy()
            return {"action": "none", "reason": why}

        if self._exhausted:
            return {"action": "exhausted", "reason": "自动恢复尝试次数已用尽, 等待人工处理"}

        now = time.time()
        if now < self._next_attempt_at:
            return {"action": "waiting", "reason": "退避中", "retry_in": round(
                self._next_attempt_at - now, 1)}

        # 前置条件不满足 → 等下一轮(这正是「重新连接/重新对账」发生的时间)
        result = perform_recovery(
            risk_manager=self.risk_manager, lifecycle=self.lifecycle,
            gate=self.gate, force=False,
        )
        if not result.get("ok"):
            self.attempts += 1
            self._next_attempt_at = now + self._backoff_seconds()
            missing = result.get("missing") or []
            if self.attempts >= self.max_attempts:
                self._exhausted = True
                operator_log.emit(
                    KIND_ERROR,
                    f"自动恢复连续 {self.attempts} 次未成功, 需要人工处理",
                    level="ACTION_REQUIRED",
                    detail={"missing": missing, "attempts": self.attempts},
                )
            return {"action": "waiting", "missing": missing, "attempts": self.attempts}

        # 恢复成功
        self.attempts = 0
        self._next_attempt_at = 0.0
        kill = getattr(self.risk_manager, "kill_switch", None)
        if kill is not None:
            await kill.persist()
        operator_log.emit(
            KIND_RECOVERY, "系统已自动恢复(无需人工操作)", level="NORMAL",
            detail={"actor": "auto", "steps": result.get("steps")},
        )
        self.logger.info("自动恢复完成", steps=result.get("steps"))
        return {"action": "recovered", "steps": result.get("steps", [])}

    def _reset_if_healthy(self) -> None:
        """没被冻结时把计数清零 —— 否则一次偶发失败会永久抬高后续的退避。"""
        if self.attempts or self._exhausted or self._next_attempt_at:
            self.attempts = 0
            self._exhausted = False
            self._next_attempt_at = 0.0

    def status(self) -> dict[str, Any]:
        kill = getattr(self.risk_manager, "kill_switch", None)
        return {
            "armed": bool(getattr(kill, "is_armed", False)),
            "origin": str(getattr(kill, "origin", "") or ""),
            "self_healable": self.self_healable()[0],
            "attempts": self.attempts,
            "exhausted": self._exhausted,
            "next_attempt_in": max(0.0, round(self._next_attempt_at - time.time(), 1)),
            "max_attempts": self.max_attempts,
        }


def is_self_healable_origin(origin: str) -> bool:
    """来源是否属于可自愈类(供页面/测试直接问, 不必构造协调器)。"""
    return (origin or "").strip() in SELF_HEALABLE_KILL_ORIGINS
