"""
事件总线(Redis Stream)

架构:
    Binance WS -> Market Engine -> Market Event -> Redis Stream
                                            -> Analytics / Strategy / Risk / Monitor 消费

Redis 不可用时静默降级(不影响主链路,主链路仍走内存回调)。
"""

import json
from typing import Any, Optional

from at01_common.logger import LoggerMixin

STREAM_MARKET = "at:market:events"  # 行情事件流
STREAM_SIGNAL = "at:signal:events"  # 策略信号流
MAX_STREAM_LEN = 10000  # 每条流保留上限
STREAM_DLQ_SUFFIX = ":dlq"  # 死信队列后缀
MAX_RETRY = 3  # 处理失败最大重试次数(超限进死信队列)


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
        """消费组模式消费(XREADGROUP ">"),返回处理条数

        handler: async callable(event: dict) -> None

        ACK 语义(V10.6): ACK 是「业务处理成功」的结果, 不是 finally 行为。
        - handler 成功 → ACK
        - handler 失败但已安全转投 retry/DLQ → ACK 原消息(副本已入队)
        - handler 失败且转投也失败 → 不 ACK, 消息留在 PEL, 由 recover_pending 兜底
        """
        if not self.available:
            return 0
        try:
            await self._ensure_group(stream, group)
            entries = await self._redis.xreadgroup(
                group, consumer, {stream: ">"},
                count=count, block=block_ms,
            )
            processed = 0
            for _stream, messages in entries or []:
                for msg_id, fields in messages:
                    if await self._process_one(stream, group, msg_id, fields, handler):
                        processed += 1
            return processed
        except Exception as e:
            self.logger.debug("消费失败", stream=stream, error=str(e))
            return 0

    async def recover_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler,
        count: int = 10,
    ) -> int:
        """重放本消费者 PEL 中未被 ACK 的消息(XREADGROUP "0"), 返回处理条数

        兜底 consume() 里「转投也失败、未 ACK」的遗留消息: 逐条重放, 成功 ACK;
        仍失败按 retry/DLQ 转投后 ACK。调用方应周期触发(如消费主循环尾)。
        """
        if not self.available:
            return 0
        recovered = 0
        try:
            await self._ensure_group(stream, group)
            entries = await self._redis.xreadgroup(
                group, consumer, {stream: "0"},
                count=count, block=None,
            )
            for _stream, messages in entries or []:
                for msg_id, fields in messages:
                    if await self._process_one(stream, group, msg_id, fields, handler):
                        recovered += 1
        except Exception as e:
            self.logger.debug("pending 恢复失败", stream=stream, error=str(e))
        return recovered

    async def _ensure_group(self, stream: str, group: str) -> None:
        """确保消费组存在(BUSYGROUP 已存在则忽略)"""
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception:
            pass  # BUSYGROUP: 已存在

    async def _process_one(
        self,
        stream: str,
        group: str,
        msg_id: str,
        fields: dict[str, Any],
        handler,
    ) -> bool:
        """处理单条消息, 返回是否已安全 ACK(业务成功或已转投 retry/DLQ)"""
        try:
            event = json.loads(fields.get("data", "{}"))
            await handler(event)
            await self._redis.xack(stream, group, msg_id)
            return True
        except Exception as e:
            self.logger.exception("事件处理失败", stream=stream)
            requeued = await self._requeue_or_dlq(stream, fields, msg_id, e)
            if requeued:
                # 已安全转投(重试/死信), 原消息可 ACK
                await self._redis.xack(stream, group, msg_id)
            # requeue 也失败: 不 ACK, 消息留 PEL 由 recover_pending 兜底
            return requeued

    async def _requeue_or_dlq(
        self, stream: str, fields: dict[str, Any], msg_id: str, error: Exception
    ) -> bool:
        """处理失败: 携带 retry 计数重入队(同流重试), 超 MAX_RETRY 转死信队列

        返回 True 表示已成功转投(重试或死信), 原消息可安全 ACK; False 表示转投也失败。
        """
        try:
            data = fields.get("data", "{}")
            retry = 0
            try:
                retry = int(fields.get("retry", "0") or 0)
            except (TypeError, ValueError):
                retry = 0
            if retry < MAX_RETRY:
                await self._redis.xadd(
                    stream,
                    {"data": data, "retry": str(retry + 1), "error": str(error)},
                    maxlen=MAX_STREAM_LEN, approximate=True,
                )
            else:
                await self._redis.xadd(
                    f"{stream}{STREAM_DLQ_SUFFIX}",
                    {
                        "data": data,
                        "error": str(error),
                        "original_id": msg_id,
                        "retries": str(retry),
                    },
                )
            return True
        except Exception:
            self.logger.exception("重试/死信队列写入失败", stream=stream, msg_id=msg_id)
            return False

    async def dead_letter_count(self, stream: str) -> int:
        """死信队列长度(监控用); 不可用返回 0"""
        if not self.available:
            return 0
        try:
            return await self._redis.xlen(f"{stream}{STREAM_DLQ_SUFFIX}")
        except Exception:
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
            "dlq_suffix": STREAM_DLQ_SUFFIX,
            "max_retry": MAX_RETRY,
        }
