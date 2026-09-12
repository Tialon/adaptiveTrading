"""V15 恢复检查 —— 「异常 → 冻结 → 人工恢复 → 条件复检 → 能否交易」完整闭环。

用户真实验收暴露的问题:

    系统进入安全模式 → equity=KILL / position=PAUSE / cash=PAUSE → 行情静默 → 资金漂移
    → 用户点击「恢复急停」 → **仍然无法恢复到可交易状态**, 而且页面没说清是谁在挡。

V15 §8 把 `recover` 重新定义为**恢复检查**(不是「清除 KILL」):

    条件全部满足 → 恢复
    任一不满足   → **保持冻结**, 并逐项说明是谁在挡、为什么、能不能自证、要不要人

**第一原则(本文件的重点)**: 不得为了让恢复"成功"而降低任何安全门槛。
本文件最重要的一组断言是 §10 的矩阵 —— **任何安全条件没恢复, 就不能交易**。
"""

from __future__ import annotations

import pytest

from at01_common.operator_narrative import KILL_ORIGIN_MANUAL
from at50_risk.recovery_flow import (
    CATEGORY_ACCOUNT_TRUTH,
    CATEGORY_BLOCKING,
    CATEGORY_SELF_HEALABLE,
    perform_recovery,
    run_recovery_check,
)
from at50_risk.risk_manager import RiskManager
from at50_risk.system_lifecycle import SystemLifecycle
from at50_risk.trading_gate import TradingGate


def _system():
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    lifecycle.warm_up(); lifecycle.sync(); lifecycle.self_check()
    lifecycle.ready(); lifecycle.start_trading()
    return rm, lifecycle, gate


def _freeze(rm, lifecycle, reason: str = "回撤 15%") -> None:
    rm.kill_switch.arm(reason, origin=KILL_ORIGIN_MANUAL)
    rm.state_machine.kill(reason)
    lifecycle.enter_safe_mode(reason)


def _items(check) -> dict:
    return {i.key: i for i in check.items}


# ---------------------------------------------------------------------------
# §3/§4: 必须能指出「到底是谁在挡」
# ---------------------------------------------------------------------------


def test_check_reports_every_dimension_the_task_lists() -> None:
    """§8 的检查清单: 行情 / 交易所账户 / 对账 / 关键任务 / 配置 / 停机。"""
    rm, _lc, gate = _system()
    keys = set(_items(run_recovery_check(gate, rm, settings=None)))
    assert {"shutdown", "market", "exchange", "reconcile", "tasks", "kill_switch"} <= keys


def test_every_failing_item_explains_who_can_fix_it() -> None:
    """§4: 禁止只给「恢复失败」这种没有原因的结论。"""
    rm, _lc, gate = _system()
    gate.connection_ok = False
    gate.market_data_healthy = False
    check = run_recovery_check(gate, rm)
    assert check.ok is False
    for item in check.items:
        if not item.ok:
            assert item.label.strip()
            assert item.detail.strip()
            assert item.to_dict()["why"].strip(), "每一项失败都要说清为什么"


def test_reconcile_failure_is_narrowed_to_the_actual_dimension() -> None:
    """§6/§7: 资金漂移 / 持仓不符 / 现金不符要**分别定位**, 不能笼统说「对账失败」。"""
    rm, _lc, gate = _system()
    gate.reconciled = False
    for message, expect_key in (
        ("equity_drift SOLUSDT", "equity"),
        ("position:mismatch SOLUSDT", "position"),
        ("cash_mismatch SOLUSDT", "cash"),
    ):
        check = run_recovery_check(gate, rm, last_error={"message": message})
        failing = {i.key for i in check.items if not i.ok}
        assert expect_key in failing, f"{message} 未定位到 {expect_key}: {failing}"


def test_categories_decide_whether_a_human_can_override() -> None:
    """§9: 能自证的等系统自己恢复; 无法自证的资金异常才允许人工确认越过。"""
    rm, _lc, gate = _system()
    gate.market_data_healthy = False          # 能自证 → 人工也不能越过
    assert _items(run_recovery_check(gate, rm))["market"].category == CATEGORY_SELF_HEALABLE

    gate.market_data_healthy = True
    gate.reconciled = False                    # 无法自证 → 可人工确认
    assert _items(run_recovery_check(gate, rm))["reconcile"].category == CATEGORY_ACCOUNT_TRUTH

    gate.reconciled = True
    gate.shutting_down = True                  # 结构性 → 确认也没用
    assert _items(run_recovery_check(gate, rm))["shutdown"].category == CATEGORY_BLOCKING


def test_being_frozen_does_not_block_its_own_recovery() -> None:
    """回归: 「急停已武装」不能算未通过 —— 那会形成「因为冻结所以不许解冻」的自锁。

    (这条是在实现时被测试抓住的: 第一版把 kill_switch 写成了阻塞项。)
    """
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    check = run_recovery_check(gate, rm)
    assert _items(check)["kill_switch"].ok is True
    assert check.ok is True, f"冻结本身不该阻塞恢复: {check.missing}"


# ---------------------------------------------------------------------------
# §8: 恢复 = 检查, 不是清除 KILL
# ---------------------------------------------------------------------------


def test_unmet_conditions_keep_the_system_frozen() -> None:
    """**这是与 V13 的关键契约变更**: 旧版点了就解冻, 新版不满足就保持冻结。"""
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    gate.reconciled = False

    result = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)

    assert result["ok"] is False
    assert result["stage"] == "recovery_check"
    # 三层状态**一点没动**
    assert rm.kill_switch.is_armed is True
    assert rm.state_machine.state.value == "KILLED"
    assert lifecycle.current == "SAFE_MODE"
    assert gate.can_open_position()[0] is False


