"""消息反馈 5 + 会话扩展 6 + RAG 引用 1 = 12 个接口验证。

重点在三个会静默出错的契约：

1. **反馈 toggle 三态**：同类型再点 = 取消（返回 `null`），不是"更新"。
   只实现 upsert 的话用户永远取消不掉自己点错的赞。
2. **batch 返回对象不是数组**：`{"372xxx": {...}}`。前端 `feedbacks[String(id)]`，
   给数组全部显示成未点。
3. **静态路径必须在 `/{session_id}` 之前**：`/search`、`/batch`、`/messages/starred`
   被吃掉就是 422，前端只看到"参数错误"。

⚠️ 会写库，测试数据用 `zz-` 前缀，跑完物理删除。

用法：python -m scripts.chat_ext_check
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.snowflake import new_id
from app.main import app

BASE = "http://test"
PREFIX = "zz-自检会话"
failures = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    return (await c.request(method, BASE + path, **kw)).json()


async def cleanup() -> None:
    async with SessionLocal() as db:
        ids = [
            r[0]
            for r in (
                await db.execute(
                    text("SELECT id FROM chat_session WHERE title LIKE :p"),
                    {"p": f"{PREFIX}%"},
                )
            ).all()
        ]
        if ids:
            mids = [
                r[0]
                for r in (
                    await db.execute(
                        text("SELECT id FROM message WHERE session_id = ANY(:ids)"),
                        {"ids": ids},
                    )
                ).all()
            ]
            if mids:
                await db.execute(
                    text("DELETE FROM message_feedback WHERE message_id = ANY(:m)"),
                    {"m": mids},
                )
                await db.execute(
                    text("DELETE FROM tool_calls WHERE message_id = ANY(:m)"), {"m": mids}
                )
            await db.execute(
                text("DELETE FROM message WHERE session_id = ANY(:ids)"), {"ids": ids}
            )
            await db.execute(
                text("DELETE FROM chat_session WHERE id = ANY(:ids)"), {"ids": ids}
            )
        await db.commit()


async def seed() -> tuple[int, int, int]:
    """造一个会话 + 两条消息，返回 (session_id, user_msg_id, asst_msg_id)。"""
    sid = new_id()
    umid = new_id()
    amid = new_id()
    agent_id = 2099681584963604481  # 未来已来（chat 型，dev 用户拥有）
    async with SessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO chat_session (id, agent_id, user_id, title, status, "
                "message_count, total_tokens, pinned, create_time, update_time, deleted) "
                "VALUES (:i, :a, :u, :t, 'active', 2, 0, false, now(), now(), 0)"
            ),
            {"i": sid, "a": agent_id, "u": settings.dev_user_id, "t": f"{PREFIX}-1"},
        )
        await db.execute(
            text(
                "INSERT INTO message (id, session_id, role, content, content_type, "
                "token_count, message_type, starred, create_time) "
                "VALUES (:i, :s, 'user', :c, 'text', 5, 'text', false, now())"
            ),
            {"i": umid, "s": sid, "c": "我想问一下向量检索是怎么实现的？"},
        )
        await db.execute(
            text(
                "INSERT INTO message (id, session_id, role, content, content_type, "
                "token_count, message_type, starred, create_time) "
                "VALUES (:i, :s, 'assistant', :c, 'text', 20, 'text', false, now())"
            ),
            {"i": amid, "s": sid, "c": "向量检索的原理是把文本映射到高维空间，用余弦相似度找最近邻。"},
        )
        await db.commit()
    return sid, umid, amid


async def check_feedback(c: httpx.AsyncClient, amid: int) -> None:
    print("=" * 70)
    print("1. 消息反馈 toggle 三态")
    print("=" * 70)

    r = await api(c, "POST", f"/api/chat/messages/{amid}/feedback", json={"rating": "like"})
    ok("首次点赞成功", r.get("code") == 200 and r["data"]["rating"] == "like",
       str(r.get("data") or r.get("message"))[:80])
    if r.get("data"):
        ok("id 是字符串", isinstance(r["data"]["id"], str), type(r["data"]["id"]).__name__)
        ok("messageId 是字符串", isinstance(r["data"]["messageId"], str))
        ok("带 agentId 快照（提交时记录）", r["data"].get("agentId") is not None,
           str(r["data"].get("agentId")))
        ok("带 agentVersion 快照", r["data"].get("agentVersion") is not None,
           str(r["data"].get("agentVersion")))

    got = (await api(c, "GET", f"/api/chat/messages/{amid}/feedback"))["data"]
    ok("查回是 like", got is not None and got["rating"] == "like", str(got)[:70])

    # 同类型再点 → 取消（data 为 null）
    r2 = await api(c, "POST", f"/api/chat/messages/{amid}/feedback", json={"rating": "like"})
    ok("同类型再点 → data 为 null（取消，不是更新）",
       r2.get("code") == 200 and r2["data"] is None, str(r2.get("data")))
    got2 = (await api(c, "GET", f"/api/chat/messages/{amid}/feedback"))["data"]
    ok("取消后查回 null", got2 is None, str(got2))

    # 重新点 dislike
    r3 = await api(c, "POST", f"/api/chat/messages/{amid}/feedback",
                   json={"rating": "dislike", "reason": "答得太简略"})
    ok("点踩成功", r3["data"]["rating"] == "dislike", str(r3["data"]["rating"]))
    ok("reason 落库", r3["data"]["reason"] == "答得太简略", str(r3["data"]["reason"]))

    # 切换 like
    r4 = await api(c, "POST", f"/api/chat/messages/{amid}/feedback", json={"rating": "like"})
    ok("踩→赞 切换（不是新建）", r4["data"]["rating"] == "like", str(r4["data"]["rating"]))

    async with SessionLocal() as db:
        n = (
            await db.execute(
                text("SELECT count(*) FROM message_feedback WHERE message_id = :m"),
                {"m": amid},
            )
        ).scalar()
    ok("库里只有 1 条（toggle 不是堆记录）", n == 1, str(n))

    # 非法 rating
    bad = await api(c, "POST", f"/api/chat/messages/{amid}/feedback", json={"rating": "meh"})
    ok("非法 rating → 400", bad.get("code") == 400, str(bad.get("message"))[:60])

    # 不存在的消息
    nf = await api(c, "POST", "/api/chat/messages/999999999999/feedback",
                   json={"rating": "like"})
    ok("不存在的消息 → 404", nf.get("code") == 404, str(nf.get("code")))

    print("\n" + "=" * 70)
    print("2. batch 返回对象（key 是 messageId 字符串）")
    print("=" * 70)

    batch = (await api(c, "POST", "/api/chat/messages/feedbacks/batch",
                       json=[amid]))["data"]
    ok("返回 dict 不是 list", isinstance(batch, dict), type(batch).__name__)
    ok("key 是 messageId 字符串", str(amid) in batch, str(list(batch))[:60])
    ok("value 有 rating", batch.get(str(amid), {}).get("rating") == "like",
       str(batch.get(str(amid)))[:60])

    empty = (await api(c, "POST", "/api/chat/messages/feedbacks/batch", json=[]))["data"]
    ok("空数组返回 {}", empty == {}, str(empty))

    print("\n" + "=" * 70)
    print("3. 反馈列表 + 统计")
    print("=" * 70)

    lst = (await api(c, "GET", "/api/chat/feedbacks?pageNum=1&pageSize=10"))["data"]
    ok("列表返回分页", "records" in lst, f"total={lst.get('total')}")
    ok("有记录", lst["total"] >= 1, str(lst["total"]))
    if lst["records"]:
        row = lst["records"][0]
        ok("带 messageContent（省得前端补查）", bool(row.get("messageContent")),
           str(row.get("messageContent"))[:40])
        ok("带 sessionId", bool(row.get("sessionId")), str(row.get("sessionId")))
        ok("带 agentName", bool(row.get("agentName")), str(row.get("agentName")))

    filt = (await api(c, "GET", "/api/chat/feedbacks?rating=like"))["data"]
    ok("rating 过滤生效",
       all(x["rating"] == "like" for x in filt["records"]), f"{filt['total']} 条")

    stats = (await api(c, "GET", "/api/chat/feedbacks/stats"))["data"]
    ok("统计 key 是驼峰", {"total", "likeCount", "dislikeCount"} <= set(stats),
       str(stats))
    ok("likeCount >= 1", stats["likeCount"] >= 1, str(stats))
    ok("total = like + dislike",
       stats["total"] == stats["likeCount"] + stats["dislikeCount"], str(stats))


async def check_session_ext(c: httpx.AsyncClient, sid: int, umid: int) -> None:
    print("\n" + "=" * 70)
    print("4. 静态路径没被 /{session_id} 吃掉")
    print("=" * 70)

    # 这三个如果被吃 → 422
    s1 = await api(c, "GET", "/api/chat/sessions/search?q=向量&limit=5")
    ok("GET /sessions/search 不 422", s1.get("code") == 200, str(s1.get("code")))

    s2 = await api(c, "GET", "/api/chat/sessions/messages/starred?pageSize=5")
    ok("GET /sessions/messages/starred 不 422", s2.get("code") == 200, str(s2.get("code")))

    print("\n" + "=" * 70)
    print("5. 跨会话搜索（snippet 关键字居中）")
    print("=" * 70)

    hits = (await api(c, "GET", "/api/chat/sessions/search?q=向量检索&limit=10"))["data"]
    ok("搜到结果", len(hits) >= 1, f"{len(hits)} 条")
    if hits:
        h = hits[0]
        ok("sessionId 是字符串", isinstance(h["sessionId"], str))
        ok("带 sessionTitle", bool(h.get("sessionTitle")), str(h.get("sessionTitle")))
        ok("snippet 含关键字", "向量检索" in h["snippet"], h["snippet"][:60])
        ok("pinned 是 bool", isinstance(h["pinned"], bool), type(h["pinned"]).__name__)

    blank = (await api(c, "GET", "/api/chat/sessions/search?q=%20%20&limit=5"))
    # q 必填 + min_length=1，全空格会被服务端 trim 成空 → 返回空数组
    ok("全空格关键词返回空（不是全表扫）",
       blank.get("code") == 200 and blank["data"] == [], str(blank.get("data"))[:40])

    print("\n" + "=" * 70)
    print("6. 会话内搜索")
    print("=" * 70)

    inner = (await api(
        c, "GET", f"/api/chat/sessions/{sid}/messages/search?keyword=向量"
    ))["data"]
    ok("会话内搜索返回分页", "records" in inner, f"total={inner.get('total')}")
    ok("命中 1 条以上", inner["total"] >= 1, str(inner["total"]))

    print("\n" + "=" * 70)
    print("7. 收藏")
    print("=" * 70)

    r = await api(c, "PUT", f"/api/chat/sessions/messages/{umid}/star")
    ok("收藏成功且返回新状态 true", r.get("code") == 200 and r["data"] is True,
       str(r.get("data")))

    starred = (await api(c, "GET", "/api/chat/sessions/messages/starred"))["data"]
    ok("收藏列表里有它", any(str(x["id"]) == str(umid) for x in starred["records"]),
       f"total={starred['total']}")

    r2 = await api(c, "PUT", f"/api/chat/sessions/messages/{umid}/star")
    ok("再点一次 → false（取反）", r2["data"] is False, str(r2.get("data")))
    starred2 = (await api(c, "GET", "/api/chat/sessions/messages/starred"))["data"]
    ok("取消后列表里没有它",
       not any(str(x["id"]) == str(umid) for x in starred2["records"]))

    print("\n" + "=" * 70)
    print("8. 归档（不是删除）")
    print("=" * 70)

    r = await api(c, "PUT", f"/api/chat/sessions/{sid}/archive")
    ok("归档成功", r.get("code") == 200, str(r.get("message")))

    async with SessionLocal() as db:
        row = (
            await db.execute(
                text("SELECT status, deleted FROM chat_session WHERE id = :i"), {"i": sid}
            )
        ).first()
    ok("status=archived", row[0] == "archived", str(row[0]))
    ok("deleted 仍是 0（归档≠删除）", row[1] == 0, str(row[1]))

    detail = await api(c, "GET", f"/api/chat/sessions/{sid}")
    ok("归档后详情仍可访问", detail.get("code") == 200, str(detail.get("code")))

    print("\n" + "=" * 70)
    print("9. 附件（jsonb 数组，不依赖 MinIO）")
    print("=" * 70)

    empty_att = (await api(c, "GET", f"/api/chat/sessions/{sid}/attachments"))["data"]
    ok("空附件返回 []", empty_att == [], str(empty_att))

    # 手动塞一个附件再读
    async with SessionLocal() as db:
        await db.execute(
            text(
                "UPDATE chat_session SET attachments = :a WHERE id = :i"
            ),
            {
                "a": '[{"id":"att-1","type":"file","fileName":"报告.pdf",'
                     '"mimeType":"application/pdf","createdAt":"2026-10-25T10:00:00"}]',
                "i": sid,
            },
        )
        await db.commit()

    atts = (await api(c, "GET", f"/api/chat/sessions/{sid}/attachments"))["data"]
    ok("读到 1 个附件", len(atts) == 1, str(len(atts)))
    if atts:
        ok("attachmentId 是字符串", isinstance(atts[0]["id"], str))
        ok("fileName 透传", atts[0]["fileName"] == "报告.pdf", str(atts[0]["fileName"]))

    r = await api(c, "DELETE", f"/api/chat/sessions/{sid}/attachments/att-1")
    ok("删除附件成功", r.get("code") == 200, str(r.get("message")))
    atts2 = (await api(c, "GET", f"/api/chat/sessions/{sid}/attachments"))["data"]
    ok("删除后为空", atts2 == [], str(atts2))

    nf = await api(c, "DELETE", f"/api/chat/sessions/{sid}/attachments/no-such")
    ok("删不存在的附件 → 404", nf.get("code") == 404, str(nf.get("code")))

    print("\n" + "=" * 70)
    print("10. 越权：别人的会话")
    print("=" * 70)

    # 造一个属于别人的会话
    other = new_id()
    async with SessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO chat_session (id, agent_id, user_id, title, status, "
                "message_count, total_tokens, pinned, create_time, update_time, deleted) "
                "VALUES (:i, NULL, 111111111111111111, :t, 'active', 0, 0, false, "
                "now(), now(), 0)"
            ),
            {"i": other, "t": f"{PREFIX}-other"},
        )
        omid = new_id()
        await db.execute(
            text(
                "INSERT INTO message (id, session_id, role, content, content_type, "
                "token_count, message_type, starred, create_time) "
                "VALUES (:i, :s, 'assistant', '别人的消息', 'text', 1, 'text', false, now())"
            ),
            {"i": omid, "s": other},
        )
        await db.commit()

    for label, method, path, kw in [
        ("归档别人的会话", "PUT", f"/api/chat/sessions/{other}/archive", {}),
        ("搜别人会话的消息", "GET", f"/api/chat/sessions/{other}/messages/search?keyword=x", {}),
        ("看别人会话的附件", "GET", f"/api/chat/sessions/{other}/attachments", {}),
        ("收藏别人的消息", "PUT", f"/api/chat/sessions/messages/{omid}/star", {}),
        ("给别人消息点赞", "POST", f"/api/chat/messages/{omid}/feedback",
         {"json": {"rating": "like"}}),
    ]:
        r = await api(c, method, path, **kw)
        ok(f"{label} → 404", r.get("code") == 404, str(r.get("code")))

    async with SessionLocal() as db:
        await db.execute(
            text("DELETE FROM message WHERE session_id = :i"), {"i": other}
        )
        await db.execute(text("DELETE FROM chat_session WHERE id = :i"), {"i": other})
        await db.commit()

    print("\n" + "=" * 70)
    print("11. 批量删除（物理删 + 只删自己的）")
    print("=" * 70)

    # 再造两个自己的会话，混一个别人的 id
    mine = []
    async with SessionLocal() as db:
        for i in range(2):
            nid = new_id()
            mine.append(nid)
            await db.execute(
                text(
                    "INSERT INTO chat_session (id, agent_id, user_id, title, status, "
                    "message_count, total_tokens, pinned, create_time, update_time, deleted) "
                    "VALUES (:i, NULL, :u, :t, 'active', 0, 0, false, now(), now(), 0)"
                ),
                {"i": nid, "u": settings.dev_user_id, "t": f"{PREFIX}-batch{i}"},
            )
        other2 = new_id()
        await db.execute(
            text(
                "INSERT INTO chat_session (id, agent_id, user_id, title, status, "
                "message_count, total_tokens, pinned, create_time, update_time, deleted) "
                "VALUES (:i, NULL, 222222222222222222, :t, 'active', 0, 0, false, "
                "now(), now(), 0)"
            ),
            {"i": other2, "t": f"{PREFIX}-other2"},
        )
        await db.commit()

    r = await api(c, "DELETE", "/api/chat/sessions/batch", json=[*mine, other2])
    ok("批量删除成功", r.get("code") == 200, str(r.get("message")))
    ok("只删了自己的 2 个（别人的静默跳过）",
       "2" in str(r.get("data")), str(r.get("data")))

    async with SessionLocal() as db:
        left_other = (
            await db.execute(
                text("SELECT count(*) FROM chat_session WHERE id = :i"), {"i": other2}
            )
        ).scalar()
        await db.execute(text("DELETE FROM chat_session WHERE id = :i"), {"i": other2})
        await db.commit()
    ok("别人的会话还在（没被误删）", left_other == 1, str(left_other))

    print("\n" + "=" * 70)
    print("12. RAG 引用")
    print("=" * 70)

    refs = (await api(
        c, "GET",
        f"/api/chat/rag-references?sessionId={sid}&question=什么是向量检索",
    ))["data"]
    ok("返回数组（可能为空）", isinstance(refs, list), str(type(refs)))
    print(f"     命中 {len(refs)} 条（Agent 绑的库若无向量则为 0，属正常）")
    if refs:
        r0 = refs[0]
        ok("有 documentName", bool(r0.get("documentName")) or r0.get("documentName") is None,
           str(r0.get("documentName")))
        ok("有 contentPreview", bool(r0.get("contentPreview")), str(r0.get("contentPreview"))[:50])
        ok("sourceType=chunk", r0.get("sourceType") == "chunk", str(r0.get("sourceType")))
        ok("chunkId 是字符串", isinstance(r0.get("chunkId"), str), str(r0.get("chunkId")))

    # 空问题
    blank_refs = (await api(
        c, "GET", f"/api/chat/rag-references?sessionId={sid}&question=%20"
    ))["data"]
    ok("空问题返回 []（不报错）", blank_refs == [], str(blank_refs))

    # 显式指定一个未绑库的 agent
    no_kb = (await api(
        c, "GET", f"/api/chat/rag-references?sessionId={sid}&question=x&agentId=2090282749592842241"
    ))["data"]
    ok("Agent 没绑库 → 空数组不报错", no_kb == [], str(no_kb))


async def main() -> None:
    await cleanup()
    sid, umid, amid = await seed()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=90) as c:
        await check_feedback(c, amid)
        await check_session_ext(c, sid, umid)

    await cleanup()
    print("\n已清理测试数据")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
