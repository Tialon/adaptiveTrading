"""V12.6 P2: 启动守卫显式解锁 —— 回归锚定

**边界是本设计的核心**(详见 `at01_common/guard_override.py`):

- ✅ 解锁**启动前**守卫(`mainnet_blocked_reason` / 就绪自检 / 测试网闸门)——一次性预检
- ❌ **不解锁**运行时闸门 `TradingGate` —— 每一笔单的判定, 动它等于拿错账本下真钱单

因此本文件既要证明「解锁真的生效」, 也要证明「默认行为一点没变」和「运行时闸门没被碰」。
"""

from __future__ import annotations

import time

import pytest

from at01_common.guard_override import CONFIRM_PHRASE, parse_guard_override

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


class TestParseGuardOverride:
    """fail-closed: 只有「格式对 + 短语逐字对 + 未过期」三者同时成立才放行。"""

    def test_not_configured_is_inactive(self):
        assert parse_guard_override("").active is False
        assert parse_guard_override("   ").active is False

    def test_valid_override_is_active(self):
        o = parse_guard_override(f"{FUTURE}:{CONFIRM_PHRASE}")
        assert o.active is True
        assert o.expires_at > 0
        assert o.reason

    def test_wrong_phrase_rejected(self):
        """短语错一个字符都不行 —— 防手滑是本开关存在的理由。"""
        o = parse_guard_override(f"{FUTURE}:i-know-this-is-mainnet")  # 大小写不同
        assert o.active is False
        assert "短语" in o.error
        assert parse_guard_override(f"{FUTURE}:{CONFIRM_PHRASE}X").active is False

    def test_expired_rejected(self):
        o = parse_guard_override(f"{PAST}:{CONFIRM_PHRASE}")
        assert o.active is False
        assert "过期" in o.error

    def test_expiry_is_evaluated_live(self):
        """到期时间不是摆设: 同一串在到期前 active、到期后 inactive。"""
        raw = f"2030-01-01T00:00:00Z:{CONFIRM_PHRASE}"
        before = parse_guard_override(raw, now=time.time())
        after = parse_guard_override(raw, now=4102444800.0)  # 2100
        assert before.active is True
        assert after.active is False

    @pytest.mark.parametrize(
        "raw",
        [
            CONFIRM_PHRASE,           # 没有时间部分
            f"{FUTURE}:",             # 空短语
            f"not-a-time:{CONFIRM_PHRASE}",
            ":",                      # 全空
        ],
    )
    def test_malformed_rejected(self, raw):
        assert parse_guard_override(raw).active is False

    def test_iso_with_colons_is_parsed(self):
        """ISO8601 自带冒号, 必须从**最后一个**冒号切分。"""
        o = parse_guard_override(f"2099-06-15T12:30:45+00:00:{CONFIRM_PHRASE}")
        assert o.active is True


class TestDefaultBehaviourUnchanged:
    """不设 `GUARD_OVERRIDE` 时, 行为必须与以往逐字一致。"""

    def test_settings_default_is_empty(self):
        from at01_common.settings import Settings

        assert Settings().guard_override == ""

    def test_mainnet_still_blocked_by_default(self):
        """主网默认拦截不得因本功能被放宽。"""
        from at01_common.settings import Settings

        s = Settings(paper_trading=False, binance_testnet=False, live_trading_confirm="",
                     guard_override="")
        assert s.mainnet_blocked_reason()  # 有理由 = 被拦

    def test_override_does_not_change_block_reason_fn(self):
        """解锁是 wiring 层的**跳过**, 不改判定函数本身 —— 判定仍然说真话。"""
        from at01_common.settings import Settings

        s = Settings(paper_trading=False, binance_testnet=False, live_trading_confirm="",
                     guard_override=f"{FUTURE}:{CONFIRM_PHRASE}")
        assert s.mainnet_blocked_reason(), "判定函数仍应给出拦截理由(由 wiring 决定是否跳过)"


class TestOperatorStatusExposure:
    """解锁状态必须**可见** —— 隐形状态比没有状态更危险。"""

    def test_not_configured_is_quiet(self):
        """没配就不显示横幅: 天天挂个「未解锁」红条会稀释告警的意义。"""
        from at01_common.settings import Settings
        from at90_web.web_operator_status import build_operator_status

        body = build_operator_status(Settings(), {})
        assert body["guard_override"]["configured"] is False
        assert body["guard_override"]["active"] is False

    def test_active_override_is_exposed_with_banner(self):
        from at01_common.settings import Settings
        from at90_web.web_operator_status import build_operator_status

        s = Settings(guard_override=f"{FUTURE}:{CONFIRM_PHRASE}")
        g = build_operator_status(s, {})["guard_override"]
        assert g["active"] is True
        assert g["configured"] is True
        assert g["banner"], "生效时必须带横幅文案"

    def test_broken_override_is_still_visible(self):
        """填了但没生效(过期/写错)也必须看得见, 否则会误以为已解锁。"""
        from at01_common.settings import Settings
        from at90_web.web_operator_status import build_operator_status

        s = Settings(guard_override=f"{PAST}:{CONFIRM_PHRASE}")
        g = build_operator_status(s, {})["guard_override"]
        assert g["configured"] is True
        assert g["active"] is False
        assert "过期" in g["error"]


class TestTradingGateUntouched:
    """**最关键的一条**: 解锁不得影响运行时交易闸门。"""

    def test_gate_has_no_reference_to_guard_override(self):
        """闸门代码里不得出现 guard_override —— 结构上保证它够不着。"""
        import inspect

        from at50_risk.trading_gate import TradingGate

        src = inspect.getsource(TradingGate)
        assert "guard_override" not in src
        assert "GUARD_OVERRIDE" not in src

    def test_gate_still_blocks_on_unhealthy_market(self):
        """解锁场景下, 行情不健康仍必须禁止开仓。"""
        from at50_risk.risk_manager import RiskManager
        from at50_risk.system_lifecycle import SystemLifecycle
        from at50_risk.trading_gate import TradingGate

        lc = SystemLifecycle()
        # 推进到可交易态(否则会先被「未就绪(INIT)」拦下, 测不到行情维度)
        lc.warm_up(); lc.sync(); lc.self_check(); lc.ready(); lc.start_trading()
        gate = TradingGate(RiskManager(), lc)

        assert gate.can_open_position()[0] is True  # 基线: 全部健康时可开仓
        gate.market_data_healthy = False
        ok, reason = gate.can_open_position()
        assert ok is False
        assert "行情" in reason
