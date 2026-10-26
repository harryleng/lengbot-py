"""进程内日志环形缓冲，替代 Java 的 Logback `MemoryLogAppender`。

Java 侧是自定义 Appender 订阅日志事件，SSE 再推给前端。
Python 没有 Logback，这里用标准库 `logging.Handler` 顶上：

- 容量 2000，和 Java `recent` 接口的上限一致
- 只存「发生过什么」，不落库 —— 重启即清空（Java 也是这样）
- 装一个 Handler 到 root logger，任何模块的 logger 输出都会进缓冲

⚠️ 别在里面打日志：Handler.emit 里再 log 会递归。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime

from app.schemas.misc import LogEvent

MAX_ENTRIES = 2000

_LOCK = threading.Lock()
_BUFFER: deque[LogEvent] = deque(maxlen=MAX_ENTRIES)


class MemoryLogHandler(logging.Handler):
    """把日志事件塞进环形缓冲。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = LogEvent(
                timestamp=datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                level=record.levelname,
                logger=record.name,
                thread=record.threadName,
                message=record.getMessage(),
                stacktrace=self.formatException(record.exc_info) if record.exc_info else None,
            )
        except Exception:  # noqa: BLE001  # 格式化失败绝不能影响业务线程
            return
        with _LOCK:
            _BUFFER.append(event)


def install() -> None:
    """挂到 root logger。重复调用无副作用（按 handler 类型去重）。"""
    root = logging.getLogger()
    if any(isinstance(h, MemoryLogHandler) for h in root.handlers):
        return
    handler = MemoryLogHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def recent(limit: int) -> list[LogEvent]:
    """取最近 N 条（新的在后）。Java 侧上限 2000，Python 同样夹一次。"""
    n = max(0, min(limit, MAX_ENTRIES))
    with _LOCK:
        items = list(_BUFFER)
    return items[-n:] if n else []
