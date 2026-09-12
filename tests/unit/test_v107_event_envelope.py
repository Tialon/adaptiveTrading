"""V10.7(P0-b): Event Envelope + 事件幂等测试

验证:
- publish 自动补齐信封字段(event_id / event_time / event_version / source);
- 调用方已有字段(含 source / type)原样保留;
- event_id 幂等: 同一 event_id 重复投递(ACK 失败导致的 redelivery)只执行一次业务。
"""

import asyncio
import json

from at20_analytics.bus import EventBus, EVENT_VERSION

STREAM = "at:market:events"


class _FakeRedis:
    """最小 Redis Stream 语义桩(仅覆盖本测试需要的接口)"""

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._cursor: dict[str, int] = {}
        self._pending: dict[str, list[tuple[str, dict]]] = {}
        self._counter = 0

    async def xadd(self, stream, fields, maxlen=None, approximate=None):
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
                new = msgs[cursor: cursor + count] if count else msgs[cursor:]
                self._cursor[stream] = cursor + len(new)
                self._pending.setdefault(stream, []).extend(new)
                if new:
                    out.append([stream, new])
        return out if out else None

    async def xack(self, stream, group, msg_id):
        self._pending[stream] = [
            (m, f) for (m, f) in self._pending.get(stream, []) if m != msg_id
        ]
        return 1

    async def xlen(self, stream):
        return len(self._streams.get(stream, []))


async def _run_consumes(bus, handler, n):
    total = 0
    for _ in range(n):
        total += await bus.consume(STREAM, "g", "c", handler, count=10)
    return total


def test_envelope_fields_added():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade", "price": 100})
        _mid, fields = fake._streams[STREAM][0]
        event = json.loads(fields["data"])
        assert event["event_id"]  # 非空 UUID
        assert event["event_version"] == EVENT_VERSION
        assert event["event_time"]  # 非空
        assert event["source"] == "system"
        assert event["type"] == "trade"  # 原字段保留
        assert event["price"] == 100
    asyncio.run(go())


def test_caller_source_preserved():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        await bus.publish(STREAM, {"type": "trade", "source": "binance_ws",
                                   "correlation_id": "c1"})
        _mid, fields = fake._streams[STREAM][0]
        event = json.loads(fields["data"])
        assert event["source"] == "binance_ws"  # 调用方 source 优先
        assert event["correlation_id"] == "c1"
    asyncio.run(go())


def test_event_id_idempotent_across_deliveries():
    """同一 event_id 重复投递(ACK 失败导致的 redelivery) -> 业务 handler 只执行一次"""
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        calls = {"n": 0}

        async def handler(event):
            calls["n"] += 1

        # 显式同 event_id 的事件被投递两次(模拟 redelivery)
        await bus.publish(STREAM, {"type": "trade", "price": 100, "event_id": "e1"})
        await bus.publish(STREAM, {"type": "trade", "price": 100, "event_id": "e1"})
        n = await _run_consumes(bus, handler, 2)
        assert calls["n"] == 1  # 只执行一次业务
        assert n == 2  # 两条都 ACK
    asyncio.run(go())


def test_distinct_event_ids_both_processed():
    async def go():
        fake = _FakeRedis()
        bus = EventBus(fake)
        calls = {"n": 0}

        async def handler(event):
            calls["n"] += 1

        await bus.publish(STREAM, {"type": "trade", "price": 100})
        await bus.publish(STREAM, {"type": "trade", "price": 101})
        await _run_consumes(bus, handler, 2)
        assert calls["n"] == 2  # 不同 event_id 各自执行
    asyncio.run(go())
