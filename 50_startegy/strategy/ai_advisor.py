"""
AI 顾问

支持两种协议(pydantic ai_provider 切换):
- anthropic : Anthropic Messages API(x-api-key + anthropic-version),
              兼容各类 Claude 协议网关(如 virex / glm)
- openai    : OpenAI Chat Completions(Bearer)

未配置(ai_enabled=false)时静默跳过。
"""

import json
from typing import Any, Optional

import aiohttp

from common.config.settings import get_settings
from common.utils.logger import LoggerMixin

ANTHROPIC_VERSION = "2023-06-01"


class AIAdvisor(LoggerMixin):
    """AI 市场顾问"""

    SYSTEM_PROMPT = """你是一名专业的加密货币量化交易顾问。
根据提供的行情数据(价格/VWAP/Delta/CVD/大单/吸筹/趋势)与持仓状态,
给出交易建议。严格按以下 JSON 格式返回:
{"advice": "BUY|SELL|HOLD|WATCH", "confidence": 0.0-1.0, "summary": "简短中文理由"}
只返回 JSON,不要其他内容。"""

    def __init__(self):
        self.settings = get_settings()
        self.provider = self.settings.ai_provider.lower()
        self.model = self.settings.ai_model
        self.base_url = self.settings.ai_base_url.rstrip("/")
        self.enabled = self.settings.ai_enabled and bool(self.settings.ai_api_key)

        if self.provider not in ("anthropic", "openai"):
            self.logger.warning("未知 AI provider,禁用", provider=self.provider)
            self.enabled = False

        self._session: Optional[aiohttp.ClientSession] = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """按协议创建会话(带认证头)"""
        if self._session is None or self._session.closed:
            if self.provider == "anthropic":
                headers = {
                    "x-api-key": self.settings.ai_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                }
            else:  # openai
                headers = {
                    "Authorization": f"Bearer {self.settings.ai_api_key}",
                    "content-type": "application/json",
                }
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- 请求构造 ----------

    def _user_content(self, symbol: str, analytics: dict[str, Any], position: Optional[dict[str, Any]]) -> str:
        return json.dumps(
            {
                "symbol": symbol,
                "market_analytics": analytics,
                "position": position or {"quantity": 0},
            },
            ensure_ascii=False,
        )

    async def analyze(
        self,
        symbol: str,
        analytics: dict[str, Any],
        position: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 AI 分析,返回 {advice, confidence, summary, raw}"""
        if not self.enabled:
            return None

        session = self._ensure_session()
        user_text = self._user_content(symbol, analytics, position)

        try:
            if self.provider == "anthropic":
                content = await self._call_anthropic(session, user_text)
            else:
                content = await self._call_openai(session, user_text)
            if content is None:
                return None
            return self._parse(content)
        except Exception as e:
            self.logger.warning("AI 请求失败", error=str(e))
            return None

    async def _call_anthropic(self, session: aiohttp.ClientSession, user_text: str) -> Optional[str]:
        """Anthropic Messages API"""
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "temperature": 0.2,
            "system": self.SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_text}],
        }
        url = f"{self.base_url}/v1/messages"
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                self.logger.warning("AI 接口错误", status=resp.status, body=text[:300])
                return None
            data = await resp.json()
            # content 为分段列表,拼接 text 段
            blocks = data.get("content", [])
            parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(p for p in parts if p) or None

    async def _call_openai(self, session: aiohttp.ClientSession, user_text: str) -> Optional[str]:
        """OpenAI Chat Completions"""
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        }
        url = f"{self.base_url}/chat/completions"
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                self.logger.warning("AI 接口错误", status=resp.status, body=text[:300])
                return None
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    # ---------- 解析 ----------

    def _parse(self, content: str) -> Optional[dict[str, Any]]:
        """解析 AI 返回的 JSON(容忍 markdown 包裹)"""
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            obj = json.loads(text.strip())
            advice = str(obj.get("advice", "WATCH")).upper()
            if advice not in ("BUY", "SELL", "HOLD", "WATCH"):
                advice = "WATCH"
            confidence = obj.get("confidence", 0.5)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.5
            return {
                "advice": advice,
                "confidence": min(1.0, max(0.0, confidence)),
                "summary": str(obj.get("summary", ""))[:500],
                "raw": content[:2000],
            }
        except json.JSONDecodeError:
            self.logger.warning("AI 返回无法解析", content=content[:200])
            return None
