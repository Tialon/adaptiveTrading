"""主网只读接管(V12 §10-11)

首次主网启动前的只读接管步骤: 拉取账户余额 / SOL / 挂单 / 成交历史, 产出
账户快照、持仓快照、交易所真相对账与对账结果, 并记录 HODL 基准基线(§24)。
**只读** —— 不下单、不撤单、不改账; 把交易所既有 SOL 视为「初始持仓」(§11),
不机械清仓、不强制再平衡到三桶目标。

对账口径(首次接管):
- 账户快照成功(API 可达);
- 无「意外挂单」(我们尚未下单, 任何交易所挂单都需人工复核后再启动交易);
- 本地持仓与交易所 SOL 余额一致(本地有持仓时); 本地空仓 + 交易所有 SOL
  = 初始持仓(§11, 记基线, 不视为漂移)。
任一不满足 → `allowed=False`, 由上层停在 READY 不交易(不自行下单)。
"""

from __future__ import annotations

from typing import Any

from at01_common.logger import LoggerMixin
from at60_execution.reconciliation import _split_asset


class MainnetTakeover(LoggerMixin):
    """主网只读接管器"""

    def __init__(
        self,
        rest_client: Any = None,
        symbol: str = "SOLUSDT",
        hodl_benchmark: Any = None,
    ):
        self.rest = rest_client
        self.symbol = symbol
        self.hodl = hodl_benchmark

    # ---------- 快照 ----------

    async def snapshot(self) -> dict[str, Any]:
        """只读拉取账户 / 挂单 / 成交历史 / 价格, 返回结构化快照。

        成功返回 `{"ok": True, ...}`; 失败返回 `{"ok": False, "error": ...}`(不抛)。
        """
        base, quote = _split_asset(self.symbol)
        if self.rest is None:
            return {"ok": False, "error": "rest_client 未注入", "symbol": self.symbol}

        try:
            account = await self.rest.get_account()
        except Exception as e:  # noqa: BLE001  记录真实异常信息供人工排查
            self.logger.exception("主网接管获取账户失败")
            return {"ok": False, "error": f"get_account 失败: {e}", "symbol": self.symbol}

        try:
            price = float(await self.rest.get_price(self.symbol))
        except Exception as e:  # noqa: BLE001
            self.logger.exception("主网接管获取价格失败")
            return {"ok": False, "error": f"get_price 失败: {e}", "symbol": self.symbol}

        try:
            open_orders = await self.rest.get_open_orders(self.symbol) or []
        except Exception as e:  # noqa: BLE001
            self.logger.warning("主网接管获取挂单失败", error=str(e))
            open_orders = []

        try:
            recent_trades = await self.rest.get_my_trades(self.symbol, limit=50) or []
        except Exception as e:  # noqa: BLE001
            self.logger.warning("主网接管获取成交历史失败", error=str(e))
            recent_trades = []

        balances: dict[str, Any] = {
            str(b.get("asset", "")): b for b in account.get("balances", [])
        }

        def _amt(asset: str, key: str) -> float:
            bal = balances.get(asset)
            return float(bal.get(key, 0) or 0) if bal else 0.0

        usdt_free, usdt_locked = _amt(quote, "free"), _amt(quote, "locked")
        sol_free, sol_locked = _amt(base, "free"), _amt(base, "locked")
        usdt_total = usdt_free + usdt_locked
        sol_total = sol_free + sol_locked

        return {
            "ok": True,
            "symbol": self.symbol,
            "price": price,
            "account": {
                "quote": quote,
                "usdt_free": usdt_free,
                "usdt_locked": usdt_locked,
                "usdt_total": usdt_total,
                "base": base,
                "sol_free": sol_free,
                "sol_locked": sol_locked,
                "sol_total": sol_total,
            },
            "equity": usdt_total + sol_total * price,
            "position": {"sol_qty": sol_total, "sol_price": price},
            "open_orders": [
                {
                    "order_id": str(o.get("orderId")),
                    "side": o.get("side"),
                    "price": o.get("price"),
                    "orig_qty": o.get("origQty"),
                    "executed_qty": o.get("executedQty"),
                    "status": o.get("status"),
                }
                for o in open_orders
            ],
            "open_order_count": len(open_orders),
            "recent_trades": [
                {
                    "order_id": str(t.get("orderId")),
                    "side": "BUY" if t.get("isBuyer") else "SELL",
                    "price": t.get("price"),
                    "qty": t.get("qty"),
                }
                for t in recent_trades[:20]
            ],
            "recent_trade_count": len(recent_trades),
        }

    # ---------- 接管 ----------

    async def takeover(self, local_sol_qty: float = 0.0) -> dict[str, Any]:
        """只读接管: 快照 + 对账 + 记录 HODL 基线, 返回结构化结果。

        `local_sol_qty` 为本地持仓(重启场景); 首次接管为空 0(初始持仓由交易所读取)。
        结果 `allowed=False` 表示对账未通过, 上层应停在 READY 不交易。
        """
        snap = await self.snapshot()
        if not snap.get("ok"):
            return {
                "allowed": False,
                "blocked_reasons": [f"账户快照失败: {snap.get('error')}"],
                "snapshot": snap,
                "baseline_recorded": False,
                "reconciliation": {"api_ok": False},
            }

        reasons: list[str] = []
        exchange_sol_qty = snap["position"]["sol_qty"]
        open_orders = snap["open_order_count"]

        if open_orders > 0:
            reasons.append(f"存在 {open_orders} 笔意外挂单, 需人工复核后再启动交易")

        position_drift = abs(local_sol_qty - exchange_sol_qty) > 1e-6
        if local_sol_qty > 0 and position_drift:
            reasons.append(
                f"本地持仓 {local_sol_qty} 与交易所 SOL 余额 {exchange_sol_qty} 漂移"
            )

        baseline_recorded = False
        if self.hodl is not None:
            baseline_recorded = await self.hodl.record_baseline(
                equity=snap["equity"],
                sol_qty=exchange_sol_qty,
                sol_price=snap["price"],
            )

        reconciliation = {
            "api_ok": True,
            "open_orders": open_orders,
            "unexpected_open_orders": open_orders > 0,
            "exchange_sol_qty": exchange_sol_qty,
            "local_sol_qty": local_sol_qty,
            "position_drift": position_drift,
            "initial_position_detected": exchange_sol_qty > 0 and local_sol_qty <= 0,
        }

        return {
            "allowed": not reasons,
            "blocked_reasons": reasons,
            "snapshot": snap,
            "baseline_recorded": baseline_recorded,
            "reconciliation": reconciliation,
        }
