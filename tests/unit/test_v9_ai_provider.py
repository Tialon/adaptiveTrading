"""V9.0: AI 供应商化(llm_config 复用)测试

验证: 供应商解析(协议/base_url/key)、未知供应商禁用、Key 缺失禁用、ollama 免 Key。
"""

import pytest

from at50_strategy.llm_config import LLMConfig, list_providers, resolve_provider

# LLMConfig 会读取 OS 环境变量与 .env, 测试先清除可能干扰的变量再构造确定性配置
_LLM_ENV_VARS = [
    "DEFAULT_MODEL", "DEFAULT_PROVIDER", "TEMPERATURE", "MAX_TOKENS",
    "QWEN_API_KEY", "QWEN_BASE_URL", "MIMO_API_KEY", "MIMO_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OLLAMA_BASE_URL",
]


@pytest.fixture(autouse=True)
def _scrub_llm_env(monkeypatch):
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _cfg(**kw) -> LLMConfig:
    """构造不带 .env 的配置(确定性测试)"""
    return LLMConfig(_env_file=None, **kw)


class TestResolveProvider:
    def test_openai_protocol_and_default_url(self):
        pc = resolve_provider("openai", model="gpt-4o", config=_cfg(openai_api_key="k"))
        assert pc.protocol == "openai"
        assert pc.base_url == "https://api.openai.com/v1"
        assert pc.api_key == "k"
        assert pc.model == "gpt-4o"
        assert pc.requires_key is True

    def test_anthropic_protocol(self):
        pc = resolve_provider("anthropic", config=_cfg(anthropic_api_key="ck"))
        assert pc.protocol == "anthropic"
        assert pc.base_url == "https://api.anthropic.com"
        assert pc.api_key == "ck"

    def test_qwen_mimo_deepseek_openai_compatible(self):
        for name, url in [
            ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("mimo", "https://token-plan-cn.xiaomimimo.com/v1"),
            ("deepseek", "https://api.deepseek.com/v1"),
        ]:
            pc = resolve_provider(name, config=_cfg())
            assert pc.protocol == "openai", name
            assert pc.base_url == url, name

    def test_ollama_no_key_required(self):
        pc = resolve_provider("ollama", config=_cfg())
        assert pc.protocol == "ollama"
        assert pc.base_url == "http://localhost:11434"
        assert pc.requires_key is False

    def test_unknown_provider_returns_none(self):
        assert resolve_provider("anthropicX", config=_cfg()) is None

    def test_default_provider_used_when_none(self):
        pc = resolve_provider(config=_cfg(default_provider="deepseek"))
        assert pc.protocol == "openai"
        assert pc.base_url == "https://api.deepseek.com/v1"

    def test_explicit_override_wins(self):
        pc = resolve_provider(
            "openai", base_url="https://gw.example.com/v1", api_key="override",
            config=_cfg(openai_api_key="builtin"),
        )
        assert pc.base_url == "https://gw.example.com/v1"
        assert pc.api_key == "override"

    def test_list_providers(self):
        assert {"anthropic", "openai", "qwen", "mimo", "deepseek", "ollama"} <= set(list_providers())


class TestAIAdvisorProvider:
    def test_unknown_provider_disables(self, monkeypatch):
        from at01_common.settings import get_settings
        from at50_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "unknown-provider")
        monkeypatch.setattr(s, "ai_enabled", True)
        assert AIAdvisor().enabled is False

    def test_missing_key_disables(self, monkeypatch):
        import at50_strategy.llm_config as lc
        from at01_common.settings import get_settings
        from at50_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "openai")
        monkeypatch.setattr(s, "ai_enabled", True)
        monkeypatch.setattr(s, "ai_base_url", "")
        monkeypatch.setattr(s, "ai_api_key", "")
        monkeypatch.setattr(lc, "get_llm_config", lambda: LLMConfig(_env_file=None))
        assert AIAdvisor().enabled is False

    def test_ollama_enabled_without_key(self, monkeypatch):
        import at50_strategy.llm_config as lc
        from at01_common.settings import get_settings
        from at50_strategy.strategy_ai_advisor import AIAdvisor

        s = get_settings()
        monkeypatch.setattr(s, "ai_provider", "ollama")
        monkeypatch.setattr(s, "ai_enabled", True)
        monkeypatch.setattr(s, "ai_base_url", "")
        monkeypatch.setattr(s, "ai_api_key", "")
        monkeypatch.setattr(lc, "get_llm_config", lambda: LLMConfig(_env_file=None))
        a = AIAdvisor()
        assert a.enabled is True
        assert a.protocol == "ollama"
