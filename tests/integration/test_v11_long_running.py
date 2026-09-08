"""V11.3 P0-6 长跑仿真(1h / 6h / 24h 逻辑时间)。

无人值守系统需经得起「连续运行一天」: 本片不真等 24 小时, 而是用可拨动的
FakeClock 替换 time.time, 按 1 tick/s 喂合成行情驱动 RiskManager 异常检测与策略冷却,
断言长跑后:

1. 内存有界: 快速暴跌检测的 _price_history deque 被 15 分钟窗口修剪(不随 tick 数线性
   增长); 单标的下各字典尺寸有界;
2. 状态无腐坏: 正常行情 24h 后风险态仍 NORMAL, 无意外暂停/急停/连续错误;
3. 时间窗语义正确: 异常暂停在窗口到期后自动恢复; 策略冷却到期后放行。

正常行情合成价格: 小幅正弦震荡, 单 tick 变化 << 3%(不触发尖刺暂停)、无 15 分钟 10%
暴跌(不触发快速暴跌), 确保长跑本身不产生告警。
"""

import math
import time

import pytest

from at30_analytics.engine import MarketAnalytics
from at50_strategy.strategy_buy import BuyStrategy
from at50_strategy.strategy_grid import GridStrategy
from at60_risk.risk_manager import RiskManager
from at60_risk.risk_state import RiskState

SYMBOL = "SOLUSDT"


class FakeClock:
    """可拨动的逻辑时钟(替换 time.time)。"""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 1.0) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(time, "time", c)
    return c


def _normal_walk_prices(n: int, base: float = 100.0) -> list[float]:
    """正常行情: 小幅震荡 + 周期复位漂移, 单 tick 变化 < 3%。"""
    return [
        base * (1.0 + 0.005 * math.sin(i / 50.0) + 0.00005 * (i % 200))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. 长跑内存有界 + 状态稳定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hours,ticks", [(1, 3600), (6, 21600), (24, 86400)])
def test_long_run_price_history_bounded_and_state_stable(clock, hours, ticks):
    """正常行情跑 N 小时: 价格历史 deque 有界、风险态仍 NORMAL、无连续错误。"""
    rm = RiskManager()
    prices = _normal_walk_prices(ticks)

    for price in prices:
        rm.check_tick_anomaly(SYMBOL, price)
        rm.check_fast_crash(SYMBOL, price)
        clock.advance(1.0)

    hist = rm._price_history.get(SYMBOL)
    assert hist is not None
    # 15 分钟窗口(900 tick)修剪生效: 24h=86400 tick 也不超过窗口 + 少量冗余
    assert 0 < len(hist) <= 950, f"价格历史 deque 未按窗口修剪: len={len(hist)}"

    # 状态无腐坏: 正常行情不触发暂停/急停
    assert rm.state_machine.state is RiskState.NORMAL
    assert rm._consecutive_errors == 0
    assert not rm.kill_switch.is_armed
    assert len(rm._last_tick_price) == 1  # 单标的


def test_fast_crash_window_prunes_without_unbounded_growth(clock):
    """直接验证 deque 修剪: 持续喂 tick, 窗口外旧数据被弹出(内存有界)。"""
    rm = RiskManager()
    for i in range(2000):
        rm.check_fast_crash(SYMBOL, 100.0 + 0.01 * math.sin(i))
        clock.advance(1.0)
    # 2000 tick 后, deque 应稳定在 ~900(15 分钟窗口), 而非 2000
    assert len(rm._price_history[SYMBOL]) <= 950


# ---------------------------------------------------------------------------
# 2. 时间窗语义(逻辑时间推进)
# ---------------------------------------------------------------------------

def test_anomaly_pause_auto_recovers_after_window(clock):
    """价格尖刺(>3%)触发暂停, 逻辑时间推进超过 pause 窗口后自动恢复 NORMAL。"""
    rm = RiskManager()

    # 先打一个基准 tick
    rm.check_tick_anomaly(SYMBOL, 100.0)
    clock.advance(1.0)
    assert rm.state_machine.state is RiskState.NORMAL

    # 尖刺 5% -> 触发暂停(窗口默认 60s)
    triggered = rm.check_tick_anomaly(SYMBOL, 105.0)
    assert triggered
    assert rm.state_machine.is_paused()
    assert not rm.can_buy()

    # 推进 59s: 仍在暂停
    clock.advance(59.0)
    assert rm.state_machine.is_paused()

    # 再推进 1s: 窗口到期自动恢复
    clock.advance(1.0)
    assert rm.state_machine.state is RiskState.NORMAL
    assert rm.can_buy()


def test_strategy_cooldown_expires_over_logical_time(clock):
    """策略信号冷却到期(逻辑时间)后放行同标的再次信号。"""
    s = BuyStrategy(symbols=[SYMBOL])
    s.signal_cooldown = 30.0

    def analytics(price=98.9):
        return MarketAnalytics(
            symbol=SYMBOL, price=price, vwap=101.0,
            vwap_deviation=(price - 101.0) / 101.0,
            accumulation=0.0, is_accumulating=False,
            cvd_rising=True, cvd_falling=False, trend="neutral",
            ema_fast=price, ema_slow=price,
            recent_high=103.0, recent_low=99.0,
            volume_ratio=1.5, delta_ratio=0.3,
        )

    first = s.on_market(analytics(98.9))
    assert len(first) == 1  # 首次放行

    # 冷却期内同标的被拦
    assert s.on_market(analytics(98.9)) == []

    # 推进超过冷却窗口后放行
    clock.advance(31.0)
    again = s.on_market(analytics(98.9))
    assert len(again) == 1


# ---------------------------------------------------------------------------
# 3. 网格有界
# ---------------------------------------------------------------------------

def test_grid_levels_bounded_after_many_cycles(clock):
    """网格反复重设后层数有界(count+1), 不随重设次数累积。"""
    g = GridStrategy(symbols=[SYMBOL])
    base = 100.0
    for i in range(5000):
        # 价格持续上漂触发越界重设, 反复重建网格
        price = base * (1.0 + 0.03 * (i + 1))
        g.on_market(MarketAnalytics(
            symbol=SYMBOL, price=price, vwap=price, vwap_deviation=0.0,
            accumulation=0.0, is_accumulating=False,
            cvd_rising=False, cvd_falling=False, trend="neutral",
            ema_fast=price, ema_slow=price,
            recent_high=price * 1.01, recent_low=price * 0.99,
            volume_ratio=1.0, delta_ratio=0.0,
        ))
        clock.advance(1.0)

    grid = g.get_grid(SYMBOL)
    assert grid is not None
    assert len(grid.levels) == g.count + 1  # 层数恒有界
    assert len(g._grids) == 1  # 单标的单网格, 不累积
