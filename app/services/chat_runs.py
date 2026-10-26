"""流式对话的运行注册表。

对照 Java：`ChatRunBus`（Sinks.Many 广播 + 重放缓冲）。两者做同一件事：
把"正在进行中的 run"按 requestId 存起来，好让另外两个接口能操作它 ——
`/stream/stop` 叫停它、`/stream/reconnect` 重新订阅并重放。

**边界（重要）**：这是**进程内内存**实现，只在单实例下成立。
多副本部署时必须换成 Redis（run 状态 + 帧缓冲），否则 stop 打不到另一个进程上的 run。
Java 侧用 Sinks 也是同样的单实例语义。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatRun:
    """一次流式运行的完整状态。"""

    request_id: str
    user_id: int
    # 停止信号：生产者每个 token 检查一次，置位后立即收尾
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # 结束信号：读者靠它判断"不用再等了"
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # 帧缓冲：(eventId, payload)，重连时按 eventId 重放
    frames: list[tuple[int, str]] = field(default_factory=list)
    assistant_message_id: int | None = None
    # 后台生产任务的引用，防止被 GC 回收
    task: Any = None
    created_at: float = field(default_factory=time.time)

    def append(self, payload: str) -> int:
        """写入缓冲并返回 eventId（从 1 开始递增）。"""
        event_id = len(self.frames) + 1
        self.frames.append((event_id, payload))
        return event_id


class ChatRunRegistry:
    """requestId → ChatRun。"""

    # 超过这个时间还没结束的 run 视为僵尸（客户端断开但生成器卡住）
    TTL_SECONDS = 600
    # 已结束的 run 保留一会儿，供客户端立刻重连拿到 [DONE]
    DONE_RETAIN_SECONDS = 60

    def __init__(self) -> None:
        self._runs: dict[str, ChatRun] = {}

    def create(self, request_id: str, user_id: int) -> ChatRun:
        self._gc()
        run = ChatRun(request_id=request_id, user_id=user_id)
        self._runs[request_id] = run
        return run

    def get(self, request_id: str) -> ChatRun | None:
        return self._runs.get(request_id)

    def stop(self, request_id: str, user_id: int) -> bool:
        """置位取消信号。

        返回 False 表示"没这个 run"或"不是你的 run"——
        两种情况对外都是 200，不泄露"这个 requestId 存在但属于别人"。
        """
        run = self._runs.get(request_id)
        if run is None or run.user_id != user_id:
            return False
        run.cancel.set()
        return True

    def _gc(self) -> None:
        now = time.time()
        for key, run in list(self._runs.items()):
            age = now - run.created_at
            if run.done.is_set() and age > self.DONE_RETAIN_SECONDS or age > self.TTL_SECONDS:
                self._runs.pop(key, None)


registry = ChatRunRegistry()


async def tail(run: ChatRun, last_event_id: int | None = None) -> AsyncIterator[str]:
    """从帧缓冲读数据，读空且 run 结束时退出。

    `stream`（首次连接）和 `reconnect`（重连）共用这一个函数 —— 两者的区别
    只是 `last_event_id` 传不传：传了就跳过客户端已有的帧。

    为什么用 0.05s 轮询而不是 asyncio.Queue：Queue 需要"谁来关闭"的约定，
    一个 run 可能有多个读者（原连接 + 重连），Queue 的 get() 会把帧取走而不是广播。
    读下标 + 轮询虽然笨，但对多读者是天然正确的。代价是每 50ms 醒一次，
    单机几十个并发连接完全扛得住。
    """
    idx = 0
    while True:
        while idx < len(run.frames):
            event_id, payload = run.frames[idx]
            idx += 1
            if last_event_id is not None and event_id <= last_event_id:
                continue  # 客户端已经有这一帧了
            yield f"id: {event_id}\ndata:{payload}\n\n"

        if run.done.is_set() and idx >= len(run.frames):
            return
        await asyncio.sleep(0.05)


async def replay(request_id: str, last_event_id: int | None, user_id: int) -> AsyncIterator[str]:
    """重连续流：从断点之后重放，并继续推送到 run 结束。

    run 不存在（已过期或从没存在）或不属于你 → 发 `[STATUS]{"type":"not_found"}`
    后结束。前端据此判断"这次 run 已经没了，别再等"。
    """
    run = registry.get(request_id)
    if run is None or run.user_id != user_id:
        yield 'data:[STATUS]{"type":"not_found"}\n\n'
        return
    async for frame in tail(run, last_event_id):
        yield frame
