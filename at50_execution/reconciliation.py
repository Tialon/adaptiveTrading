"""
交易所持仓对账(V8)

比较本地总账持仓与交易所账户余额, 检测不一致(缺币/多币/API 异常)。
纸面模式下仅做现金非负自检(无交易所余额可对)。

原则: 只检测与告警, 由上层(RiskManager.pause)决定是否暂停交易。
"""

from typing import Any, Optional

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
        """实盘对账: 本地持仓 vs 交易所余额, 返回差异列表"""
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
        return mismatches

    def reconcile_paper(self, cash: float) -> list[dict[str, Any]]:
        """纸面自检: 现金不得为负(资金被超额卖出/记账错误)"""
        if cash < 0:
            return [{"type": "paper_cash_negative", "cash": cash}]
        return []
