"""端到端对话验证：真的问模型，看它会不会用 execute_code / web_search。

不走 HTTP，直接驱动 ChatStreamService，把 SSE 帧解出来打印。

用法：
    python -m scripts.e2e_chat_check "请用 execute_code 计算 1 到 100 的和"
    python -m scripts.e2e_chat_check "搜一下 Python 3.15 的发布时间"
    python -m scripts.e2e_chat_check "..." 2099681584963604481 2090273750973657090
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.core.config import settings
from app.schemas.chat import ChatRequestIn
from app.services.chat_stream_service import ChatStreamService

DEFAULT_AGENT = "2099681584963604481"  # 未来已来：绑了 execute_code / web_search / ocr / 文件工具


def unwrap(frame: str) -> str:
    """SSE 帧 -> payload。帧形如 'id: 3\\ndata:xxx\\n\\n'。"""
    for line in frame.splitlines():
        if line.startswith("data:"):
            return line[5:]
    return frame


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "请用 execute_code 计算 1 到 100 的和"
    agent_id = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_AGENT
    user_id = int(sys.argv[3]) if len(sys.argv) > 3 else settings.dev_user_id
    asyncio.run(run(question, agent_id, user_id))


async def run(question: str, agent_id: str, user_id: int) -> None:
    print(f"\n===== agent={agent_id} 提问: {question} =====")
    svc = ChatStreamService()
    req = ChatRequestIn(message=question, agentId=agent_id)
    text_parts: list[str] = []
    tool_calls = 0

    async for frame in svc.stream(req, user_id):
        payload = unwrap(frame)
        if payload.startswith("[STATUS]"):
            try:
                p = json.loads(payload[8:])
            except json.JSONDecodeError:
                print(f"  ?? {payload[:200]}")
                continue
            t = p.get("type")
            if t == "tool_call":
                tool_calls += 1
                print(f"  >> 调用 {p.get('toolName')}  args={str(p.get('args'))[:300]}")
            elif t == "tool_result":
                print(f"  << {p.get('toolName')} 返回: {str(p.get('result'))[:400]}")
            elif t == "tool_complete":
                print("  -- 本轮工具结束")
            elif t == "error":
                print(f"  !! 错误: {p.get('message')}")
            else:
                print(f"  .. {t}: {str(p)[:200]}")
            continue
        if payload.startswith("[REQUEST_ID]"):
            print(f"  requestId={payload[12:]}")
            continue
        if payload.startswith("[DONE]"):
            print(f"  [DONE] {payload[6:]}")
            continue
        text_parts.append(payload)

    print(f"\n===== 工具调用次数: {tool_calls} =====")
    print(f"===== 最终回答 =====\n{''.join(text_parts)}")


if __name__ == "__main__":
    main()
