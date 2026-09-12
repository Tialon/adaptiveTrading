"""V11.2 P1-3 Backtest 最终验收。

用真实策略管线 `PortfolioBacktester`(与实盘同一套 StrategyEngine/DecisionEngine/PositionSizer/
PortfolioLedger), 在「牛→熊→恐慌→横盘」多市场态合成数据上做最终验收, 断言:

1. 全量 bar 处理(bars == 输入长度);
2. 账务守恒 —— 组合账本对账 `balanced == True`(核心验收, 任何记账漂移即失败);
3. 权益/敞口/现金曲线完整且数值合法(正权益、非负现金、敞口无杠杆失控);
4. 基准(buy-hold)与超额已计算且有限;
5. 回撤 ∈ [0,1]; 夏普/索提诺/卡玛有限;
6. 胜率 ∈ [0,1]、盈利因子非负、闭环成交指标自洽。
"""

import math

from at80_backtest.backtest_portfolio import PortfolioBacktester


def _klines(segments: list[tuple[int, float]]) -> list[list]:
    """按 (bars, 每bar乘数) 生成多市场态合成 K 线(币安 9 元素格式)。

    segments 例: [(300, 1.002), (300, 0.998), (200, 0.99), (400, 1.0001)]
    依次为 牛 / 熊 / 恐慌 / 横盘。
    """
    out: list[list] = []
    price = 100.0
    ts = 1_700_000_000_000
    for bars, factor in segments:
        for _ in range(bars):
            price *= factor
            out.append([
                ts, price, price * 1.001, price * 0.999, price,
                10.0, ts + 59_999, 1000.0, 5,
            ])
            ts += 60_000
    return out


async def test_backtest_final_acceptance_multiregime():
    klines = _klines([(300, 1.002), (300, 0.998), (200, 0.99), (400, 1.0001)])
    bt = PortfolioBacktester(symbol="SOLUSDT", initial_cash=20000.0)
    r = await bt.run(klines)

    # 1. 全量 bar 处理
    assert r.bars == len(klines)

    # 2. 账务守恒(核心验收)
    assert r.reconciliation.get("balanced") is True, r.reconciliation

    # 3. 曲线完整且数值合法(取最近 200)
    assert len(r.equity_curve) == 200
    assert all(v > 0 for v in r.equity_curve)
    assert len(r.exposure_curve) == 200
    assert all(0.0 <= e <= 1.0 for e in r.exposure_curve)  # 无负敞口、无杠杆失控
    assert len(r.cash_curve) == 200
    assert all(c >= 0.0 for c in r.cash_curve)  # 现金非负

    # 4. 基准/超额已计算且有限
    assert math.isfinite(r.benchmark_return)
    assert math.isfinite(r.excess_return)

    # 5. 回撤 ∈ [0,1], 风险调整指标有限
    assert 0.0 <= r.max_drawdown <= 1.0
    assert math.isfinite(r.sharpe)
    assert math.isfinite(r.sortino)
    assert math.isfinite(r.calmar)

    # 6. 闭环成交指标自洽
    assert 0.0 <= r.win_rate <= 1.0
    assert r.profit_factor >= 0.0


async def test_backtest_bear_market_remains_balanced():
    """持续下跌(恐慌)市场: 即便亏损也绝不能破坏账务守恒。"""
    klines = _klines([(600, 0.995)])  # 单边下跌
    bt = PortfolioBacktester(symbol="SOLUSDT", initial_cash=20000.0)
    r = await bt.run(klines)
    assert r.bars == 600
    assert r.reconciliation.get("balanced") is True, r.reconciliation
    assert r.max_drawdown >= 0.0
    # 亏损市场中仍能输出有限指标
    assert math.isfinite(r.sharpe)
    assert math.isfinite(r.benchmark_return)
