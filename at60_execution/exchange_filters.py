"""
交易规则过滤器(V10.5)

从 /api/v3/exchangeInfo 解析 LOT_SIZE / PRICE_FILTER / NOTIONAL(回退 MIN_NOTIONAL),
下单前对齐 stepSize / tickSize / minQty / minNotional, 避免被币安 4xx 拒绝。

纯函数(不查网络、不改记账): 解析与调整逻辑独立, 便于单元测试。
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_DOWN, Decimal
from typing import Optional


def _dec(value) -> Decimal:
    """安全转 Decimal(容忍 None / 空串)"""
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass
class SymbolFilters:
    """单个标的的交易规则(缺省字段为 0 = 不限制)"""

    symbol: str
    min_qty: Decimal = Decimal("0")
    max_qty: Optional[Decimal] = None
    step_size: Decimal = Decimal("0")
    min_price: Decimal = Decimal("0")
    max_price: Optional[Decimal] = None
    tick_size: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("0")

    @classmethod
    def from_exchange_info(cls, symbol: str, data: dict) -> "SymbolFilters":
        """从 exchangeInfo 响应解析(兼容单标的/全量两种返回)"""
        sym_info = None
        for s in data.get("symbols", []):
            if s.get("symbol") == symbol:
                sym_info = s
                break
        if sym_info is None:
            raise ValueError(f"exchangeInfo 未包含 {symbol}")

        by_type = {f.get("filterType"): f for f in sym_info.get("filters", [])}
        lot = by_type.get("LOT_SIZE", {})
        price = by_type.get("PRICE_FILTER", {})
        # 币安现用 NOTIONAL(取代旧 MIN_NOTIONAL, 含 minNotional/applyMinToMarket);
        # 旧符号仍可能返回 MIN_NOTIONAL, 故优先 NOTIONAL、回退 MIN_NOTIONAL。
        notional = by_type.get("NOTIONAL", by_type.get("MIN_NOTIONAL", {}))

        def _opt(f, key) -> Optional[Decimal]:
            v = f.get(key)
            return _dec(v) if v not in (None, "") else None

        return cls(
            symbol=symbol,
            min_qty=_dec(lot.get("minQty")),
            max_qty=_opt(lot, "maxQty"),
            step_size=_dec(lot.get("stepSize")),
            min_price=_dec(price.get("minPrice")),
            max_price=_opt(price, "maxPrice"),
            tick_size=_dec(price.get("tickSize")),
            min_notional=_dec(notional.get("minNotional")),
        )

    # ---------- 调整 ----------

    def round_down_to_step(self, value: Decimal, step: Decimal) -> Decimal:
        """向下取整到 step 的整数倍(数量精度对齐)"""
        if step is None or step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step

    def round_to_tick(self, value: Decimal, tick: Decimal) -> Decimal:
        """四舍五入到 tick 的整数倍(价格精度对齐)"""
        if tick is None or tick <= 0:
            return value
        return (value / tick).to_integral_value(rounding=ROUND_HALF_DOWN) * tick

    def adjust(self, quantity: Decimal, price: Decimal) -> tuple[Decimal, Decimal, list[str]]:
        """调整数量/价格到交易所规则, 返回 (调整后数量, 调整后价格, 致命违规列表)

        数量向下取整到 stepSize(宁可少买少卖, 不超规则);
        价格四舍五入到 tickSize。违规 = 调整后仍低于 minQty / 高于 maxQty /
        名义价值低于 minNotional / 价格越界 —— 这些应直接拒绝下单。
        """
        violations: list[str] = []
        qty = self.round_down_to_step(quantity, self.step_size)
        px = self.round_to_tick(price, self.tick_size)

        if self.min_qty > 0 and qty < self.min_qty:
            violations.append(f"qty {qty} < minQty {self.min_qty}")
        if self.max_qty is not None and qty > self.max_qty:
            violations.append(f"qty {qty} > maxQty {self.max_qty}")
        if self.min_price > 0 and px < self.min_price:
            violations.append(f"price {px} < minPrice {self.min_price}")
        if self.max_price is not None and px > self.max_price:
            violations.append(f"price {px} > maxPrice {self.max_price}")
        if self.min_notional > 0 and qty * px < self.min_notional:
            violations.append(
                f"notional {qty * px} < minNotional {self.min_notional}"
            )
        return qty, px, violations
