"""
AI 顾问(V2.0 改造, V9.0 供应商化)

原则: **AI 不直接交易**。只输出:
- 市场状态判断
- 参数建议(grid_spacing / position_ratio / risk 等)
- 风险提醒

供应商(通过 ai_provider 选择, Key/base_url 从 .env 读取, 见 at50_strategy/llm_config.py):
- openai    : OpenAI Chat Completions
- qwen      : 通义千问(openai 兼容协议)
- deepseek  : DeepSeek(openai 兼容协议)

周期默认 30 分钟,输入最近行情/交易记录/策略绩效/指标。
建议写入 ai_advices 表 + 运行时参数缓存(策略下一周期读取)。
"""

import json
from typing import Any, Optional

import aiohttp

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at50_strategy.llm_config import resolve_provider


class AIAdvisor(LoggerMixin):
    """AI 参数优化顾问(仅建议,不交易)"""

    SYSTEM_PROMPT = """你是一名量化交易系统的参数优化顾问。你不做任何买卖决策。
根据提供的行情数据、交易记录、策略绩效,输出参数优化建议。
严格按以下 JSON 格式返回:
{
  "market_regime": "BULL|NORMAL|SIDEWAY|VOLATILE|BEAR|PANIC",
  "grid_spacing": "建议网格间距百分比, 如 3%",
  "position_ratio": "建议仓位比例, 如 30%",
  "risk_level": "low|medium|high",
  "warnings": ["风险提醒列表"],
  "summary": "一句话总结"
}
只返回 JSON,不要其他内容。"""

    def __init__(self):
        self.settings = get_settings()
        self.provider = self.settings.ai_provider.lower().strip()

        # 供应商解析: 通用覆盖(ai_base_url/ai_api_key)优先, 否则按供应商从 .env 取
        pc = resolve_provider(
            provider=self.provider,
            model=self.settings.ai_model,
            base_url=self.settings.ai_base_url or None,
            api_key=self.settings.ai_api_key or None,
        )
        if pc is None:
            self.logger.warning("未知 AI 供应商, 禁用", provider=self.provider)
            self.enabled = False
            self.base_url = ""
            self.api_key = ""
            self.model = self.settings.ai_model
        else:
            self.base_url = pc.base_url
            self.api_key = pc.api_key
            self.model = pc.model
            self.enabled = self.settings.ai_enabled and bool(pc.api_key)
            if self.settings.ai_enabled and not self.enabled:
                self.logger.warning(
                    "AI 已启用但缺少 API Key, 禁用", provider=self.provider, model=self.model
                )

        # 运行时参数建议缓存(策略引擎可读取)
        self.latest_advice: dict[str, Any] = {}

        self._session: Optional[aiohttp.ClientSession] = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """创建会话(OpenAI 兼容协议, Bearer 认证)"""
        if self._session is None or self._session.closed:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
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

    # ---------- 主入口 ----------

    async def advise(
        self,
        market_snapshot: dict[str, Any],
        recent_orders: Optional[list[dict[str, Any]]] = None,
        strategy_performance: Optional[list[dict[str, Any]]] = None,
        position: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """生成参数建议(不交易)"""
        if not self.enabled:
            return None

        session = self._ensure_session()
        user_text = json.dumps(
            {
                "market": market_snapshot,
                "recent_orders": (recent_orders or [])[-20:],
                "strategy_performance": strategy_performance or [],
                "position": position or {"quantity": 0},
            },
            ensure_ascii=False,
            default=str,
        )

        try:
            content = await self._call_chat(session, user_text)
            if content is None:
                return None
            advice = self._parse(content)
            if advice:
                self.latest_advice = advice
            return advice
        except Exception as e:
            self.logger.warning("AI 请求失败", error=str(e))
            return None

    # ---------- 协议调用 ----------

    async def _call_chat(self, session: aiohttp.ClientSession, user_text: str) -> Optional[str]:
        """OpenAI Chat Completions(openai/qwen/deepseek 共用)"""
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 500,
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
        """解析建议 JSON(容忍 markdown 包裹)"""
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            obj = json.loads(text.strip())
            regime = str(obj.get("market_regime", "SIDEWAY")).upper()
            if regime not in ("BULL", "NORMAL", "SIDEWAY", "VOLATILE", "BEAR", "PANIC"):
                regime = "SIDEWAY"
            warnings = obj.get("warnings", [])
            if not isinstance(warnings, list):
                warnings = [str(warnings)]
            return {
                "market_regime": regime,
                "grid_spacing": str(obj.get("grid_spacing", "")),
                "position_ratio": str(obj.get("position_ratio", "")),
                "risk_level": str(obj.get("risk_level", "medium")),
                "warnings": [str(w)[:200] for w in warnings[:5]],
                "summary": str(obj.get("summary", ""))[:500],
                "raw": content[:2000],
            }
        except json.JSONDecodeError:
            self.logger.warning("AI 返回无法解析", content=content[:200])
            return None
