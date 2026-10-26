"""链路冒烟测试：不启 HTTP，直接验证 ORM → schema → JSON 三层是否对齐。

为什么要这个：HTTP 层出问题时，你分不清是 DB 连不上、ORM 映射错、
还是 schema 序列化不符合前端契约。这个脚本把三层串起来一次跑完，
错了能立刻看到错在哪一层。

用法：
    uv run python -m scripts.smoke_test
"""

from __future__ import annotations

import asyncio
import json

from app.core.config import settings
from app.core.db import SessionLocal
from app.repositories.chat_session_repo import ChatSessionRepository
from app.services.chat_session_service import ChatSessionService


async def main() -> None:
    print(f"连库: {settings.database_url.split('@')[-1]}")
    print(f"dev_user_id: {settings.dev_user_id}\n")

    async with SessionLocal() as db:
        service = ChatSessionService(ChatSessionRepository(db))

        # 1) 会话列表
        page = await service.list_sessions(settings.dev_user_id, page_num=1, page_size=3)
        print("=== GET /api/chat/sessions ===")
        print(f"total={page.total} size={page.size} current={page.current} pages={page.pages}")
        print(f"records={len(page.records)}")
        if page.records:
            # 用 by_alias=True 模拟 FastAPI 的 response_model_by_alias
            first = json.loads(page.records[0].model_dump_json(by_alias=True))
            print("\n首条会话的真实 JSON（前端看到的就是这个）:")
            print(json.dumps(first, ensure_ascii=False, indent=2, default=str))

            # 关键断言：id 必须是字符串，否则 JS 精度丢失
            assert isinstance(first["id"], str), "id 必须是字符串！"
            assert "agentId" in first, "必须是 camelCase（agentId）"
            print("\n断言通过: id 为字符串 / 字段为 camelCase")

            # 2) 会话详情 + 消息
            sid = int(first["id"])
            detail = await service.get_session(sid, settings.dev_user_id)
            print(f"\n=== GET /api/chat/sessions/{sid} ===")
            print(f"title={detail.title!r} messageCount={detail.message_count} tokens={detail.total_tokens}")

            msgs = await service.list_messages(sid, settings.dev_user_id, page_num=1, page_size=2)
            print(f"\n=== GET /api/chat/sessions/{sid}/messages ===")
            print(f"total={msgs.total} records={len(msgs.records)}")
            if msgs.records:
                m = json.loads(msgs.records[0].model_dump_json(by_alias=True))
                print(f"首条消息: role={m['role']} id={m['id']} (type={type(m['id']).__name__})")
                print(f"字段名: {sorted(m.keys())}")
                assert isinstance(m["id"], str), "消息 id 也必须是字符串！"
                assert "metadata" in m, "必须输出 metadata 而非 msgMetadata"

    print("\n全部通过")


if __name__ == "__main__":
    asyncio.run(main())
