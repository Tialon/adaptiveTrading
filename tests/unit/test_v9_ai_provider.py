"""V9.0: AI 供应商化(llm_config 复用)测试

验证: 供应商解析(base_url/key)、未知供应商禁用、Key 缺失禁用。
"""

import pytest

from at30_strategy.llm_config import LLMConfig, list_providers, resolve_provider

# LLMConfig 会读取 OS 环境变量与 .env, 测试先清除可能干扰的变量再构造确定性配置
_LLM_ENV_VARS = [
    "DEFAULT_MODEL", "DEFAULT_PROVIDER", "TEMPERATURE", "MAX_TOKENS",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "QWEN_API_KEY", "QWEN_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
]


@pytest.fixture(autouse=True)
def _scrub_llm_env(monkeypatch):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _cfg(**kw) -> LLMConfig:
    """构造不带 .env 的配置(确定性测试)"""
    return LLMConfig(_env_file=None, **kw)


class TestResolveProvider:
    def test_openai(self):
        pc = resolve_provider("openai", model="gpt-4o-mini", config=_cfg(openai_api_key="k"))
        assert pc.base_url == "https://api.openai.com/v1"
        assert pc.api_key == "k"
        assert pc.model == "gpt-4o-mini"

    def test_qwen(self):
        pc = resolve_provider("qwen", config=_cfg(qwen_api_key="qk"))
        assert pc.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert pc.api_key == "qk"

    def test_deepseek(self):
        pc = resolve_provider("deepseek", config=_cfg(deepseek_api_key="dk"))
        assert pc.base_url == "https://api.deepseek.com/v1"
        assert pc.api_key == "dk"

    def test_unknown_provider_returns_none(self):
        # anthropic / ollama 已移除, 应返回 None
        assert resolve_provider("anthropic", config=_cfg()) is None
        assert resolve_provider("ollama", config=_cfg()) is None
        assert resolve_provider("mimo", config=_cfg()) is None

    def test_default_provider_used_when_none(self):
        pc = resolve_provider(config=_cfg(default_provider="deepseek"))
        assert pc.base_url == "https://api.deepseek.com/v1"

    def test_explicit_override_wins(self):
        pc = resolve_provider(
            "openai", base_url="https://gw.example.com/v1", api_key="override",
            config=_cfg(openai_api_key="builtin"),
        )
        assert pc.base_url == "https://gw.example.com/v1"
        assert pc.api_key == "override"

    def test_list_providers(self):
        assert list_providers() == ["deepseek", "openai", "qwen"]


class TestAIAdvisorProvider:
    def test_unknown_provider_disables(self, monkeypatch):
        from at01_common.settings import get_settings
        from at30_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "anthropic")  # 已移除
        monkeypatch.setattr(s, "ai_enabled", True)
        assert AIAdvisor().enabled is False

    def test_missing_key_disables(self, monkeypatch):
        import at30_strategy.llm_config as lc
        from at01_common.settings import get_settings
        from at30_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "openai")
        monkeypatch.setattr(s, "ai_enabled", True)
        monkeypatch.setattr(s, "ai_base_url", "")
        monkeypatch.setattr(s, "ai_api_key", "")
        monkeypatch.setattr(lc, "get_llm_config", lambda: LLMConfig(_env_file=None))
        assert AIAdvisor().enabled is False

    def test_enabled_with_key(self, monkeypatch):
        import at30_strategy.llm_config as lc
        from at01_common.settings import get_settings
        from at30_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "qwen")
        monkeypatch.setattr(s, "ai_enabled", True)
        monkeypatch.setattr(s, "ai_base_url", "")
        monkeypatch.setattr(s, "ai_api_key", "")
        monkeypatch.setattr(lc, "get_llm_config", lambda: LLMConfig(_env_file=None, qwen_api_key="qk"))
        a = AIAdvisor()
        assert a.enabled is True
        assert a.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
