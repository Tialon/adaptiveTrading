"""统一手续费计价器(V11.1 P0-2)

现货 SOLUSDT 手续费只计价 USDT(quote) / SOL(base) 两种资产; 其它计价资产(如 BNB,
币安默认用 BNB 抵扣手续费)无法折算成 quote -> fee_unpriced=True(降级), **绝不静默记为 0**。

单一来源: 成交摄入(`_record_fills`)与成交指标合成(`_compute_fill_metrics`)都走本模块,
消除「两处各自判 asset 口径不一致」的漂移, 保证 fee_quote / fee_valuation_status 口径统一。
"""

from dataclasses import dataclass, field
from typing import Any

ZERO = "zero"
PRICED = "priced"
UNPRICED = "unpriced"


@dataclass
class FillFee:
    """单笔成交手续费估值(quote 口径)"""
    fee_quote: float = 0.0
    valuation_status: str = ZERO  # zero / priced / unpriced
    commission: float = 0.0
    commission_asset: str = ""


@dataclass
class FeeResult:
    """一批成交手续费汇总"""
    fee_quote: float = 0.0
    unpriced: bool = False              # 存在非 quote/base 计价的手续费(无法折算)
    unpriced_assets: list[str] = field(default_factory=list)
    fills: list[FillFee] = field(default_factory=list)


class FeeCalculator:
    """统一手续费计价器(USDT/SOL 可折算; 其它 -> unpriced 降级)"""

    def __init__(self, symbol: str):
        # SOLUSDT -> base=SOL, quote=USDT(按符号拆分, 适配单币系统)
        self.base = symbol[:-4] if len(symbol) > 4 else ""
        self.quote = symbol[-4:]

    def fill_fee(self, fill: dict[str, Any]) -> FillFee:
        """单笔成交手续费折算 quote 口径。

        - commission == 0 -> zero(无手续费)
        - commissionAsset == quote -> fee_quote = commission
        - commissionAsset == base  -> fee_quote = commission × price
        - 其它(BNB 等)           -> unpriced(fee_quote=0, 但显式标记不可计价)
        """
        comm = float(fill.get("commission", 0) or 0)
        asset = str(fill.get("commissionAsset", "") or "").upper()
        price = float(fill.get("price", 0) or 0)
        if comm <= 0:
            return FillFee(fee_quote=0.0, valuation_status=ZERO,
                           commission=comm, commission_asset=asset)
        if asset == self.quote:
            return FillFee(fee_quote=comm, valuation_status=PRICED,
                           commission=comm, commission_asset=asset)
        if asset == self.base:
            return FillFee(fee_quote=comm * price, valuation_status=PRICED,
                           commission=comm, commission_asset=asset)
        return FillFee(fee_quote=0.0, valuation_status=UNPRICED,
                       commission=comm, commission_asset=asset)

    def total(self, fills: list[dict[str, Any]]) -> FeeResult:
        """一批成交的汇总手续费(含 unpriced 降级标记与逐笔明细)"""
        result = FeeResult()
        for f in fills:
            ff = self.fill_fee(f)
            result.fee_quote += ff.fee_quote
            if ff.valuation_status == UNPRICED:
                result.unpriced = True
                if ff.commission_asset not in result.unpriced_assets:
                    result.unpriced_assets.append(ff.commission_asset)
            result.fills.append(ff)
        return result
