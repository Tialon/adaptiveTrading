"""V10.5: EventBus 死信队列(DLQ)测试

V10.6 起 ACK 语义修正: ACK 是「业务处理成功」的结果, 不是 finally 行为。
- 处理失败 -> 有限重试(携带 retry 计数重入队) -> 超 MAX_RETRY 转死信队列
- 转投也失败 -> 不 ACK, 消息留 PEL, 由 recover_pending 重放兜底
"""

import asyncio

from at20_analytics.bus import EventBus, MAX_RETRY, STREAM_DLQ_SUFFIX


class _FakeRedis:
    """最小 Redis Stream 语义桩(消费者组 ">" 新消息 + "0" pending 重放)"""

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._cursor: dict[str, int] = {}
        self._pending: dict[str, list[tuple[str, dict]]] = {}  # stream -> PEL(已投递未 ACK)
        self.acked: list[tuple[str, str]] = []
        self.fail_xadd = False  # 模拟 Redis 写失败(重试/死信转投也失败)
        self._counter = 0

    async def xadd(self, stream, fields, maxlen=None, approximate=None):
        if self.fail_xadd:
            raise RuntimeError("redis down")
        self._counter += 1
        mid = f"{self._counter}-0"
        self._streams.setdefault(stream, []).append((mid, dict(fields)))
        return mid

    async def xgroup_create(self, stream, group, id=None, mkstream=False):
        return True

    async def xreadgroup(self, group, consumer, streams, count=None, block=None):
        out = []
        for stream, last_id in streams.items():
            if last_id == ">":
                msgs = self._streams.get(stream, [])
                cursor = self._cursor.get(stream, 0)
                new = msgs[cursor : cursor + count] if count else msgs[cursor:]
                self._cursor[stream] = cursor + len(new)
                self._pending.setdefault(stream, []).extend(new)  # 投递即进入 PEL
                if new:
                    out.append([stream, new])
            elif last_id == "0":
                pending = self._pending.get(stream, [])
                if pending:
                    out.append([stream, list(pending)])
        return out if out else None

    async def xack(self, stream, group, msg_id):
        self.acked.append((stream, msg_id))
        self._pending[stream] = [
            (m, f) for (m, f) in self._pending.get(stream, []) if m != msg_id
        ]
        return 1

    async def xlen(self, stream):
        return len(self._streams.get(stream, []))


def _handler_failing(times: int):
    """返回一个前 `times` 次调用抛异常的 handler"""
    state = {"n": 0}

    async def handler(event):
        state["n"] += 1
        if state["n"] <= times:
            raise RuntimeError("boom")

    return handler, state


STREAM = "at:market:events"
DLQ = f"{STREAM}{STREAM_DLQ_SUFFIX}"


async def _run_consumes(bus, handler, n):
    total = 0
    for _ in range(n):
        total += await bus.consume(STREAM, "g", "c", handler, count=10)
    return total


def test_successful_message_no_dlq():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade", "price": 100})

        async def ok(event):
            return None

        n = await bus.consume(STREAM, "g", "c", ok)
        assert n == 1
        assert await bus.dead_letter_count(STREAM) == 0
        assert len(fake.acked) == 1

    asyncio.run(go())


def test_failed_message_retries_then_dlq():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade", "price": 100})

        async def always_fail(event):
            raise RuntimeError("boom")

        # 首次 + MAX_RETRY 次重试 = MAX_RETRY+1 次消费后进 DLQ
        n = await _run_consumes(bus, always_fail, MAX_RETRY + 1)
        assert n == MAX_RETRY + 1
        assert await bus.dead_letter_count(STREAM) == 1
        # 死信保留原始 data + error + retries
        dlq_msgs = fake._streams[DLQ]
        assert len(dlq_msgs) == 1
        fields = dlq_msgs[0][1]
        assert '"price": 100' in fields["data"]
        assert fields["error"] == "boom"
        assert fields["retries"] == str(MAX_RETRY)


def test_retry_count_increments():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade"})

        async def always_fail(event):
            raise RuntimeError("boom")

        await bus.consume(STREAM, "g", "c", always_fail)  # retry 0 -> 1
        # 重入队消息带 retry=1
        retried = fake._streams[STREAM][-1][1]
        assert retried["retry"] == "1"

    asyncio.run(go())


def test_retry_then_success_no_dlq():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade"})

        handler, state = _handler_failing(1)  # 第 1 次失败, 之后成功
        n = await _run_consumes(bus, handler, 2)
        assert n == 2
        assert state["n"] == 2
        assert await bus.dead_letter_count(STREAM) == 0

    asyncio.run(go())


def test_disabled_bus_dlq_noop():
    async def go():
        bus = EventBus(redis_client=None)
        assert await bus.dead_letter_count(STREAM) == 0

    asyncio.run(go())


def test_requeue_failure_does_not_ack():
    """handler 失败 + 转投(重试/DLQ)也失败 -> 不 ACK, 消息留 PEL(不丢)"""
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade", "price": 100})
        fake.fail_xadd = True  # Redis 写失败, 重试/死信转投也失败

        async def always_fail(event):
            raise RuntimeError("boom")

        n = await bus.consume(STREAM, "g", "c", always_fail)
        assert n == 0  # 未安全处理 -> 不计入已处理
        assert len(fake.acked) == 0  # 关键: 不 ACK(否则消息彻底消失)
        assert len(fake._pending[STREAM]) == 1  # 消息留在 PEL

    asyncio.run(go())


def test_recover_pending_replays_and_acks():
    """Redis 恢复后 recover_pending 重放 PEL, handler 成功 -> ACK"""
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade"})
        fake.fail_xadd = True

        async def always_fail(event):
            raise RuntimeError("boom")

        await bus.consume(STREAM, "g", "c", always_fail)
        assert len(fake.acked) == 0

        fake.fail_xadd = False  # Redis 恢复

        async def ok(event):
            return None

        recovered = await bus.recover_pending(STREAM, "g", "c", ok)
        assert recovered == 1
        assert len(fake.acked) == 1
        assert len(fake._pending[STREAM]) == 0

    asyncio.run(go())
