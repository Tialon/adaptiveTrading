"""
行情数据引擎

职责:
- 消费 WebSocket 数据,维护各标的核心状态(最新价/成交窗口/盘口/K线)
- 收盘 K线与逐笔成交批量持久化(MySQL/SQLite)
- 通过 Redis 发布实时行情(可选)并回调订阅者(分析引擎)
"""

import asyncio
import json
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Optional

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at10_market.data_validator import MarketDataValidator
from at10_market.market_models import KlineBar, SymbolState, TradeTick
from at10_market.market_rest_client import BinanceRestClient
from at10_market.market_ws_client import BinanceWsClient

MarketCallback = Callable[[str, TradeTick], Awaitable[None]]
KlineCallback = Callable[[KlineBar], Awaitable[None]]


def merge_klines(state: "SymbolState", bars: list[KlineBar]) -> int:
    """按 open_time 幂等合并回补 K线(跳过旧 bar / 替换最后一根 / 追加新 bar), 返回新增数"""
    added = 0
    for bar in bars:
        if not state.klines:
            state.klines.append(bar)
            added += 1
            continue
        last = state.klines[-1]
        if bar.open_time < last.open_time:
            continue
        if bar.open_time == last.open_time:
            state.klines[-1] = bar
        else:
            state.klines.append(bar)
            added += 1
    return added


def merge_trades(state: "SymbolState", ticks: list[TradeTick]) -> int:
    """按 trade_id 幂等合并回补成交(只补大于当前最大 id 的), 返回新增数"""
    added = 0
    max_id = state.trades[-1].trade_id if state.trades else 0
    for t in sorted(ticks, key=lambda x: x.trade_id):
        if t.trade_id <= max_id:
            continue
        state.trades.append(t)
        added += 1
    return added


