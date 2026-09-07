"""
AI 模型配置中心(复用 bianAgent `src/config/llm_config.py` 的供应商选择模式)

统一管理 AI 顾问的多供应商配置, 支持 anthropic / openai / qwen / mimo / deepseek / ollama。
所有 API Key 从 `.env` 读取(默认空), **不硬编码在代码中**。

用法:
    from at50_strategy.llm_config import get_llm_config, resolve_provider

    cfg = get_llm_config()                      # 读取 .env
    pc = resolve_provider("qwen", "qwen-turbo") # -> ProviderConfig(protocol/base_url/api_key/model)
    pc = resolve_provider(model="gpt-4o")       # 用 default_provider

协议映射:
    - anthropic      : Anthropic Messages API(原生)
    - openai         : OpenAI Chat Completions(原生)
    - qwen/mimo/deepseek : openai 兼容协议(Chat Completions)
    - ollama         : 本地 Ollama(免 Key)
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """LLM 多供应商配置(Key 从 .env 读, 默认空)"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 默认模型与供应商
    default_model: str = "qwen-turbo"
    default_provider: str = "openai"

    # 通义千问(openai 兼容协议)
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 小米 MIMO(openai 兼容协议)
    mimo_api_key: str = ""
    mimo_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"

    # DeepSeek(openai 兼容协议)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # Anthropic(Claude)
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    # Ollama(本地, 无需 Key)
    ollama_base_url: str = "http://localhost:11434"

    # 模型参数
    temperature: float = 0.7
    max_tokens: int = 2000


@lru_cache()
def get_llm_config() -> LLMConfig:
    """获取 LLM 配置单例(读 .env)"""
    return LLMConfig()


@dataclass
class ProviderConfig:
    """供应商解析结果"""

    protocol: str  # "anthropic" | "openai" | "ollama"
    base_url: str
    api_key: str
    model: str
    requires_key: bool = True


# 供应商 -> (protocol, base_url 字段, api_key 字段, 是否需要 key)
_PROVIDERS: dict[str, tuple[str, str, Optional[str], bool]] = {
    "anthropic": ("anthropic", "anthropic_base_url", "anthropic_api_key", True),
    "openai": ("openai", "openai_base_url", "openai_api_key", True),
    "qwen": ("openai", "qwen_base_url", "qwen_api_key", True),
    "mimo": ("openai", "mimo_base_url", "mimo_api_key", True),
    "deepseek": ("openai", "deepseek_base_url", "deepseek_api_key", True),
    "ollama": ("ollama", "ollama_base_url", None, False),
}


def resolve_provider(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    config: Optional[LLMConfig] = None,
) -> Optional[ProviderConfig]:
    """按供应商解析 (protocol, base_url, api_key, model)。

    - provider 未给 -> 用 default_provider
    - base_url/api_key 显式传入优先(通用覆盖), 否则按供应商从 .env 取
    - 未知供应商 -> 返回 None(调用方应禁用 AI)
    """
    cfg = config or get_llm_config()
    provider = (provider or cfg.default_provider).lower().strip()
    model = (model or cfg.default_model).strip()

    entry = _PROVIDERS.get(provider)
    if entry is None:
        return None
    protocol, url_field, key_field, requires_key = entry

    resolved_key = api_key if api_key else (getattr(cfg, key_field) if key_field else "ollama")
    resolved_url = (base_url or getattr(cfg, url_field)).rstrip("/")

    return ProviderConfig(
        protocol=protocol,
        base_url=resolved_url,
        api_key=resolved_key,
        model=model,
        requires_key=requires_key,
    )


def list_providers() -> list[str]:
    """列出支持的供应商"""
    return sorted(_PROVIDERS)
