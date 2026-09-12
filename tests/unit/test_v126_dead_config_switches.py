"""V12.6 P3: 死开关 / 空转开关 防回归守卫

**背景**: 管理页面 `/admin` 上曾有一个 `MAINNET_READINESS_ENABLED` 开关 —— 它在
`settings.py` 里声明, 但**全代码库从不被读取**(主网就绪自检在 `wiring.py` 中只要
`BINANCE_TESTNET=false` 就无条件执行)。操作者把它关掉, 以为放行了, 实际什么都没变:
**虚假的掌控感比没有开关更糟**。已移除, 并顺带移除四个同类死配置
(`portfolio_profit_sweep_enabled` / `database_pool_size` / `database_max_overflow` /
`regime_hmm_model_path`, 均 0 处真实读取)。

**本测试的作用**: 断言 `FIELD_SPECS` 里每个**可编辑**字段在代码库中**确有一处真实读取**
(形如 `settings.x` / `self.settings.x` / `s.x`)。
以后再往管理页面塞不生效的开关, 这里会红。

**判定口径**: 排除 `config_store.py` —— 它是通用 UI 读写层(`getattr(settings, spec.attr)`),
那里的读取只说明「页面渲染得出来」, 不说明「业务逻辑真的用这个值」。
`settings.py` **保留在搜索范围内**: 像 `web_admin_auth` 这种字段就是在 settings 自身的
属性里被消费的(`admin_auth_disabled`), 排除它会误判成死开关。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from at01_common.config_store import FIELD_SPECS

ROOT = Path(__file__).resolve().parents[2]

CODE_DIRS = (
    "at01_common", "at10_market", "at20_analytics", "at30_strategy", "at40_portfolio",
    "at50_risk", "at60_execution", "at70_journal", "at80_backtest", "at85_optimizer",
    "at90_web",
)
_EXCLUDE_FILES = {"config_store.py"}  # 通用 UI 读写层, 不参与「是否真的被使用」判定

# 已核实「可编辑但值不被任何业务逻辑消费」的字段。
# 与死开关的区别: 它们**被版本/优化器管线按名字引用**(StrategyVersionManager.TRACKED_PARAMS),
# 直接删除字段可能破坏参数追踪 —— 属设计取舍, 需操作者决定「接线还是撤下」。
# 每一项都必须写明理由, 且被 test_known_unconsumed_is_still_accurate 反向校验。
KNOWN_UNCONSUMED: dict[str, str] = {
    "BUY_DIP_PCT": (
        "V2 遗留参数。仅出现在 strategy_version.TRACKED_PARAMS / PARAM_GROUPS 的字符串列表里, "
        "没有任何策略读它的值参与决策 —— 改它不产生任何行为变化, 优化器对它的建议也是空转。"
    ),
    "SELL_PROFIT_PCT": "同上(V2 遗留, 仅进版本快照, 无策略消费)。",
}


def _code_blob() -> str:
    parts = []
    for d in CODE_DIRS:
        for p in (ROOT / d).glob("*.py"):
            if p.name in _EXCLUDE_FILES:
                continue
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    parts.append((ROOT / "run.py").read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _is_read(blob: str, attr: str) -> bool:
    """字段是否被真实读取。要求 `.attr` 形式(声明处是 `attr: type = ...`, 无点, 不会误判)。"""
    return bool(re.search(rf"\.{re.escape(attr)}\b", blob))


@pytest.fixture(scope="module")
def code_blob() -> str:
    return _code_blob()


class TestNoDeadSwitches:
    def test_every_editable_field_is_actually_read(self, code_blob):
        dead: list[str] = []
        for spec in FIELD_SPECS:
            if not spec.editable or spec.key in KNOWN_UNCONSUMED:
                continue
            if not _is_read(code_blob, spec.attr):
                dead.append(f"{spec.key} (Settings.{spec.attr})")
        assert not dead, (
            "以下配置项在管理页面上可编辑, 但业务代码从不读取 —— "
            "操作者改了不会有任何效果(虚假的掌控感)。"
            "要么接线, 要么从 FIELD_SPECS 移除, 要么登记进 KNOWN_UNCONSUMED 并写明理由:\n  "
            + "\n  ".join(dead)
        )

    def test_known_unconsumed_is_still_accurate(self, code_blob):
        """豁免清单不得腐烂: 一旦某字段被接线, 必须从 KNOWN_UNCONSUMED 移除。"""
        for key, reason in KNOWN_UNCONSUMED.items():
            spec = next((s for s in FIELD_SPECS if s.key == key), None)
            assert spec is not None, f"{key} 已不在 FIELD_SPECS, 请从 KNOWN_UNCONSUMED 删除"
            assert reason, f"{key} 必须写明豁免理由"
            assert not _is_read(code_blob, spec.attr), (
                f"{key} 现在有业务读取了 —— 应从 KNOWN_UNCONSUMED 移除, 让它重新受守卫保护"
            )


class TestRemovedDeadFields:
    """清理掉的字段不得复活。"""

    @pytest.mark.parametrize(
        "attr",
        [
            "mainnet_readiness_enabled",
            "portfolio_profit_sweep_enabled",
            "database_pool_size",
            "database_max_overflow",
            "regime_hmm_model_path",
        ],
    )
    def test_field_stays_removed(self, attr):
        from at01_common.settings import Settings

        assert not hasattr(Settings(), attr), (
            f"Settings.{attr} 已被判定为死配置并移除 —— 若确需重新引入, "
            "必须同时接线到真实业务逻辑并更新本测试"
        )

    def test_dead_switch_not_offered_in_admin_ui(self):
        assert "MAINNET_READINESS_ENABLED" not in {s.key for s in FIELD_SPECS}
