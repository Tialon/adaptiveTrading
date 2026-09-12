"""V13 P0 自动恢复测试 —— 无人值守的**安全边界**。

任务书同时提了两件方向相反的事:

    KILL → 自动进入 RECOVERY_CHECK → 重新连接 → 重新对账 → … → 全部正常 → 自动恢复
    对「人工主动 KILL」与「重大资金异常 KILL」保留人工恢复确认

所以本文件的重点是**边界**, 不只是功能:

- 可自愈来源(AUTO_TASK / AUTO_DATA)→ 无人干预下回到可交易;
- MANUAL / AUTO_EQUITY / AUTO_ACCOUNTING / AUTO_RECONCILE / 未知来源 → **绝不**自动解除;
- 前置条件不满足 → 原地不动, 且**不消耗**「无限等待」的耐心(有退避与上限);
- 连续失败到上限 → 升级为「需要人工」, 不再空转;
- 恢复期间不产生任何下单动作。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from at01_common.operator_narrative import (
    KILL_ORIGIN_AUTO_ACCOUNTING,
    KILL_ORIGIN_AUTO_EQUITY,
    KILL_ORIGIN_AUTO_RECONCILE,
    KILL_ORIGIN_AUTO_TASK,
    KILL_ORIGIN_MANUAL,
    SELF_HEALABLE_KILL_ORIGINS,
)
from at01_common.runtime_supervisor import RuntimeSupervisor
from at50_risk.auto_recovery import (
    AutoRecoveryCoordinator,
    RecoveryPolicy,
    classify_recovery,
    is_self_healable_origin,
)
from at50_risk.risk_manager import RiskManager
from at50_risk.system_lifecycle import SystemLifecycle
from at50_risk.trading_gate import TradingGate


def _system(*, origin: str, armed: bool = True):
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    lifecycle.warm_up()
    lifecycle.sync()
    lifecycle.self_check()
    lifecycle.ready()
    lifecycle.start_trading()
    if armed:
        rm.kill_switch.arm("测试冻结", origin=origin)
        rm.state_machine.kill("测试冻结")
        lifecycle.enter_safe_mode("测试冻结")
    return rm, lifecycle, gate


def _coordinator(rm: Any, lifecycle: Any, gate: Any, **kw: Any) -> AutoRecoveryCoordinator:
    return AutoRecoveryCoordinator(rm, lifecycle, gate, base_interval=0.0, **kw)


# ---------------------------------------------------------------------------
# 边界: 谁能自愈
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", sorted(SELF_HEALABLE_KILL_ORIGINS))
def test_self_healable_origins_recover_without_a_human(origin: str) -> None:
    """关键任务崩溃 / 行情失真 —— 条件恢复后系统自己起来, **无人操作**。"""
    rm, lifecycle, gate = _system(origin=origin)
    coord = _coordinator(rm, lifecycle, gate)

    outcome = asyncio.run(coord.tick())

    assert outcome["action"] == "recovered"
    assert rm.kill_switch.is_armed is False
    assert rm.state_machine.state.value == "NORMAL"
    assert lifecycle.current == "TRADING"
    assert gate.can_open_position()[0] is True


@pytest.mark.parametrize("origin", [
    KILL_ORIGIN_MANUAL,               # 人按的按钮
    KILL_ORIGIN_AUTO_EQUITY,          # 重大资金异常
    KILL_ORIGIN_AUTO_ACCOUNTING,      # 账本已不可信
    KILL_ORIGIN_AUTO_RECONCILE,       # 账户与账本对不上
    "",                               # 未知来源
    "SOMETHING_NEW",                  # 将来新增但没标注的
])
def test_non_self_healable_origins_never_recover_automatically(origin: str) -> None:
    """**这是本模块最重要的断言**: 人工冻结与资金异常永远等人。

    系统无法自证「账本没算错」—— 重新对账只是拿自己的账本对自己的账本。
    """
    rm, lifecycle, gate = _system(origin=origin)
    coord = _coordinator(rm, lifecycle, gate)

    outcome = asyncio.run(coord.tick())

    assert outcome["action"] == "none"
    assert rm.kill_switch.is_armed is True
    assert rm.state_machine.state.value == "KILLED"
    assert lifecycle.current == "SAFE_MODE"


def test_manual_kill_reason_is_explained() -> None:
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_MANUAL)
    allowed, why = _coordinator(rm, lifecycle, gate).self_healable()
    assert allowed is False
    assert "人工" in why


def test_major_fund_origin_reason_tells_the_user_what_to_do() -> None:
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_EQUITY)
    allowed, why = _coordinator(rm, lifecycle, gate).self_healable()
    assert allowed is False
    assert "人工" in why and "核对账户" in why


def test_is_self_healable_origin_helper_matches_the_constants() -> None:
    for origin in SELF_HEALABLE_KILL_ORIGINS:
        assert is_self_healable_origin(origin) is True
    for origin in (KILL_ORIGIN_MANUAL, KILL_ORIGIN_AUTO_EQUITY, "", "X"):
        assert is_self_healable_origin(origin) is False


# ---------------------------------------------------------------------------
# 前置条件: 账户不可信时不动
# ---------------------------------------------------------------------------


def test_waits_while_the_account_state_is_untrusted() -> None:
    """「重新连接 → 重新对账」需要时间 —— 前置没满足就等, 不硬解冻。"""
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK)
    gate.reconciled = False
    coord = _coordinator(rm, lifecycle, gate)

    outcome = asyncio.run(coord.tick())

    assert outcome["action"] == "waiting"
    assert outcome["missing"]
    assert rm.kill_switch.is_armed is True  # 一点没动


def test_recovers_once_preconditions_become_true() -> None:
    """条件恢复后**下一轮**就能自愈 —— 不需要重启、不需要人。"""
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK)
    gate.reconciled = False
    coord = _coordinator(rm, lifecycle, gate)

    assert asyncio.run(coord.tick())["action"] == "waiting"
    gate.reconciled = True
    assert asyncio.run(coord.tick())["action"] == "recovered"


# ---------------------------------------------------------------------------
# 退避与上限
# ---------------------------------------------------------------------------


def test_backoff_grows_between_failed_attempts() -> None:
    """冻结/解冻之间来回抖动比不动更糟 —— 失败必须退避。"""
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK)
    gate.reconciled = False
    coord = AutoRecoveryCoordinator(rm, lifecycle, gate, base_interval=10.0)

    asyncio.run(coord.tick())
    assert coord.attempts == 1
    first = coord.status()["next_attempt_in"]
    assert first > 0

    # 退避未到 → 直接等待, 不再尝试
    assert asyncio.run(coord.tick())["action"] == "waiting"
    assert coord.attempts == 1


def test_exhausting_attempts_escalates_to_a_human_instead_of_spinning() -> None:
    """反复失败后**停止空转**并升级为「需要人工」—— 这正是任务书说的
    「系统宁可自己停, 也不要要求用户不断看守」。"""
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK)
    gate.reconciled = False
    coord = AutoRecoveryCoordinator(
        rm, lifecycle, gate, base_interval=0.0, max_attempts=3
    )

    for _ in range(3):
        asyncio.run(coord.tick())

    assert coord.status()["exhausted"] is True
    outcome = asyncio.run(coord.tick())
    assert outcome["action"] == "exhausted"


def test_successful_recovery_resets_the_attempt_counter() -> None:
    """一次偶发失败不该永久抬高后续的退避。"""
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK)
    gate.reconciled = False
    coord = _coordinator(rm, lifecycle, gate)
    asyncio.run(coord.tick())
    assert coord.attempts == 1

    gate.reconciled = True
    asyncio.run(coord.tick())
    assert coord.attempts == 0
    assert coord.status()["exhausted"] is False


def test_healthy_system_resets_state() -> None:
    rm, lifecycle, gate = _system(origin=KILL_ORIGIN_AUTO_TASK, armed=False)
    coord = _coordinator(rm, lifecycle, gate)
    coord.attempts = 2
    assert asyncio.run(coord.tick())["action"] == "none"
    assert coord.attempts == 0


# ---------------------------------------------------------------------------
# 策略表(任务书 P1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        ("read timeout", RecoveryPolicy.RETRY),
        ("连接被重置", RecoveryPolicy.RETRY),
        ("critical 任务 risk-loop 异常退出", RecoveryPolicy.RETRY),
        ("本地记账失败 at-123", RecoveryPolicy.FREEZE_HUMAN),
        ("资金漂移: equity_drift 12%", RecoveryPolicy.KILL),
        ("对账矩阵 KILLED: orphan_trade", RecoveryPolicy.KILL),
        ("行情数据失真", RecoveryPolicy.KILL),
        ("完全看不懂的错误", RecoveryPolicy.PAUSE),
        ("", RecoveryPolicy.PAUSE),
    ],
)
def test_classify_recovery_maps_every_documented_case(
    error: str, expected: RecoveryPolicy
) -> None:
    assert classify_recovery(error) == expected


def test_unknown_errors_pause_instead_of_retrying_blindly() -> None:
    """认不出来的错误先冷却是安全的; 无脑重试一个未知错误才是危险的。"""
    from at50_risk.auto_recovery import _DEFAULT_POLICY

    assert _DEFAULT_POLICY is RecoveryPolicy.PAUSE


def test_every_policy_in_the_table_is_reachable() -> None:
    """策略表里不该有永远走不到的分支 —— 那说明规则写错了。"""
    seen = {
        classify_recovery(s) for s in
        ("timeout", "记账失败", "资金漂移", "看不懂", "")
    }
    assert seen == {RecoveryPolicy.RETRY, RecoveryPolicy.FREEZE_HUMAN,
                    RecoveryPolicy.KILL, RecoveryPolicy.PAUSE}


# ---------------------------------------------------------------------------
# 监督器有界重启
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_task_is_restarted_before_freezing() -> None:
    """关键任务崩一次不再必须由人重启进程 —— 这是「无人值守」的头号前提。"""
    frozen: list[tuple[str, BaseException]] = []
    spawned: list[str] = []

    async def _boom() -> None:
        raise RuntimeError("boom")

    def factory(name: str):
        spawned.append(name)
        return _stable()

    async def _stable() -> None:
        await asyncio.sleep(3600)

    sup = RuntimeSupervisor(
        on_critical_failure=lambda n, e: frozen.append((n, e)),
        restart_factory=factory, restart_backoff=0.01,
    )
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await asyncio.sleep(0.2)

    assert "risk-loop" in spawned        # 重启被安排
    assert frozen == []                  # 没有冻结
    assert sup.restart_count == 1
    await sup.shutdown()


@pytest.mark.asyncio
async def test_restart_quota_is_bounded_and_falls_back_to_freezing() -> None:
    """重启额度用尽后行为与旧版一致(冻结) —— **没有降低最终安全等级**。"""
    frozen: list[tuple[str, BaseException]] = []

    async def _boom() -> None:
        raise RuntimeError("boom")

    def factory(name: str):
        return _boom()

    sup = RuntimeSupervisor(
        on_critical_failure=lambda n, e: frozen.append((n, e)),
        restart_factory=factory, restart_backoff=0.01, max_restarts=2,
    )
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await asyncio.sleep(0.5)

    assert frozen, "额度用尽后必须触发冻结回调"
    assert frozen[0][0] == "risk-loop"
    await sup.shutdown()


@pytest.mark.asyncio
async def test_non_critical_task_is_never_restarted() -> None:
    """只对 critical 任务重启 —— 非关键任务崩了不该被反复拉起。"""
    async def _boom() -> None:
        raise RuntimeError("boom")

    calls: list[str] = []
    sup = RuntimeSupervisor(restart_factory=lambda n: calls.append(n) or _stable(),
                            restart_backoff=0.01)

    async def _stable() -> None:
        await asyncio.sleep(3600)

    sup.spawn(_boom(), name="regime-loop", critical=False)
    await asyncio.sleep(0.1)
    assert calls == []
    await sup.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_restarts() -> None:
    """停机后不该又被拉起一个任务。"""
    async def _boom() -> None:
        raise RuntimeError("boom")

    async def _stable() -> None:
        await asyncio.sleep(3600)

    sup = RuntimeSupervisor(
        restart_factory=lambda n: _stable(), restart_backoff=5.0,
    )
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await sup.shutdown()
    await asyncio.sleep(0.1)
    assert sup.task_count() == 0


@pytest.mark.asyncio
async def test_restart_quota_is_counted_by_base_name_not_suffixed_name() -> None:
    """额度必须按**基线名**计。

    回归: 重启出来的任务名每次都带新后缀(`risk-loop#r1`, `risk-loop#r1#r1`, …),
    若按全名计数, 计数永远从 0 开始 → 额度形同虚设 → 崩溃任务被**无限拉起**。
    这是崩溃循环, 比不重启危险得多。
    """
    async def _boom() -> None:
        raise RuntimeError("boom")

    sup = RuntimeSupervisor(
        restart_factory=lambda n: _boom(), restart_backoff=0.01, max_restarts=3,
    )
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await asyncio.sleep(0.4)
    await sup.shutdown()

    # 基线名计到上限即停 —— 不会出现 #r1#r1#r1… 这种无限增长的链条
    assert sup.restart_status().get("risk-loop", 0) <= 3
    assert not any("##" in s["name"] for s in sup.status())


def test_base_name_strips_the_restart_suffix() -> None:
    assert RuntimeSupervisor.base_name("risk-loop") == "risk-loop"
    assert RuntimeSupervisor.base_name("risk-loop#r2") == "risk-loop"
    assert RuntimeSupervisor.base_name("risk-loop#r2#r3") == "risk-loop"


@pytest.mark.asyncio
async def test_restart_keeps_the_unique_name_contract() -> None:
    """原任务名保持占用, 重启任务用 `#rN` 后缀 —— 不破坏「同名 spawn 报错」的契约。"""
    async def _boom() -> None:
        raise RuntimeError("boom")

    async def _stable() -> None:
        await asyncio.sleep(3600)

    sup = RuntimeSupervisor(restart_factory=lambda n: _stable(), restart_backoff=0.01)
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await asyncio.sleep(0.2)

    names = {s["name"] for s in sup.status()}
    assert "risk-loop" in names
    assert any(n.startswith("risk-loop#r") for n in names)
    leftover = _stable()
    with pytest.raises(ValueError):
        sup.spawn(leftover, name="risk-loop")
    leftover.close()  # 名字重复时 spawn 直接抛, 这个协程不会被 await —— 显式关掉
    await sup.shutdown()


@pytest.mark.asyncio
async def test_without_a_factory_behavior_is_unchanged() -> None:
    """没配工厂时**退回旧契约**: 直接冻结。既有的 V11.6 契约不被破坏。"""
    frozen: list[str] = []

    async def _boom() -> None:
        raise RuntimeError("boom")

    sup = RuntimeSupervisor(on_critical_failure=lambda n, e: frozen.append(n))
    sup.spawn(_boom(), name="risk-loop", critical=True)
    await asyncio.sleep(0.1)
    assert frozen == ["risk-loop"]
    assert sup.restart_count == 0
    await sup.shutdown()
