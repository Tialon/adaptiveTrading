"""
交易所持仓对账(V8)

比较本地总账持仓与交易所账户余额, 检测不一致(缺币/多币/API 异常)。
纸面模式下仅做现金非负自检(无交易所余额可对)。

原则: 只检测与告警, 由上层(RiskManager.pause)决定是否暂停交易。
"""

from typing import Any

from at01_common.logger import LoggerMixin

QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB", "EUR")


def _split_asset(symbol: str) -> tuple[str, str]:
    """SOLUSDT -> (SOL, USDT)"""
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


class PositionReconciler(LoggerMixin):
    """持仓对账器"""

    def __init__(self, rest_client: Any = None, tolerance: float = 1e-6):
        self.rest = rest_client
        self.tolerance = tolerance

    async def reconcile_live(self, local_positions: dict[str, Any]) -> list[dict[str, Any]]:
        """实盘对账: 本地持仓 vs 交易所余额, 返回差异列表

        - mismatch: 本地与交易所数量不一致(含本地有/交易所无)。
        - exchange_only(V10.2): 交易所有该 base 资产余额、本地持仓为 0 —— 反向遍历检出。
        - api_error: 无法获取交易所账户。
        """
        if self.rest is None:
            return []
        try:
            account = await self.rest.get_account()
        except Exception as e:
            self.logger.warning("对账获取账户失败", error=str(e))
            return [{"type": "api_error", "symbol": "*", "detail": str(e)}]

        balances: dict[str, dict[str, Any]] = {}
        for b in account.get("balances", []):
            balances[str(b.get("asset", ""))] = b

        mismatches: list[dict[str, Any]] = []
        for symbol, pos in local_positions.items():
            if pos.quantity <= 0:
                continue
            base, _quote = _split_asset(symbol)
            bal = balances.get(base)
            exchange_qty = 0.0
            if bal:
                exchange_qty = float(bal.get("free", 0) or 0) + float(bal.get("locked", 0) or 0)
            diff = pos.quantity - exchange_qty
            if abs(diff) > self.tolerance:
                mismatches.append({
                    "type": "mismatch",
                    "symbol": symbol,
                    "local": pos.quantity,
                    "exchange": exchange_qty,
                    "diff": diff,
                })

        # V10.2: 反向遍历交易所余额, 检出「交易所有 / 本地无」盲区(EXCHANGE_ONLY)
        base_to_symbol = {_split_asset(s)[0]: s for s in local_positions}
        for asset, bal in balances.items():
            if asset not in base_to_symbol:
                continue
            symbol = base_to_symbol[asset]
            exchange_qty = float(bal.get("free", 0) or 0) + float(bal.get("locked", 0) or 0)
            local_qty = local_positions[symbol].quantity if symbol in local_positions else 0.0
            if exchange_qty > self.tolerance and local_qty <= self.tolerance:
                mismatches.append({
                    "type": "exchange_only",
                    "symbol": symbol,
                    "local": local_qty,
                    "exchange": exchange_qty,
                    "diff": local_qty - exchange_qty,
                })
        return mismatches

    def reconcile_paper(self, cash: float) -> list[dict[str, Any]]:
        """纸面自检: 现金不得为负(资金被超额卖出/记账错误)"""
        if cash < 0:
            return [{"type": "paper_cash_negative", "cash": cash}]
        return []

    async def reconcile_account(
        self,
        symbol: str,
        local_equity: float,
        last_price: float,
        tolerance_pct: float = 0.02,
    ) -> list[dict[str, Any]]:
        """权益对账(V10): 本地权益 vs 交易所账户权益, 超出容差返回漂移差异

        交易所权益 = 计价资产(USDT)free+locked + base 资产(SOL)free+locked × last_price。
        仅检测与返回差异, 由上层决定是否冻结(持久急停)。
        """
        if self.rest is None:
            return []
        try:
            account = await self.rest.get_account()
        except Exception as e:
            return [{"type": "api_error", "symbol": symbol, "detail": str(e)}]

        base, quote = _split_asset(symbol)
        balances: dict[str, dict[str, Any]] = {}
        for b in account.get("balances", []):
            balances[str(b.get("asset", ""))] = b

        quote_free = 0.0
        base_qty = 0.0
        for asset, bal in balances.items():
            free = float(bal.get("free", 0) or 0)
            locked = float(bal.get("locked", 0) or 0)
            if asset == quote:
                quote_free += free + locked
            elif asset == base:
                base_qty += free + locked
        exchange_equity = quote_free + base_qty * last_price

        if local_equity <= 0:
            return []
        diff = local_equity - exchange_equity
        if abs(diff) / local_equity > tolerance_pct:
            return [{
                "type": "equity_drift",
                "symbol": symbol,
                "local": local_equity,
                "exchange": exchange_equity,
                "diff": diff,
            }]
        return []
