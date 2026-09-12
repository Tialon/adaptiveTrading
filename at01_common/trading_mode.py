"""运行模式解析(V12.7)

**这个模块解决什么**: 操作者不该被 `PAPER_TRADING` / `BINANCE_TESTNET` /
`RUN_TESTNET_TRADING` / `LIVE_TRADING_CONFIRM` / `MAINNET_API_SCOPE_CONFIRMED`
这堆布尔量的组合关系干扰。对外只有三个模式:

    模拟(paper) / 测试网(testnet) / 实盘(live)

内部安全机制照旧复杂 —— 本模块**只做翻译**, 不削弱任何守卫。

## 三个模式

| 模式 | 语义 | 真实下单 |
|------|------|:--------:|
| `paper` | 本地模拟成交 | ❌ |
| `testnet` | Binance 测试网真实下单 | ✅ 假钱 |
| `live` | Binance 主网真实下单 | ✅ **真钱** |

## 行情数据源是**正交**的, 不是第四种模式

测试网行情稀薄、人造; 想看真实流动性与微观结构, 可以用 `paper` + 主网行情。
所以「行情数据源」独立于「模式」:

    TradingMode      ×   MarketDataSource
    paper / testnet / live      testnet / mainnet

页面只显示三个模式; 主网行情是**高级选项**。

## 单一事实来源

`TRADING_MODE` 一旦设置即为权威, 解析结果**驱动**内部字段(写回 settings),
于是既有守卫读到的仍是自洽的值 —— 不需要改动 `TradingGate` / `RiskManager` /
`ExecutionEngine` 的任何逻辑。

**未设 `TRADING_MODE` 时按旧配置推导**(见 `resolve_mode`), 保证老 `.env` 照常可用。
组合无法确定时 **FAIL CLOSED, 绝不猜**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TradingMode(str, Enum):
    """操作者可见的三个模式。"""

    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class MarketDataSource(str, Enum):
    """行情数据源(与模式正交)。"""

    TESTNET = "testnet"
    MAINNET = "mainnet"


# V13: 对外只暴露「模拟 / 测试 / 实盘」三个词(任务书 P0「模式模型简化」)。
# 注意「测试网」这个词在**技术文档与代码注释**里仍照常用(它指 Binance Testnet 本身),
# 只有**操作者界面上的模式名**统一成「测试」—— 界面上多一个字就多一分「我该选哪个」的犹豫。
MODE_LABELS: dict[TradingMode, str] = {
    TradingMode.PAPER: "模拟",
    TradingMode.TESTNET: "测试",
    TradingMode.LIVE: "实盘",
}

MODE_DESCRIPTIONS: dict[TradingMode, str] = {
    TradingMode.PAPER: "本地模拟成交, 不产生任何真实订单。",
    TradingMode.TESTNET: "连接 Binance 测试网, 真实下单但使用假钱。",
    TradingMode.LIVE: "连接 Binance 主网, **真实资金**产生交易。",
}


# 每个模式**决定**的内部字段(解析器会把这些写回 settings)。
# 注意这里**不含** `live_trading_confirm` / `mainnet_api_scope_confirmed` ——
# 它们是"我确认"的显式输入, 不是"我是什么"的推导结果。理由见 resolve_mode 文档。
MODE_DERIVED: dict[TradingMode, dict[str, Any]] = {
    TradingMode.PAPER: {
        "paper_trading": True,
    },
    TradingMode.TESTNET: {
        "paper_trading": False,
        "binance_testnet": True,
        "run_testnet_trading": "1",
    },
    TradingMode.LIVE: {
        "paper_trading": False,
        "binance_testnet": False,
        "live_trading_confirm": "true",
    },
}

# LIVE 额外**要求**操作者已显式给出的字段(不由解析器代填)。
LIVE_REQUIRED_CONFIRMS: tuple[str, ...] = ("live_trading_confirm", "mainnet_api_scope_confirmed")


@dataclass
class ModeResolution:
    """解析结果。`error` 非空 → 调用方必须 fail-closed(拒绝启动)。"""

    mode: TradingMode | None = None
    market_data_source: MarketDataSource = MarketDataSource.TESTNET
    source: str = ""  # "TRADING_MODE" | "legacy" | ""
    derived: dict[str, Any] = field(default_factory=dict)  # 要写回 settings 的字段
    error: str = ""
    legacy_hint: str = ""

    @property
    def ok(self) -> bool:
        return self.mode is not None and not self.error

    @property
    def label(self) -> str:
        return MODE_LABELS.get(self.mode, "未知") if self.mode else "未知"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value if self.mode else "",
            "mode_label": self.label,
            "market_data_source": self.market_data_source.value,
            "source": self.source,
            "error": self.error,
        }


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() == "true"


def _is_one(raw: Any) -> bool:
    return str(raw or "").strip() == "1"


def _derive_legacy(
    *, paper_trading: bool, binance_testnet: bool, run_testnet_trading: Any,
    live_trading_confirm: Any, mainnet_api_scope_confirmed: bool,
) -> tuple[TradingMode | None, str]:
    """按旧配置推导模式。返回 `(mode | None, 失败原因)`。

    推导不出来时**返回 None 而不是猜一个** —— 宁可拒绝启动, 也不让操作者
    以为自己在 A 模式而实际跑在 B 模式。
    """
    if paper_trading:
        return TradingMode.PAPER, ""
    if binance_testnet:
        if _is_one(run_testnet_trading):
            return TradingMode.TESTNET, ""
        return None, (
            "旧配置无法确定模式: PAPER_TRADING=false + BINANCE_TESTNET=true 但未设 "
            "RUN_TESTNET_TRADING=1。请显式设 TRADING_MODE=testnet, 或补上 RUN_TESTNET_TRADING=1"
        )
    if _truthy(live_trading_confirm):
        return TradingMode.LIVE, ""
    return None, (
        "旧配置无法确定模式: PAPER_TRADING=false + BINANCE_TESTNET=false 但未设 "
        "LIVE_TRADING_CONFIRM=true。请显式设 TRADING_MODE=live(并完成实盘确认), "
        "或设 TRADING_MODE=paper"
    )


def resolve_mode(
    *,
    trading_mode: str = "",
    paper_trading: bool = True,
    binance_testnet: bool = True,
    run_testnet_trading: Any = "",
    live_trading_confirm: Any = "",
    mainnet_api_scope_confirmed: bool = False,
    explicitly_set: frozenset[str] | set[str] = frozenset(),
) -> ModeResolution:
    """解析运行模式。

    **优先级**: `TRADING_MODE` > 旧配置推导。两者都有且**明显冲突**时 fail-closed。

    `explicitly_set` 是"操作者在配置里显式写了的字段名集合"(pydantic 的
    `model_fields_set`)。只有显式写下的旧字段才参与冲突判定 —— 否则默认值会
    被误当成操作者的意图。

    ## 为什么 LIVE 的两个确认字段不由本函数代填

    它们正是主网守卫 `mainnet_blocked_reason()` 要求的输入。解析器代填等于
    "配置里写一个词就自动满足了人工确认", 把**刻意确认**变成**配置副作用**。
    因此本函数把它们当**要求**校验: `TRADING_MODE=live` 但确认未给 → fail-closed。
    页面上的实盘确认弹窗负责写这两个标志, 那人工作用才是真实的。
    """
    raw_mode = (trading_mode or "").strip().lower()

    if raw_mode:
        try:
            target = TradingMode(raw_mode)
        except ValueError:
            return ModeResolution(
                error=(
                    f"TRADING_MODE 取值非法: {trading_mode!r}。"
                    f"只接受: {', '.join(m.value for m in TradingMode)}"
                )
            )
        derived = dict(MODE_DERIVED[target])

        # 冲突检查: 操作者显式写下的旧字段若与目标模式矛盾 → 不静默选择, 直接报错
        conflicts: list[str] = []
        for key, want in derived.items():
            if key not in explicitly_set:
                continue
            got = {
                "paper_trading": paper_trading,
                "binance_testnet": binance_testnet,
                "run_testnet_trading": run_testnet_trading,
                "live_trading_confirm": live_trading_confirm,
            }.get(key)
            # 显式**留空**不算冲突 —— 那表示"我没表态", 不是"我要它为空"。
            # (例: `.env` 里留着 `RUN_TESTNET_TRADING=`, 不应挡住 TRADING_MODE=testnet)
            if isinstance(got, str) and not got.strip():
                continue
            if str(got).strip().lower() != str(want).strip().lower():
                conflicts.append(
                    f"{key} 显式设为 {got!r}, 但 TRADING_MODE={target.value} 要求 {want!r}"
                )
        if conflicts:
            return ModeResolution(
                error="配置冲突(不静默选择其一, 请修正后重启): " + "; ".join(conflicts)
            )

        # LIVE 的两个确认必须是显式的, 不由解析器代填
        if target is TradingMode.LIVE:
            missing = [
                name for name in LIVE_REQUIRED_CONFIRMS
                if not (
                    _truthy(live_trading_confirm)
                    if name == "live_trading_confirm"
                    else mainnet_api_scope_confirmed
                )
            ]
            if missing:
                return ModeResolution(
                    mode=target,
                    source="TRADING_MODE",
                    error=(
                        f"TRADING_MODE=live 需要操作者显式确认: {', '.join(m.upper() for m in missing)} 未给出。"
                        "实盘确认必须是一次**人工动作** —— 请在管理页面的实盘确认弹窗中操作, "
                        "或自己在配置里显式写上这两项。此处刻意不代填。"
                    ),
                )
            # 确认已给 → 不再需要解析器写 live_trading_confirm(它已经是 true)
            derived.pop("live_trading_confirm", None)

        return ModeResolution(
            mode=target,
            # 行情源取**解析后**的值 —— TRADING_MODE 权威时, 输入的 binance_testnet
            # 可能是即将被覆盖的遗留值(例如遗留 true 但目标是 live)。
            market_data_source=(
                MarketDataSource.TESTNET
                if derived.get("binance_testnet", binance_testnet)
                else MarketDataSource.MAINNET
            ),
            source="TRADING_MODE",
            derived=derived,
        )

    legacy_mode, why = _derive_legacy(
        paper_trading=paper_trading,
        binance_testnet=binance_testnet,
        run_testnet_trading=run_testnet_trading,
        live_trading_confirm=live_trading_confirm,
        mainnet_api_scope_confirmed=mainnet_api_scope_confirmed,
    )
    if legacy_mode is None:
        return ModeResolution(error=why)

    return ModeResolution(
        mode=legacy_mode,
        market_data_source=(
            MarketDataSource.TESTNET if binance_testnet else MarketDataSource.MAINNET
        ),
        source="legacy",
        derived={},  # 旧配置已是权威, 不回写
        legacy_hint=(
            "当前按旧配置推导模式。建议显式设 TRADING_MODE="
            f"{legacy_mode.value}(三选一: paper/testnet/live) —— 以后只需改这一个值。"
        ),
    )
