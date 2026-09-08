"""
HMM Regime 离线训练脚本(V9.0 M3.3) — CLI 独立跑, 不挂 run.py

用法:
    python at30_analytics/regime_hmm_train.py --symbol SOLUSDT --days 30 --states 3

流程: 读 klines -> 特征向量(log 收益 / realized vol / 量比 / 高低振幅)
      -> GaussianHMM.fit -> 存 models/regime_hmm.json
"""

import argparse
import asyncio
import math
from pathlib import Path

from at30_analytics.regime_hmm import GaussianHMM


def build_features(klines: list[list]) -> list[list[float]]:
    """K线 -> 特征向量(每根 bar 4 维)

    klines 行: [open_time, open, high, low, close, volume, ...]
    特征:
      0. log 收益       ln(close_t / close_{t-1})
      1. realized vol   log 收益 15 根滚动标准差
      2. 量比           volume_t / SMA(volume, 20)
      3. 高低振幅       (high - low) / close
    """
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    n = len(klines)

    log_ret = [0.0]
    for i in range(1, n):
        log_ret.append(math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0)

    def _sma(vals, window):
        out = [0.0] * n
        acc = 0.0
        for i in range(n):
            acc += vals[i]
            if i >= window:
                acc -= vals[i - window]
            out[i] = acc / min(i + 1, window)
        return out

    vol_window = 15
    # 滚动 std(基于已计算窗口)
    realized_vol = [0.0] * n
    for i in range(n):
        w = min(i + 1, vol_window)
        seg = log_ret[i - w + 1: i + 1]
        if len(seg) < 2:
            continue
        m = sum(seg) / len(seg)
        realized_vol[i] = math.sqrt(sum((x - m) ** 2 for x in seg) / len(seg))

    vol_sma20 = _sma(vols, 20)

    feats: list[list[float]] = []
    for i in range(n):
        c = closes[i]
        amp = (highs[i] - lows[i]) / c if c > 0 else 0.0
        vr = (vols[i] / vol_sma20[i]) if vol_sma20[i] > 0 else 1.0
        feats.append([log_ret[i], realized_vol[i], vr, amp])
    return feats


async def _fetch(symbol: str, interval: str, days: int) -> list[list]:
    from at20_market.market_rest_client import BinanceRestClient

    client = BinanceRestClient()
    await client.connect()
    try:
        all_klines: list[list] = []
        end_time = None
        target = days * 1440 if interval == "1m" else days * 24 * (60 // 5)
        while len(all_klines) < target:
            kwargs = {"symbol": symbol, "interval": interval, "limit": 1000}
            if end_time:
                kwargs["end_time"] = end_time
            page = await client.get_klines(**kwargs)
            if not page:
                break
            all_klines = page + all_klines
            end_time = int(page[0][0]) - 1
            if len(page) < 1000:
                break
        return all_klines[-target:]
    finally:
        await client.disconnect()


async def train(symbol: str, interval: str, days: int, n_states: int, out: str) -> None:
    klines = await _fetch(symbol, interval, days)
    if len(klines) < 100:
        raise SystemExit(f"K线不足({len(klines)}), 无法训练")

    feats = build_features(klines)
    model = GaussianHMM(n_states=n_states, n_features=4, seed=42)
    model.fit(feats, n_iter=30)

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    model.save(out)
    print(f"HMM 已训练: {len(feats)} bars, {n_states} states -> {out}")
    print("起始概率:", [round(p, 3) for p in model.startprob])
    print("转移矩阵:")
    for row in model.transmat:
        print("  ", [round(p, 3) for p in row])


def main() -> None:
    p = argparse.ArgumentParser(description="训练 HMM Regime 模型")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--states", type=int, default=3)
    p.add_argument("--out", default="models/regime_hmm.json")
    args = p.parse_args()
    asyncio.run(train(args.symbol, args.interval, args.days, args.states, args.out))


if __name__ == "__main__":
    main()
