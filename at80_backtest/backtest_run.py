"""
回测 CLI

用法:
    python -m backtest.run --symbol SOLUSDT --days 7 --interval 1m
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for d in ("at01_common", "at10_market", "at20_analytics", "at30_strategy", "at60_execution", "at50_risk", "at80_backtest"):
    sys.path.insert(0, str(ROOT / d))

from at80_backtest.backtest_engine import BacktestConfig, run_backtest  # noqa: E402
from at01_common.logger import setup_logging  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="adaptiveTrading 回测")
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--days", type=int, default=7, help="回测天数(最多拉1000根)")
    parser.add_argument("--cash", type=float, default=10000.0)
    parser.add_argument("--threshold", type=float, default=80.0)
    args = parser.parse_args()

    setup_logging()
    config = BacktestConfig(
        symbol=args.symbol,
        interval=args.interval,
        days=args.days,
        initial_cash=args.cash,
        entry_threshold=args.threshold,
    )
    result = await run_backtest(config)

    print("\n========== 回测结果 ==========")
    for k, v in result.summary().items():
        print(f"{k:>14}: {v}")
    print(f"{'wins/losses':>14}: {result.wins}/{result.losses}")
    if result.trade_records:
        print("\n最近 5 笔交易:")
        for t in result.trade_records[-5:]:
            print(f"  {t}")


if __name__ == "__main__":
    asyncio.run(main())
