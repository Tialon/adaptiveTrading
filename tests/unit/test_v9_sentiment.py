"""V9.0 M3.4: Funding + OI 情绪因子测试

验证: 固定 funding/OI -> 分数/偏向正确; 客户端解析; 关闭时不产出。
"""

import pytest

from at10_market.market_futures_client import BinanceFuturesClient
from at20_analytics.sentiment import SentimentAnalyzer, compute_sentiment


class TestComputeSentiment:
    def test_flat_neutral(self):
        r = compute_sentiment(0.0, 100.0, 100.0, funding_threshold=0.0005)
        assert r.score == pytest.approx(0.0)
        assert r.bias == "neutral"
        assert r.oi_change_pct == pytest.approx(0.0)

    def test_high_funding_crowded_long_contrarian_bearish(self):
        # funding = 2x 阈值 -> 拥挤多头 -> 反向偏空
        r = compute_sentiment(0.001, 100.0, 100.0, funding_threshold=0.0005)
        assert r.score == pytest.approx(-0.5)
        assert r.bias == "bearish"

    def test_oi_surge_bullish(self):
        # OI +10%(>5% 满分) -> 新资金进场 -> 偏多
        r = compute_sentiment(0.0, 110.0, 100.0, funding_threshold=0.0005)
        assert r.score == pytest.approx(0.5)
        assert r.bias == "bullish"
        assert r.oi_change_pct == pytest.approx(0.1)

    def test_prev_oi_none(self):
        r = compute_sentiment(0.0, 100.0, None)
        assert r.oi_change_pct == 0.0


class TestFuturesClient:
    async def test_get_funding_rate(self, monkeypatch):
        c = BinanceFuturesClient(base_url="http://test")

        async def _fake_get(path, params=None):
            assert "fundingRate" in path
            return [{"fundingRate": "0.000100", "fundingTime": 1}]

        monkeypatch.setattr(c, "_get", _fake_get)
        assert await c.get_funding_rate("SOLUSDT") == pytest.approx(0.0001)

    async def test_get_open_interest(self, monkeypatch):
        c = BinanceFuturesClient(base_url="http://test")

        async def _fake_get(path, params=None):
            assert "openInterest" in path
            return {"openInterest": "12345.6"}

        monkeypatch.setattr(c, "_get", _fake_get)
        assert await c.get_open_interest("SOLUSDT") == pytest.approx(12345.6)


class _FakeClient:
    def __init__(self, funding, oi):
        self._funding = funding
        self._oi = oi

    async def get_funding_rate(self, symbol):
        return self._funding

    async def get_open_interest(self, symbol):
        return self._oi


class TestSentimentAnalyzer:
    async def test_disabled_returns_none(self):
        from at01_common.settings import get_settings

        get_settings().sentiment_enabled = False
        a = SentimentAnalyzer(client=_FakeClient(0.001, 100.0))
        assert await a.poll("SOLUSDT") is None

    async def test_enabled_produces_result(self, monkeypatch):
        from at01_common.settings import get_settings

        monkeypatch.setattr(get_settings(), "sentiment_enabled", True)
        a = SentimentAnalyzer(client=_FakeClient(0.001, 110.0))
        r = await a.poll("SOLUSDT")
        assert r is not None
        assert r.oi == 110.0
        assert r.funding_rate == 0.001
        # prev_oi 首轮为 None -> OI 变化 0
        assert r.oi_change_pct == 0.0

        # 第二轮: prev_oi 已更新
        a = SentimentAnalyzer(client=_FakeClient(0.001, 110.0))
        await a.poll("SOLUSDT")
        a2 = _FakeClient(0.001, 121.0)
        a.client = a2
        r2 = await a.poll("SOLUSDT")
        assert r2.oi_change_pct == pytest.approx(0.1)
