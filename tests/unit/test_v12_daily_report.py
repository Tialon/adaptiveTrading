"""V12 §37: 每日复盘报告 —— 账户/持仓 + HODL 对标 + 交易活动 + 交易门/对账。

- DailyReport._render 新增「账户与持仓」「HODL 对标」「交易活动」三节(metrics/activity 驱动);
- DailyReport._render_health 新增「交易门·开仓/减仓 + 对账/交易所/行情健康」;
- run.py._v12_report_metrics 组装账户/持仓/HODL 指标(纯本地, 不查交易所)。

纯渲染测试(无 DB / 无网络)。
"""

import pytest

import run
from at40_journal.daily_report import DailyReport

_BENCH = {
    "initial_equity": 10000.0,
    "initial_sol_qty": 50.0,
    "initial_sol_price": 100.0,
    "adaptive_equity": 10800.0,
    "hodl_equity": 11000.0,
    "cash_equity": 10000.0,
    "alpha": -200.0,
    "adaptive_pnl": 800.0,
    "hodl_pnl": 1000.0,
}

_METRICS = {
    "sol_qty": 50.0,
    "usdt_cash": 5000.0,
    "price": 100.0,
    "sol_exposure_pct": 0.5,
    "unrealized_pnl": 800.0,
    "realized_pnl": 200.0,
    "drawdown_pct": 0.03,
    "benchmark": _BENCH,
}

_ACTIVITY = {"buy_count": 2, "sell_count": 1, "fees": 1.5}


def _render(health=None, metrics=None, activity=None):
    return DailyReport()._render(
        "SOLUSDT", "2026-09-08", "TREND_UP", 10000.0,
        [], 0, [], health, metrics, activity,
    )


class TestV12ReportSections:
    def test_account_section(self):
        lines = _render(metrics=_METRICS)
        assert "## 账户与持仓" in lines
        assert "- SOL 敞口: 50.00%(上限 70%)" in lines
        assert "- 未实现盈亏: +800.00" in lines
        assert "- 回撤: 3.00%" in lines

    def test_benchmark_section(self):
        lines = _render(metrics=_METRICS)
        assert "## HODL 对标" in lines
        assert "- 策略权益(Adaptive): 10,800.00" in lines
        assert "- Alpha(跑赢持有): -200.00" in lines
        assert "- 策略盈亏: +800.00 / 持有盈亏: +1,000.00" in lines

    def test_benchmark_missing_graceful(self):
        lines = _render(metrics={"benchmark": None})
        assert "## HODL 对标" in lines
        assert any("未记录基线" in x for x in lines)

    def test_activity_section(self):
        lines = _render(metrics=_METRICS, activity=_ACTIVITY)
        assert "## 交易活动" in lines
        assert "- 当日买入: 2 笔 / 卖出: 1 笔" in lines
        assert "- 当日手续费: 1.5000 USDT" in lines

    def test_section_order(self):
        lines = _render(metrics=_METRICS, activity=_ACTIVITY)
        assert lines.index("## 运行状态") < lines.index("## 账户与持仓")
        assert lines.index("## 账户与持仓") < lines.index("## HODL 对标")
        assert lines.index("## HODL 对标") < lines.index("## 交易活动")
        assert lines.index("## 交易活动") < lines.index("## 成交记录")

    def test_none_metrics_graceful(self):
        # metrics/activity 均缺省时不抛异常(向后兼容旧调用)。
        lines = _render()
        assert "## 账户与持仓" in lines
        assert "## HODL 对标" in lines
        assert "## 交易活动" in lines


class TestHealthTradingGate:
    def test_gate_rendered(self):
        lines = _render(
            health={
                "trading_gate": {
                    "can_open_position": True,
                    "can_reduce_position": True,
                    "reconciled": True,
                    "exchange_healthy": True,
                    "market_data_healthy": True,
                }
            }
        )
        assert "- 交易门·开仓: 放行" in lines
        assert "- 交易门·减仓: 放行" in lines
        assert "- 对账健康: 是 / 交易所: 是 / 行情: 是" in lines

    def test_gate_blocked_reason(self):
        lines = _render(
            health={
                "trading_gate": {
                    "can_open_position": False,
                    "open_reason": "风险禁止开仓",
                    "can_reduce_position": True,
                    "reconciled": False,
                    "exchange_healthy": True,
                    "market_data_healthy": False,
                }
            }
        )
        assert "- 交易门·开仓: 禁止" in lines
        assert "- 开仓阻断: 风险禁止开仓" in lines
        assert "- 对账健康: 否 / 交易所: 是 / 行情: 否" in lines

    def test_no_gate_no_gate_lines(self):
        # 无 trading_gate 键(旧 health)不渲染交易门行, 向后兼容。
        lines = _render(health={"lifecycle": "TRADING", "risk_state": "NORMAL", "alerts": 0})
        assert not any("交易门" in x for x in lines)


class _FakePosition:
    quantity = 50.0
    avg_price = 90.0
    realized_pnl = 0.0


class _FakePositions:
    def __init__(self):
        self._p = _FakePosition()
        self.positions = {"SOLUSDT": self._p}

    def get_or_none(self, symbol):
        return self._p


class _EmptyPositions:
    positions = {}

    def get_or_none(self, symbol):
        return None


class _FakeDrawdown:
    def status(self):
        return {"drawdown": 0.03}


class _FakeRM:
    def __init__(self, positions=None):
        self.positions = positions if positions is not None else _FakePositions()
        self.drawdown = _FakeDrawdown()


class _FakeBenchResult:
    def to_dict(self):
        return {"alpha": -200.0, "adaptive_equity": 10800.0}


class _FakeHodl:
    async def evaluate(self, equity, price):
        return _FakeBenchResult()


class TestV12ReportMetrics:
    async def test_metrics_assembled(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.risk_manager = _FakeRM()
        sys.hodl_benchmark = _FakeHodl()
        m = await sys._v12_report_metrics("SOLUSDT", 10000.0, 100.0)
        assert m["sol_qty"] == pytest.approx(50.0)
        assert m["usdt_cash"] == pytest.approx(10000.0 - 50.0 * 100.0)
        assert m["sol_exposure_pct"] == pytest.approx(0.5)
        assert m["unrealized_pnl"] == pytest.approx((100.0 - 90.0) * 50.0)
        assert m["realized_pnl"] == pytest.approx(0.0)
        assert m["drawdown_pct"] == pytest.approx(0.03)
        assert m["benchmark"]["alpha"] == -200.0

    async def test_metrics_no_hodl(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.risk_manager = _FakeRM()
        sys.hodl_benchmark = None
        m = await sys._v12_report_metrics("SOLUSDT", 10000.0, 100.0)
        assert m["benchmark"] is None

    async def test_metrics_no_position(self):
        sys = object.__new__(run.AdaptiveTradingSystem)
        sys.risk_manager = _FakeRM(positions=_EmptyPositions())
        sys.hodl_benchmark = None
        m = await sys._v12_report_metrics("SOLUSDT", 10000.0, 100.0)
        assert m["sol_qty"] == pytest.approx(0.0)
        assert m["sol_exposure_pct"] == pytest.approx(0.0)
        assert m["unrealized_pnl"] == pytest.approx(0.0)
