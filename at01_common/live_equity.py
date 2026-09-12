"""实盘权益基线播种(V12.6 P0)

**背景(修复的真实缺陷)**: 风控模型的一切都建立在 `settings.risk_initial_equity` 之上 ——

- `RiskManager.equity()`      = `risk_initial_equity + realized + unrealized`
- `max_position_quote`        = `current_equity × risk_max_position_pct`
- `max_sol_exposure_quote`    = `current_equity × risk_max_sol_exposure`(无绝对值覆盖)
- `PortfolioAllocator(initial_equity=...)`
- 各策略构造期的 `single_quote` 回落值
- `DrawdownController.peak_equity`

而该值**出厂默认 100000.0**, 且 `wiring.py` 里唯一会「用真实账户权益覆盖它」的接管逻辑
带条件 `not binance_testnet` —— **只在主网执行**。于是实盘(含测试网真实执行)下:

1. 本地权益恒为配置默认 100000(与真实账户无关)
2. 交易所权益 = 实际余额
3. `reconcile_account` 算出漂移 |local-exchange|/local ≈ **100%**
4. `equity_drift` 在 `reconciliation_matrix` 属 `_KILLED_ALONE`(单发即 KILL)
5. → 急停 → SAFE_MODE → 无成交 → 漂移永不收敛 → **永久锁死**

Pi 实测(`live_testnet`): `reconcile_drift_pct=1.0`、`reconcile_killed=81`、`orders_total=0`。

**本模块的职责**: 在**任何非纸面模式**下, 启动时从交易所账户读取真实权益并作为风控基线,
使本地与交易所同源。这样漂移才是真漂移(该 KILL 时照 KILL), 限额也才是真实资金的比例。

**不做的事**: 不放宽 `equity_drift` 的 KILLED 判定, 不动 `TradingGate`, 不改变纸面语义
(纸面有自己的 `PAPER_INITIAL_CASH` 账)。
"""

from __future__ import annotations

from typing import Any

from at01_common.logger import LoggerMixin


def _split_asset(symbol: str) -> tuple[str, str]:
    """'SOLUSDT' -> ('SOL', 'USDT')。"""
    for quote in ("USDT", "BUSD", "USDC", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, "USDT"


def compute_account_equity(
    account: dict[str, Any], symbol: str, price: float
) -> dict[str, float]:
    """从币安账户快照计算权益(纯函数, 便于单测)。

    权益 = 计价资产(USDT) free+locked + 基础资产(SOL) free+locked × price
    """
    base, quote = _split_asset(symbol)
    cash = 0.0
    qty = 0.0
    for bal in account.get("balances", []) or []:
        asset = str(bal.get("asset", ""))
        free = float(bal.get("free", 0) or 0)
        locked = float(bal.get("locked", 0) or 0)
        if asset == quote:
            cash += free + locked
        elif asset == base:
            qty += free + locked
    return {"cash": cash, "position": qty, "equity": cash + qty * price}


class LiveEquitySeeder(LoggerMixin):
    """实盘权益基线播种器(只读交易所账户, 不产生任何订单)。"""

    def __init__(self, rest_client: Any, symbol: str):
        self.rest = rest_client
        self.symbol = symbol

    async def seed(self) -> dict[str, Any]:
        """读取交易所账户权益。返回 {ok, equity, cash, position, price, error}。

        **只读** —— 仅 `get_price` + `get_account`, 无任何下单/撤单调用。
        """
        try:
            price = float(await self.rest.get_price(self.symbol))
        except Exception as e:
            return {"ok": False, "error": f"取价失败: {e}"}
        try:
            account = await self.rest.get_account()
        except Exception as e:
            return {"ok": False, "error": f"取账户失败: {e}"}

        snap = compute_account_equity(account, self.symbol, price)
        return {"ok": True, "price": price, **snap, "error": ""}


async def seed_live_equity_baseline(settings: Any) -> dict[str, Any]:
    """实盘启动: 用交易所账户真实权益覆盖 `risk_initial_equity`。

    **必须在风控/策略组件构造之前调用** —— 它们在自己的构造期就读取该值
    (`PortfolioAllocator(initial_equity=)`, 各策略的 `single_quote`)。

    返回报告 dict; `ok=False` 时调用方应拒绝启动(fail-closed):
    拿不到真实权益就交易, 等于按虚构的 100000 权益算仓位。
    """
    symbol = settings.symbol_list[0]
    original = settings.risk_initial_equity

    from at10_market.market_rest_client import BinanceRestClient

    client = BinanceRestClient()
    try:
        await client.connect()
        result = await LiveEquitySeeder(client, symbol).seed()
    except Exception as e:
        return {
            "ok": False,
            "symbol": symbol,
            "original": original,
            "error": f"连接交易所失败: {e}",
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not result["ok"]:
        return {"ok": False, "symbol": symbol, "original": original, "error": result["error"]}

    equity = float(result["equity"])
    if equity <= 0:
        # 权益为 0 无法建立基线(且 sol_exposure_ratio 会除零): 拒绝启动, 不用 0 顶替默认值。
        return {
            "ok": False,
            "symbol": symbol,
            "original": original,
            "error": (
                f"交易所账户权益为 {equity}(计价资产与基础资产均为 0)。"
                "需要先入金/领测试网水龙头, 否则无法建立风控基线。"
            ),
        }

    settings.risk_initial_equity = equity
    return {
        "ok": True,
        "symbol": symbol,
        "original": original,
        "seeded": equity,
        "cash": result["cash"],
        "position": result["position"],
        "price": result["price"],
        "error": "",
    }
