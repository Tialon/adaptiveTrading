"""V11.4 P1-4 运行报告机制 — 每日复盘附「运行状态」快照。

验证 `DailyReport` 渲染运行状态(生命周期 / 风险态 / 急停 / 熔断 / 告警):
- 正常态各字段正确;
- 异常态(急停 + 熔断 + 暂停)附原因;
- health=None 时优雅占位, 不抛异常。

纯渲染测试(无 DB / 无网络), 直接调用 `_render`。
"""

import run
from at70_journal.daily_report import DailyReport


def _render(health=None):
    return DailyReport()._render("SOLUSDT", "2026-09-07", "TREND_UP", 10000.0, [], 0, [], health)


class TestRuntimeHealthSection:
    def test_normal_health_rendered(self):
        lines = _render(
            {
                "lifecycle": "TRADING",
                "risk_state": "NORMAL",
                "risk_reason": "",
                "kill_switch_armed": False,
                "kill_switch_reason": "",
                "breaker_open": False,
                "breaker_reason": "",
                "alerts": 0,
            }
        )
        assert "## 运行状态" in lines
        assert "- 生命周期: TRADING" in lines
        assert "- 风险状态: NORMAL" in lines
        assert "- 急停: 否" in lines
        assert "- 资金熔断: 关" in lines
        assert "- 活跃告警: 0" in lines

    def test_kill_armed_shows_reason(self):
        lines = _render(
            {
                "lifecycle": "SAFE_MODE",
                "risk_state": "KILLED",
                "risk_reason": "交叉对账失败",
                "kill_switch_armed": True,
                "kill_switch_reason": "交叉对账失败",
                "breaker_open": True,
                "breaker_reason": "Equity 漂移 0.6%",
                "alerts": 2,
            }
        )
        assert "- 生命周期: SAFE_MODE" in lines
        assert "- 风险状态: KILLED(交叉对账失败)" in lines
        assert "- 急停: 是 — 交叉对账失败" in lines
        assert "- 资金熔断: 开 — Equity 漂移 0.6%" in lines
        assert "- 活跃告警: 2" in lines

    def test_none_health_graceful(self):
        lines = _render(None)
        assert "## 运行状态" in lines
        assert "- 生命周期: 未知" in lines
        assert "- 风险状态: 未知" in lines
        assert "- 急停: 否" in lines
        assert "- 资金熔断: 关" in lines
        assert "- 活跃告警: 0" in lines

    def test_report_contains_trades_and_performance_sections(self):
        """运行状态段插入后, 原成交/绩效段仍保留(不破坏既有结构)。

        V13: 原 `## 下一步` 占位段(`- (待 AI 优化器接入后自动生成)`)已由真正的
        「## 今日系统复盘」取代。本测试不传复盘包, 所以段落内容是**如实说没生成**——
        带包渲染的内容由 `test_v13_daily_review.py` 覆盖。
        见 `at70_journal/daily_report.py::_render_self_review`。
        """
        lines = _render({"lifecycle": "TRADING", "risk_state": "NORMAL", "alerts": 0})
        assert "## 成交记录" in lines
        assert "## 策略绩效" in lines
        assert "## 今日系统复盘" in lines
        # 段落正文在同一行的括号里, 用整篇文本断言而不是列表成员
        assert "本次未生成复盘包" in "\n".join(lines)
        assert lines.index("## 运行状态") < lines.index("## 成交记录")


class _FakeStateMachine:
    current = "NORMAL"
    reason = ""


class _FakeKillSwitch:
    is_armed = False
    reason = ""


class _FakeBreaker:
    is_open = False
    reason = ""


class _FakeRiskManager:
    state_machine = _FakeStateMachine()
    kill_switch = _FakeKillSwitch()
    breaker = _FakeBreaker()


class _FakeLifecycle:
    current = "TRADING"


class TestRuntimeHealthAssembly:
    """run.py `_runtime_health` 汇总运行状态快照(生命周期/风险态/急停/熔断/告警)。"""

    def test_health_dict_assembled(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.lifecycle = _FakeLifecycle()
        sys.risk_manager = _FakeRiskManager()
        sys._active_alerts = {"order_fail_rate"}
        h = sys._runtime_health()
        assert h["lifecycle"] == "TRADING"
        assert h["risk_state"] == "NORMAL"
        assert h["kill_switch_armed"] is False
        assert h["breaker_open"] is False
        assert h["alerts"] == 1

    def test_health_dict_kill_armed(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.lifecycle = _FakeLifecycle()
        rm = _FakeRiskManager()
        rm.state_machine = _FakeStateMachine()
        rm.state_machine.current = "KILLED"
        rm.state_machine.reason = "交叉对账失败"
        rm.kill_switch = _FakeKillSwitch()
        rm.kill_switch.is_armed = True
        rm.kill_switch.reason = "交叉对账失败"
        rm.breaker = _FakeBreaker()
        sys.risk_manager = rm
        sys._active_alerts = set()
        h = sys._runtime_health()
        assert h["risk_state"] == "KILLED"
        assert h["risk_reason"] == "交叉对账失败"
        assert h["kill_switch_armed"] is True
        assert h["kill_switch_reason"] == "交叉对账失败"

    def test_health_none_components_graceful(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.lifecycle = None
        sys.risk_manager = None
        sys._active_alerts = set()
        h = sys._runtime_health()
        assert h["lifecycle"] == "未初始化"
        assert h["risk_state"] == "未初始化"
        assert h["kill_switch_armed"] is False
        assert h["breaker_open"] is False
        assert h["alerts"] == 0
