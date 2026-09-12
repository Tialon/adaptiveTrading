"""
回测执行模型(V7)

1. SlippageModel: 买入 close×(1+bps), 卖出 close×(1-bps), 可做敏感性(0/5/10/20bps)
2. NextBarExecutor: 信号在 bar t 收盘产生, bar t+1 开盘成交(杀 look-ahead)
   - 挂起意图队列, 下一根 bar 开盘统一执行
3. AsOfBTC: BTC 数据按 "timestamp <= sol_ts 的最近值" 对齐, 带数据龄检查
"""


class SlippageModel:
    """滑点/价差执行模型(V9.0 M3.2: 支持 regime 条件滑点)

    - 平铺 bps: 所有环境统一滑点。
    - regime_bps: {regime: bps} 覆盖表(PANIC/VOLATILE 等放大), 命中优先, 缺失回退平铺。
    """

    def __init__(self, slippage_bps: float = 10.0, regime_bps: dict | None = None):
        self.bps = slippage_bps / 10000.0
        self.regime_bps = {k.upper(): v / 10000.0 for k, v in (regime_bps or {}).items()}

    def _bps(self, regime: str | None) -> float:
        if regime and regime.upper() in self.regime_bps:
            return self.regime_bps[regime.upper()]
        return self.bps

    def buy_price(self, ref_price: float, regime: str | None = None) -> float:
        return ref_price * (1 + self._bps(regime))

    def sell_price(self, ref_price: float, regime: str | None = None) -> float:
        return ref_price * (1 - self._bps(regime))


class NextBarExecutor:
    """次 bar 执行器(无未来函数)

    bar t 收盘产生 OrderIntent -> 入队
    bar t+1 开盘: 以开盘价(含滑点)执行
    """

    def __init__(self):
        self._pending: list[dict] = []

    def submit(self, intent: dict) -> None:
        """t 收盘提交意图"""
        self._pending.append(intent)

    def execute_at_open(self, next_open: float, slippage: SlippageModel) -> list[dict]:
        """t+1 开盘执行全部挂起意图, 返回可执行列表(带执行价)"""
        ready = self._pending
        self._pending = []
        for intent in ready:
            side = intent["side"]
            regime = intent.get("regime")  # V9.0 M3.2: 透传 regime 到滑点
            intent["exec_price"] = (
                slippage.buy_price(next_open, regime) if side == "BUY"
                else slippage.sell_price(next_open, regime)
            )
        return ready

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class AsOfJoiner:
    """BTC asof 对齐(<= sol_ts 的最近值, 带数据龄)"""

    def __init__(self, btc_klines: list, max_age_ms: int = 300_000):
        # [(ts, close)] 升序
        self._data = sorted((int(k[0]), float(k[4])) for k in btc_klines)
        self.max_age_ms = max_age_ms
        self._cursor = 0

    def close_asof(self, sol_ts: int) -> tuple[float, float]:
        """返回 (btc_close, age_ms); 无可用数据返回 (0.0, inf)"""
        # 推进游标到最后一个 <= sol_ts 的位置
        while (
            self._cursor + 1 < len(self._data)
            and self._data[self._cursor + 1][0] <= sol_ts
        ):
            self._cursor += 1
        if not self._data or self._data[self._cursor][0] > sol_ts:
            return 0.0, float("inf")
        ts, close = self._data[self._cursor]
        age = sol_ts - ts
        if age > self.max_age_ms:
            return close, age  # 调用方按超龄处理
        return close, age
