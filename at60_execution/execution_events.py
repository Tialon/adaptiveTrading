"""
订单执行事件日志(V10.7)

append-only 审计事件写入器: 记录订单生命周期关键事件(创建/提交/成交/撤单/
恢复), 使「为什么这个订单最终变成这样」可完整追溯, 支撑崩溃排查、执行质量
统计与 AI 复盘。

只增不改: 事件无 update 路径; event_id 非空唯一, 重复写入静默忽略(幂等)。
失败降级: 落库异常仅记日志, 不阻断成交主路径(与 ExecutionAttempt 同)。
"""

import json
import time
import uuid
from typing import Any, Optional

from at01_common.logger import LoggerMixin


class ExecutionEventLogger(LoggerMixin):
    """订单执行事件日志写入器(append-only)"""

    def __init__(self):
        # 同订单内事件序号(进程内单调递增; 跨重启回退到 1, 仅作排序辅助, event_id 唯一兜底)
        self._seq: dict[str, int] = {}

    def _next_sequence(self, client_order_id: str) -> int:
        n = self._seq.get(client_order_id, 0) + 1
        self._seq[client_order_id] = n
        return n

    async def log(
        self,
        *,
        event_type: str,
        client_order_id: str = "",
        exchange_order_id: Optional[str] = None,
        order_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
        source: str = "execution",
    ) -> None:
        """落一条事件(append-only, 尽力而为, event_id 唯一兜底幂等)"""
        from sqlalchemy.exc import IntegrityError

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ExecutionEvent

        event_id = uuid.uuid4().hex
        try:
            async with AsyncSessionLocal() as session:
                session.add(ExecutionEvent(
                    event_id=event_id,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    event_type=event_type,
                    event_time=int(time.time() * 1000),
                    payload=json.dumps(payload or {}, ensure_ascii=False, default=str)[:2000],
                    source=source,
                    sequence=self._next_sequence(client_order_id),
                ))
                await session.commit()
        except IntegrityError:
            pass  # event_id 冲突(几乎不可能): 静默忽略
        except Exception:
            self.logger.exception(
                "执行事件落库失败", event_type=event_type, client_order_id=client_order_id,
            )
