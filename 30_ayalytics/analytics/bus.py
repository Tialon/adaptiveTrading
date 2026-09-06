"""
事件总线(Redis Stream)

架构:
    Binance WS -> Market Engine -> Market Event -> Redis Stream
                                            -> Analytics / Strategy / Risk / Monitor 消费

Redis 不可用时静默降级(不影响主链路,主链路仍走内存回调)。
"""

import json
from typing import Any, Optional

from common.utils.logger import LoggerMixin

STREAM_MARKET = "at:market:events"  # 行情事件流
STREAM_SIGNAL = "at:signal:events"  # 策略信号流
MAX_STREAM_LEN = 10000  # 每条流保留上限


class EventBus(LoggerMixin):
    """Redis Stream 事件总线(发布/消费)"""

    def __init__(self, redis_client: Any = None):
        self._redis = redis_client

    @property
    def available(self) -> bool:
        return self._redis is not None

    # ---------- 发布 ----------

    async def publish(self, stream: str, event: dict[str, Any]) -> None:
        """发布事件(XADD, 自动裁剪)"""
        if not self.available:
            return
        try:
            await self._redis.xadd(
                stream, {"data": json.dumps(event, ensure_ascii=False, default=str)},
                maxlen=MAX_STREAM_LEN, approximate=True,
            )
        except Exception as e:
            self.logger.debug("事件发布失败", stream=stream, error=str(e))

    async def publish_market(self, event: dict[str, Any]) -> None:
        """行情事件"""
        await self.publish(STREAM_MARKET, event)

    async def publish_signal(self, event: dict[str, Any]) -> None:
        """策略信号事件"""
        await self.publish(STREAM_SIGNAL, event)

    # ---------- 消费 ----------

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler,
        count: int = 10,
        block_ms: int = 2000,
    ) -> int:
        """消费组模式消费(XREADGROUP),返回处理条数

        handler: async callable(event: dict) -> None
        """
        if not self.available:
            return 0
        try:
            # 确保消费组存在
            try:
                await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
            except Exception:
                pass  # BUSYGROUP: 已存在

            entries = await self._redis.xreadgroup(
                group, consumer, {stream: ">"},
                count=count, block=block_ms,
            )
            processed = 0
            for _stream, messages in entries or []:
                for msg_id, fields in messages:
                    try:
                        event = json.loads(fields.get("data", "{}"))
                        await handler(event)
                    except Exception:
                        self.logger.exception("事件处理失败", stream=stream)
                    finally:
                        await self._redis.xack(stream, group, msg_id)
                    processed += 1
            return processed
        except Exception as e:
            self.logger.debug("消费失败", stream=stream, error=str(e))
            return 0

    async def stream_length(self, stream: str) -> int:
        """流长度(监控用)"""
        if not self.available:
            return 0
        try:
            return await self._redis.xlen(stream)
        except Exception:
            return 0

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "streams": [STREAM_MARKET, STREAM_SIGNAL],
        }
