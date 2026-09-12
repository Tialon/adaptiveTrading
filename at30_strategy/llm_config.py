"""
AI 模型配置中心(复用 bianAgent `src/config/llm_config.py` 的供应商选择模式)

统一管理 AI 顾问的供应商配置, 支持 openai / qwen / deepseek(均为 OpenAI 兼容协议)。
所有 API Key 从 `.env` 读取(默认空), **不硬编码在代码中**。

用法:
    from at30_strategy.llm_config import get_llm_config, resolve_provider

    cfg = get_llm_config()                       # 读取 .env
    pc = resolve_provider("qwen", "qwen-turbo")  # -> ProviderConfig(base_url/api_key/model)
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """LLM 供应商配置(Key 从 .env 读, 默认空)"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 默认模型与供应商
    default_model: str = "deepseek-chat"
    default_provider: str = "deepseek"

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # 通义千问(openai 兼容协议)
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # DeepSeek(openai 兼容协议)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # 模型参数
    temperature: float = 0.7
    max_tokens: int = 2000


@lru_cache()
def get_llm_config() -> LLMConfig:
    """获取 LLM 配置单例(读 .env)"""
    return LLMConfig()


@dataclass
class ProviderConfig:
    """供应商解析结果(均走 OpenAI Chat Completions 兼容协议)"""

    base_url: str
    api_key: str
    model: str


# 供应商 -> (base_url 字段, api_key 字段)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("openai_base_url", "openai_api_key"),
    "qwen": ("qwen_base_url", "qwen_api_key"),
    "deepseek": ("deepseek_base_url", "deepseek_api_key"),
}


def resolve_provider(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    config: Optional[LLMConfig] = None,
) -> Optional[ProviderConfig]:
    """按供应商解析 (base_url, api_key, model)。

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
    url_field, key_field = entry

    resolved_key = api_key if api_key else getattr(cfg, key_field)
    resolved_url = (base_url or getattr(cfg, url_field)).rstrip("/")

    return ProviderConfig(base_url=resolved_url, api_key=resolved_key, model=model)


def list_providers() -> list[str]:
    """列出支持的供应商"""
    return sorted(_PROVIDERS)
