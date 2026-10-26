"""Snowflake ID 生成器。

为什么需要：`chat_session.id` / `message.id` 是 bigint，**不是数据库自增序列**——
Java 侧由应用生成 snowflake。如果 Python 直接 INSERT 不带 id，会违反 NOT NULL。

结构（和 Java 侧同构，但 epoch 独立，因为我们只是往本地副本库写）：
    41 bit 毫秒时间戳 + 10 bit 机器号 + 12 bit 序列号

为什么不能随手用 uuid 或自增：前端和 Java 侧都假定 id 是 19 位数字字符串，
换成 uuid 会让所有 `Long.parseLong` / `int(id)` 的地方炸掉。
"""

from __future__ import annotations

import threading
import time


class Snowflake:
    EPOCH = 1704067200000  # 2024-01-01 00:00:00 UTC
    WORKER_BITS = 10
    SEQ_BITS = 12
    MAX_SEQ = (1 << SEQ_BITS) - 1

    def __init__(self, worker_id: int = 1) -> None:
        self.worker_id = worker_id & 0x3FF
        self._seq = 0
        self._last_ms = -1
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            if now == self._last_ms:
                self._seq = (self._seq + 1) & self.MAX_SEQ
                if self._seq == 0:
                    # 同一毫秒内序列号用尽，自旋等到下一毫秒
                    while now <= self._last_ms:
                        now = int(time.time() * 1000)
            else:
                self._seq = 0
            self._last_ms = now
            return (
                ((now - self.EPOCH) << (self.WORKER_BITS + self.SEQ_BITS))
                | (self.worker_id << self.SEQ_BITS)
                | self._seq
            )


snowflake = Snowflake(worker_id=1)


def new_id() -> int:
    return snowflake.next_id()