def test_blocked_recovery_gives_the_full_five_part_answer() -> None:
    """§8 指定: 失败时也要给全 原因/影响/系统动作/用户动作。"""
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    gate.reconciled = False
    result = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    for key in ("cause", "impact", "system_actions", "user_action"):
        assert result.get(key), f"缺少 {key}"
    assert "保持" in result["impact"] or "冻结" in result["impact"]


def test_recovery_succeeds_once_every_condition_is_met() -> None:
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    result = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    assert result["ok"] is True
    assert rm.kill_switch.is_armed is False
    assert lifecycle.current == "TRADING"


def test_human_confirmation_cannot_override_untrusted_market_data() -> None:
    """**安全红线**: 行情不可信时, 即便操作者说「我确认过了」也不放行 ——
    那等于在数据不可信时下单, 确认解决不了这个问题。"""
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    gate.market_data_healthy = False

    result = perform_recovery(
        risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True
    )
    assert result["ok"] is False
    assert rm.kill_switch.is_armed is True
    assert "行情" in "、".join(result["missing"])


def test_human_confirmation_can_clear_an_account_truth_block() -> None:
    """§9C 的「人工确认」路径: 账户对不上时, 操作者核对后可越过。"""
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    gate.reconciled = False

    blocked = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    assert blocked["ok"] is False
    assert blocked["human_override_available"] is True

    allowed = perform_recovery(
        risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True
    )
    assert allowed["ok"] is True
    assert rm.kill_switch.is_armed is False


def test_no_force_normal_path_exists() -> None:
    """第一原则: 禁止 `recover() → force_normal() → can_buy=True` 这种捷径。

    用 **AST** 而不是文本匹配 —— 本模块的 docstring 正解释着这个被禁止的写法,
    按文本找会把自己人也拦下(写这条时就踩了)。
    """
    import ast
    import inspect

    from at50_risk import recovery_flow

    tree = ast.parse(inspect.getsource(recovery_flow))

    # 不得定义或调用 force_normal 之类的"直接置正常"
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "force_normal" not in names

    # 不得给 can_buy / can_sell 赋值(交易许可只能由 TradingGate 决定)
    assigned = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert "can_buy" not in assigned
    assert "can_sell" not in assigned


# ---------------------------------------------------------------------------
# §10: 「恢复后是否真正可交易」矩阵 —— 本阶段最关键的一组
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "break_it,label",
    [
        (lambda g: setattr(g, "market_data_healthy", False), "行情未恢复"),
        (lambda g: setattr(g, "connection_ok", False), "连接未恢复"),
        (lambda g: setattr(g, "exchange_healthy", False), "交易所不可信"),
        (lambda g: setattr(g, "reconciled", False), "对账未通过"),
        (lambda g: setattr(g, "critical_tasks_healthy", False), "关键任务未运行"),
        (lambda g: setattr(g, "shutting_down", True), "停机中"),
    ],
)
def test_any_unmet_condition_means_no_trading(break_it, label: str) -> None:
    """**任务书 §10 的核心要求**: 任何安全条件没恢复 → 不能交易。

    即使调用方强行要求恢复(force), 也不得放行。
    """
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    break_it(gate)

    perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)

    ok, reason = gate.can_open_position()
    assert ok is False, f"{label} 时竟然可以开仓"


def test_fully_healthy_recovery_actually_enables_trading() -> None:
    """反面: 条件全好时恢复**必须真的能交易** —— 否则就是过度保守把系统锁死。"""
    rm, lifecycle, gate = _system()
    _freeze(rm, lifecycle)
    perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    ok, reason = gate.can_open_position()
    assert ok is True, f"条件全满足却仍不能开仓: {reason}"


# ---------------------------------------------------------------------------
# §18: 用户当前问题的回归测试
# ---------------------------------------------------------------------------


def test_emergency_recovery_after_market_silence_and_equity_drift() -> None:
    """§18 指定的回归: 行情静默 + 资金漂移 → KILL → 恢复 → 条件未恢复仍冻结 →
    条件恢复 → 复查 → 闸门真的放行。**不能只验证最终状态。**
    """
    rm, lifecycle, gate = _system()
    assert gate.can_open_position()[0] is True          # 1) 起初可交易

    # 2) 行情静默 + 资金漂移
    gate.market_data_healthy = False
    gate.reconciled = False
    _freeze(rm, lifecycle, "资金漂移 + 行情静默")
    assert gate.can_open_position()[0] is False          # 3) 已冻结

    # 4) 用户点恢复 —— 但条件**还没**恢复
    blocked = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    assert blocked["ok"] is False
    assert rm.kill_switch.is_armed is True               # 5) 保持冻结
    assert "行情" in "、".join(blocked["missing"])        # 6) 且说清是谁在挡
    assert gate.can_open_position()[0] is False

    # 7) 只恢复行情 —— 资金漂移还在 → 仍然不能恢复
    gate.market_data_healthy = True
    still_blocked = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    assert still_blocked["ok"] is False
    assert rm.kill_switch.is_armed is True

    # 8) 资金漂移也收敛了 → 恢复检查通过
    gate.reconciled = True
    done = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate)
    assert done["ok"] is True

    # 9) 最终闸门真的放行(不是只把状态改成 NORMAL 就算数)
    ok, reason = gate.can_open_position()
    assert ok is True, f"全链路恢复后仍不能开仓: {reason}"
    assert rm.state_machine.state.value == "NORMAL"
    assert lifecycle.current == "TRADING"