class MarketDataEngine(LoggerMixin):
    """行情数据引擎"""

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        on_trade: Optional[MarketCallback] = None,
        on_kline: Optional[KlineCallback] = None,
    ):
        self.settings = get_settings()
        self.symbols = symbols or self.settings.symbol_list
        self.on_trade = on_trade
        self.on_kline = on_kline

        self.state: dict[str, SymbolState] = {}
        for s in self.symbols:
            st = SymbolState(symbol=s)
            st.trades = deque(maxlen=self.settings.market_trade_window)
            self.state[s] = st

        self.rest: BinanceRestClient = BinanceRestClient()
        self.ws: Optional[BinanceWsClient] = None

        self._trade_buffer: dict[str, list[TradeTick]] = defaultdict(list)
        self._kline_buffer: dict[str, list[KlineBar]] = defaultdict(list)
        self._persist_task: Optional[asyncio.Task] = None
        self._running = False

        # Redis(可选)
        self._redis: Any = None
        # V2.0: Redis Stream 事件总线
        self.bus: Any = None

        # V8: 数据校验 + 异常回调(上层接入暂停交易)
        self.validator = MarketDataValidator()
        self.on_data_anomaly: Optional[Callable[[str, list[str]], Awaitable[None]]] = None
        self._prev_closed: dict[str, KlineBar] = {}
        # V10.5: 断线回补锁(防重连风暴下的并发 REST 回补)
        self._resync_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动行情引擎"""
        self._running = True

        # 预热状态:历史 K线 + 最新成交
        await self.rest.connect()
        for symbol in self.symbols:
            try:
                await self._warmup(symbol)
            except Exception as e:
                self.logger.warning("预热失败", symbol=symbol, error=str(e))

        # Redis
        if self.settings.redis_enabled:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    self.settings.redis_url, decode_responses=True
                )
                await self._redis.ping()
                self.logger.info("Redis 已连接")
            except Exception as e:
                self.logger.warning("Redis 不可用,降级为内存模式", error=str(e))
                self._redis = None

        # WebSocket
        streams: list[str] = []
        for symbol in self.symbols:
            streams.extend(
                BinanceWsClient.streams_for_symbol(
                    symbol,
                    self.settings.market_kline_interval,
                    self.settings.market_depth_level,
                )
            )
        self.ws = BinanceWsClient(on_message=self._on_ws_message, on_reconnect=self.resync)
        await self.ws.add_streams(streams)
        await self.ws.start()

        # V2.0: 事件总线(Redis 可用时)
        if self._redis is not None:
            from at20_analytics.bus import EventBus

            self.bus = EventBus(self._redis)
            self.logger.info("事件总线已启用(Redis Stream)")

        # 批量持久化任务
        self._persist_task = asyncio.create_task(self._persist_loop(), name="market-persist")

        self.logger.info("行情引擎已启动", symbols=self.symbols)

    async def stop(self) -> None:
        """停止"""
        self._running = False
        if self._persist_task:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.stop()
        await self.rest.disconnect()
        if self._redis:
            await self._redis.aclose()
        # 落盘剩余数据
        await self._flush_buffers()
        self.logger.info("行情引擎已停止")

    # ---------- 预热 ----------

    async def _warmup(self, symbol: str) -> None:
        """拉取历史K线与近期成交,初始化状态"""
        state = self.state[symbol]

        ticker = await self.rest.get_ticker(symbol)
        state.last_price = float(ticker["lastPrice"])
        state.mark_change_pct_24h = float(ticker["priceChangePercent"])
        state.high_24h = float(ticker["highPrice"])
        state.low_24h = float(ticker["lowPrice"])
        state.quote_volume_24h = float(ticker["quoteVolume"])
        state.updated_at = int(ticker.get("closeTime", 0))

        klines = await self.rest.get_klines(
            symbol, interval=self.settings.market_kline_interval, limit=200
        )
        for k in klines:
            state.klines.append(
                KlineBar(
                    symbol=symbol,
                    interval=self.settings.market_kline_interval,
                    open_time=int(k[0]),
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    quote_volume=float(k[7]),
                    trade_count=int(k[8]),
                    closed=True,
                )
            )

        # V8: 预热完成后, 以最后一根收盘 bar 作为数据校验基线
        if state.klines:
            self._prev_closed[symbol] = state.klines[-1]

        trades = await self.rest.get_agg_trades(symbol, limit=min(200, state.trades.maxlen or 200))
        for t in trades:
            state.trades.append(
                TradeTick(
                    trade_id=int(t["a"]),
                    symbol=symbol,
                    price=float(t["p"]),
                    quantity=float(t["q"]),
                    quote_quantity=float(t["p"]) * float(t["q"]),
                    is_buyer_maker=bool(t["m"]),
                    trade_time=int(t["T"]),
                )
            )

        self.logger.info(
            "预热完成",
            symbol=symbol,
            price=state.last_price,
            klines=len(state.klines),
            trades=len(state.trades),
        )

    # ---------- 断线回补 ----------

    async def resync(self) -> None:
        """WS 断线重连后经 REST 回补缺口(K线/成交/盘口快照), 幂等合并

        作为 BinanceWsClient.on_reconnect 回调触发; 用锁防重连风暴下的并发回补。
        回补失败仅记日志, 不影响主链路(下一次重连再试)。
        """
        if self._resync_lock.locked():
            return
        async with self._resync_lock:
            for symbol in self.symbols:
                state = self.state.get(symbol)
                if state is None:
                    continue
                try:
                    klines = await self.rest.get_klines(
                        symbol, interval=self.settings.market_kline_interval, limit=200
                    )
                    bars = [
                        KlineBar(
                            symbol=symbol,
                            interval=self.settings.market_kline_interval,
                            open_time=int(k[0]),
                            open=float(k[1]),
                            high=float(k[2]),
                            low=float(k[3]),
                            close=float(k[4]),
                            volume=float(k[5]),
                            quote_volume=float(k[7]),
                            trade_count=int(k[8]),
                            closed=True,
                        )
                        for k in klines
                    ]
                    nk = merge_klines(state, bars)
                    # 刷新数据校验基线(避免回补后误报缺口)
                    if state.klines:
                        self._prev_closed[symbol] = state.klines[-1]

                    trades = await self.rest.get_agg_trades(symbol, limit=200)
                    ticks = [
                        TradeTick(
                            trade_id=int(t["a"]),
                            symbol=symbol,
                            price=float(t["p"]),
                            quantity=float(t["q"]),
                            quote_quantity=float(t["p"]) * float(t["q"]),
                            is_buyer_maker=bool(t["m"]),
                            trade_time=int(t["T"]),
                        )
                        for t in trades
                    ]
                    nt = merge_trades(state, ticks)

                    depth = await self.rest.get_depth(
                        symbol, limit=self.settings.market_depth_level
                    )
                    state.depth.bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
                    state.depth.asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]

                    self.logger.info(
                        "行情回补完成", symbol=symbol, klines=nk, trades=nt,
                    )
                except Exception as e:
                    self.logger.warning("行情回补失败", symbol=symbol, error=str(e))

    # ---------- 消息处理 ----------

    async def _on_ws_message(self, data: dict[str, Any]) -> None:
        """分发 WS 消息(按事件特征字段路由,不依赖流名)"""
        stream = data.get("stream", "")
        payload = data.get("data", data)

        if not isinstance(payload, dict):
            return

        # aggTrade 有 'a'; raw trade 有 't'。kline 有 'k'。depth 有 'b'/'a' 数组。
        if "k" in payload:
            await self._handle_kline(payload)
        elif "a" in payload and "T" in payload and "q" in payload and "b" not in payload:
            # aggTrade: a=聚合ID p=价 q=量 m=买方为挂单方 T=时间
            await self._handle_trade(payload)
        elif "t" in payload and "q" in payload and "m" in payload:
            # raw trade: t=交易ID
            await self._handle_trade(payload, trade_id_key="t")
        elif "b" in payload and "a" in payload and isinstance(payload.get("b"), list):
            await self._handle_depth(payload)
        elif "P" in payload and "c" in payload:
            await self._handle_ticker(payload)
        else:
            self.logger.debug("未识别的流消息", stream=stream, keys=list(payload.keys()))

    async def _handle_trade(self, payload: dict[str, Any], trade_id_key: str = "a") -> None:
        """逐笔成交(支持 aggTrade 与 raw trade)"""
        symbol = payload.get("s")
        state = self.state.get(symbol)
        if state is None:
            return

        price = float(payload["p"])
        quantity = float(payload["q"])
        tick = TradeTick(
            trade_id=int(payload[trade_id_key]),
            symbol=symbol,
            price=price,
            quantity=quantity,
            quote_quantity=price * quantity,
            is_buyer_maker=bool(payload["m"]),
            trade_time=int(payload["T"]),
        )
        state.trades.append(tick)
        state.last_price = tick.price
        state.updated_at = tick.trade_time

        self._trade_buffer[symbol].append(tick)

        # 发布 Redis(pub-sub) + 事件总线(Redis Stream)
        if self._redis:
            try:
                await self._redis.publish(
                    f"market:trade:{symbol}",
                    json.dumps(
                        {
                            "price": tick.price,
                            "qty": tick.quantity,
                            "is_sell": tick.is_buyer_maker,
                            "ts": tick.trade_time,
                        }
                    ),
                )
            except Exception:
                # Redis pub-sub 为可选旁路(订阅方实时推送): 失败不阻断主链路(tick 已入 state/buffer)
                pass
        if self.bus is not None:
            await self.bus.publish_market(
                {
                    "type": "trade",
                    "symbol": symbol,
                    "price": tick.price,
                    "qty": tick.quantity,
                    "quote": tick.quote_quantity,
                    "is_sell": tick.is_buyer_maker,
                    "ts": tick.trade_time,
                }
            )

        if self.on_trade:
            await self.on_trade(symbol, tick)

    async def _handle_kline(self, payload: dict[str, Any]) -> None:
        """K线"""
        k = payload.get("k", {})
        symbol = k.get("s")
        state = self.state.get(symbol)
        if state is None:
            return

        bar = KlineBar(
            symbol=symbol,
            interval=k["i"],
            open_time=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            quote_volume=float(k["q"]),
            trade_count=int(k["n"]),
            closed=bool(k["x"]),
        )
        # 更新内存(未收盘覆盖最后一根)
        if state.klines and state.klines[-1].open_time == bar.open_time:
            state.klines[-1] = bar
        else:
            state.klines.append(bar)

        if bar.closed:
            # V8: 数据校验(缺口/跳变), 异常回调上层暂停交易
            issues = self.validator.validate_closed_bar(self._prev_closed.get(symbol), bar)
            if issues:
                if self.on_data_anomaly:
                    await self.on_data_anomaly(symbol, issues)
                else:
                    self.logger.warning("行情数据异常", symbol=symbol, issues=";".join(issues))
            self._prev_closed[symbol] = bar
            self._kline_buffer[symbol].append(bar)
            if self.on_kline:
                await self.on_kline(bar)

        state.last_price = bar.close
        state.updated_at = int(payload.get("E", 0))

    async def _handle_depth(self, payload: dict[str, Any]) -> None:
        """部分深度事件(全量快照)"""
        symbol = payload.get("s")
        state = self.state.get(symbol)
        if state is None:
            return
        state.depth.bids = [[float(p), float(q)] for p, q in payload.get("b", [])]
        state.depth.asks = [[float(p), float(q)] for p, q in payload.get("a", [])]
        state.updated_at = int(payload.get("E", 0))

    async def _handle_ticker(self, payload: dict[str, Any]) -> None:
        """24h ticker"""
        symbol = payload.get("s")
        state = self.state.get(symbol)
        if state is None:
            return
        state.last_price = float(payload["c"])
        state.mark_change_pct_24h = float(payload["P"])
        state.high_24h = float(payload["h"])
        state.low_24h = float(payload["l"])
        state.quote_volume_24h = float(payload["q"])
        state.updated_at = int(payload.get("E", 0))

    # ---------- 持久化 ----------

    async def _persist_loop(self) -> None:
        """定时批量落盘"""
        while self._running:
            try:
                await asyncio.sleep(5)
                await self._flush_buffers()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("持久化异常")

    async def _flush_buffers(self) -> None:
        """批量写入数据库(幂等:按唯一键跳过已有记录)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Kline, TradeRecord

        trades = [t for ts in self._trade_buffer.values() for t in ts]
        klines = [k for ks in self._kline_buffer.values() for k in ks]
        if not trades and not klines:
            return

        try:
            async with AsyncSessionLocal() as session:
                if klines:
                    existing = {
                        (r.symbol, r.interval, r.open_time)
                        for r in (
                            await session.execute(
                                select(Kline).where(
                                    Kline.symbol.in_(list({k.symbol for k in klines}))
                                )
                            )
                        ).scalars()
                    }
                    new_rows = [
                        Kline(
                            symbol=k.symbol,
                            interval=k.interval,
                            open_time=k.open_time,
                            open=k.open,
                            high=k.high,
                            low=k.low,
                            close=k.close,
                            volume=k.volume,
                            quote_volume=k.quote_volume,
                            trade_count=k.trade_count,
                            closed=k.closed,
                        )
                        for k in klines
                        if (k.symbol, k.interval, k.open_time) not in existing
                    ]
                    session.add_all(new_rows)

                if trades:
                    existing_ids = {
                        (r.symbol, r.trade_id)
                        for r in (
                            await session.execute(
                                select(TradeRecord).where(
                                    TradeRecord.symbol.in_(list({t.symbol for t in trades}))
                                )
                            )
                        ).scalars()
                    }
                    new_rows = [
                        TradeRecord(
                            symbol=t.symbol,
                            trade_id=t.trade_id,
                            price=t.price,
                            quantity=t.quantity,
                            quote_quantity=t.quote_quantity,
                            is_buyer_maker=t.is_buyer_maker,
                            trade_time=t.trade_time,
                        )
                        for t in trades
                        if (t.symbol, t.trade_id) not in existing_ids
                    ]
                    session.add_all(new_rows)

                await session.commit()
            self._trade_buffer.clear()
            self._kline_buffer.clear()
        except Exception:
            self.logger.exception("落盘失败(重试于下轮)")

    # ---------- 对外查询 ----------

    def snapshot(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """行情快照(Web 展示用)"""
        symbols = [symbol] if symbol else self.symbols
        out: dict[str, Any] = {}
        for s in symbols:
            st = self.state.get(s)
            if st is None:
                continue
            out[s] = {
                "symbol": s,
                "last_price": st.last_price,
                "change_pct_24h": st.mark_change_pct_24h,
                "high_24h": st.high_24h,
                "low_24h": st.low_24h,
                "quote_volume_24h": st.quote_volume_24h,
                "best_bid": st.depth.best_bid,
                "best_ask": st.depth.best_ask,
                "spread": st.depth.spread,
                "mid_price": st.depth.mid_price,
                "trade_count": len(st.trades),
                "kline_count": len(st.klines),
                "updated_at": st.updated_at,
                "recent_trades": [
                    {
                        "price": t.price,
                        "qty": t.quantity,
                        "quote": t.quote_quantity,
                        "is_sell": t.is_buyer_maker,
                        "ts": t.trade_time,
                    }
                    for t in list(st.trades)[-30:]
                ],
                "recent_klines": [
                    {
                        "open_time": k.open_time,
                        "open": k.open,
                        "high": k.high,
                        "low": k.low,
                        "close": k.close,
                        "volume": k.volume,
                        "closed": k.closed,
                    }
                    for k in list(st.klines)[-60:]
                ],
            }
        return out
