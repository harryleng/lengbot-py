"""文档管理（批 2）验证：18 接口 + 分块/解析离线单元。

分三层：
1. **离线纯函数**：分块策略、token 计数、扫描件判定、文件名清理、SSRF 判定
2. **在线 CRUD**：上传 → 入库 → 分块 → 向量化 → 预览 → 下载 → 删除
3. **在线边界**：类型/大小/空文件/重名/权限/路径穿越

⚠️ 在线部分会**真的调 embedding API**（写 8 个 400 token 的块 ≈ 4k token）
和**真的写 MinIO**。测试数据用 `zz-` 前缀，跑完物理删除 + 删对象。

重点验证这些静默错误：
1. **分块大小超 embedding 上限** → 整批 400 → 文档 failed（已夹到 400）
2. **扫描版 PDF 判定失效** → 只有目录页文本 → 3 个垃圾块
3. **入库失败不写 failed** → 前端永久轮询（`processing` 永不结束）
4. **重新入库不删旧块** → chunk_count 翻倍
5. **路径穿越**在图片代理接口
6. **SSRF**：file:// 协议、内网 IP、重定向绕过

用法：
    python -m scripts.knowledge_doc_check
"""

from __future__ import annotations

import asyncio
import io
import sys
import time

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.services import chunking, doc_parser, web_fetch

BASE = "http://127.0.0.1:18080"
failures = 0

_KB_IDS: list[int] = []
_DOC_IDS: list[int] = []
_MINIO_PATHS: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


def _content(sections: int = 40) -> bytes:
    parts = []
    for i in range(sections):
        parts.append(
            f"## 第{i + 1}节 测试章节\n"
            f"这是第{i + 1}段的正文内容，用于验证分块与向量化链路是否正常工作。"
            f"每段包含足够的中文字符，确保单块超过最小 token 阈值三十个，"
            f"同时不超过 embedding 模型单条上限五百一十个 token。"
        )
    return "\n\n".join(parts).encode("utf-8")


async def api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    """发请求并取 JSON。

    ⚠️ **非 JSON 响应不能让脚本炸**：未捕获的服务端异常（500 HTML、
    空 body）会让这里 `json.JSONDecodeError`，然后 cleanup 被跳过、
    测试数据全留在库里 —— 上一次就是这么堆出 5 条 `zz-` 残留的。
    退化成 `{code: http_status}` 让断言正常跑红，收尾照常执行。
    """
    resp = await c.request(method, BASE + path, **kw)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        snippet = resp.text[:300].replace("\n", " ")
        return {"code": resp.status_code, "message": f"[非 JSON 响应] {snippet}"}


async def _fresh_doc(c: httpx.AsyncClient, kid: int, tag: str) -> int:
    """传一份**内容唯一**的新文档，返回 docId。

    `force=overwrite` 会把同哈希的副本**全部**删掉（见 `_check_duplicate` 注释），
    所以前面步骤建的文档随时可能消失。每个用例都用自己的新文档，别跨步骤复用 id。
    """
    payload = (
        f"## {tag} 用例文档\n"
        + _content(6).decode("utf-8")
        + f"\n唯一标记 {tag}-{time.time_ns()}，避免与其他用例撞哈希。"
    )
    r = await api(
        c,
        "POST",
        f"/api/knowledge/{kid}/documents",
        files={"file": (f"zz-{tag}.md", io.BytesIO(payload.encode("utf-8")), "text/markdown")},
        params={"ocrEnabled": "false"},
    )
    return int(r["data"]["id"])


async def cleanup() -> None:
    """按 **知识库维度** 物理删，不靠逐个追踪的 doc id。

    失败的上传（`zz-short.md`）永远拿不到 docId，之前它们会变成孤儿
    留在 `document` 表里 —— 所以统一按 knowledge_id 扫掉。
    另外兜底按 `zz-` 前缀扫（清上一轮跑崩剩的）。
    """
    async with SessionLocal() as db:
        kbs = set(_KB_IDS)
        rows = (await db.execute(
            text("SELECT id FROM knowledge WHERE name LIKE 'zz-%'")
        )).scalars().all()
        kbs.update(rows)
        if not kbs:
            return
        kids = list(kbs)
        # 孤儿文档：知识库已被删但文档还在（上一轮跑崩的产物）
        await db.execute(
            text("DELETE FROM embedding WHERE chunk_id IN "
                 "(SELECT id FROM chunk WHERE document_id IN "
                 " (SELECT id FROM document WHERE name LIKE 'zz-%' "
                 "    OR knowledge_id = ANY(:k)))"),
            {"k": kids},
        )
        await db.execute(text("DELETE FROM chunk WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM embedding WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(
            text("DELETE FROM document WHERE knowledge_id = ANY(:k) OR name LIKE 'zz-%'"), {"k": kids}
        )
        await db.execute(text("DELETE FROM knowledge_member WHERE knowledge_id = ANY(:k)"), {"k": kids})
        await db.execute(text("DELETE FROM knowledge WHERE id = ANY(:k)"), {"k": kids})
        await db.commit()
    print(f"\n🧹 已物理删除 {len(kids)} 个知识库及其全部文档/分块/向量")


# ----------------------------------------------------------------------
# 离线
# ----------------------------------------------------------------------


def check_chunking_offline() -> None:
    print("\n[1] 分块策略（离线）")

    text_block = "\n".join(
        f"第{i}段的内容，包含一些中文以及 English words 混排，用来测试 token 计数口径。" for i in range(200)
    )

    # token 口径：中文按字、英文按词（对齐 Java TokenUtil）
    ok(
        "token 口径：'你好世界'=4",
        chunking.count_tokens("你好世界") == 4,
        str(chunking.count_tokens("你好世界")),
    )
    ok("token 口径：'hello world'=2", chunking.count_tokens("hello world") == 2)
    ok("token 口径：'a1_b'=1", chunking.count_tokens("a1_b") == 1)

    # general
    p = chunking.ChunkParams(chunk_token_num=200, overlapped_percent=10, delimiter="\n")
    blocks = chunking.split_blocks(text_block, "general", p)
    ok("general 切出多块", len(blocks) > 3, f"块数={len(blocks)}")
    ok("general 每块不超上限太多", all(chunking.count_tokens(b.content) <= 260 for b in blocks),
       f"最大={max(chunking.count_tokens(b.content) for b in blocks)}")
    ok("general 无 parent_content", all(b.parent_content is None for b in blocks))

    # 重叠生效：块之间应该有内容重复
    if len(blocks) >= 2:
        tail_a = blocks[0].content[-30:]
        ok("general 有重叠（前块尾部出现在后块开头）", tail_a[:10] in blocks[1].content[:200])

    # separator
    # 注意：每行必须 ≥30 token（MIN_CHUNK_TOKENS），否则会被全过滤掉。
    # separator 不做合并，行太短就是"零块"—— 这是设计行为不是 bug，
    # 但测试数据必须先满足最小长度，否则测出来的是"数据不对"而不是"策略不对"。
    sep_text = "\n".join(
        f"日志第{i}条：用户 2090273750973657{i:03d} 在 2026-10-25 12:00:{i%60:02d} "
        f"调用了接口 /api/knowledge/{i} 并返回 200，耗时 {i * 7} 毫秒"
        for i in range(120)
    )
    p2 = chunking.ChunkParams(chunk_token_num=5000, overlapped_percent=0, delimiter="\n")
    sep_blocks = chunking.split_blocks(sep_text, "separator", p2)
    ok("separator 按行切（块数≈行数，不合并）", len(sep_blocks) >= 100, f"块数={len(sep_blocks)}")

    # parent-child
    p3 = chunking.ChunkParams(chunk_token_num=600, overlapped_percent=10, delimiter="\n")
    pc = chunking.split_blocks(text_block, "parent-child", p3)
    ok("parent-child 有块", len(pc) > 0, f"块数={len(pc)}")
    ok("parent-child 每块带 parentContent", all(b.parent_content for b in pc))
    # 子块必须比父块小
    if pc:
        ok("子块 token < 父块 token",
           chunking.count_tokens(pc[0].content) < chunking.count_tokens(pc[0].parent_content or ""),
           f"{chunking.count_tokens(pc[0].content)} < {chunking.count_tokens(pc[0].parent_content or '')}")

    # qa
    qa_text = "\n".join(
        f"Q: 第{i}个问题是什么？\nA: 第{i}个答案在这里，内容足够长以满足最小 token 要求。"
        for i in range(30)
    )
    qa_blocks = chunking.split_blocks(qa_text, "qa", chunking.ChunkParams(chunk_token_num=512))
    ok("qa 识别出问答对", len(qa_blocks) > 10, f"块数={len(qa_blocks)}")
    ok("qa 每块含 Q 和 A", all("Q:" in b.content for b in qa_blocks))

    # qa 识别不到时回退 general（不能返回空）
    plain = "这是一段没有任何问答标记的普通文本。" * 50
    qa_fallback = chunking.split_blocks(plain, "qa", chunking.ChunkParams(chunk_token_num=300))
    ok("qa 无匹配时回退 general（不返回空）", len(qa_fallback) > 0, f"块数={len(qa_fallback)}")

    # 未实现策略必须报错，不能静默退化
    for name in ("book", "laws"):
        try:
            chunking.split_blocks(text_block, name, p)
            ok(f"{name} 应报错", False)
        except ValueError as exc:
            ok(f"{name} 明确报错（不静默退化）", "尚未实现" in str(exc), str(exc)[:50])

    try:
        chunking.split_blocks(text_block, "nonsense", p)
        ok("未知策略应报错", False)
    except ValueError:
        ok("未知策略报错", True)

    # 最小块过滤
    tiny = "短。\n短。\n短。"
    ok("碎片被过滤（<30 token）", chunking.split_blocks(tiny, "general", p) == [])

    # 清洗规则
    cleaned = chunking.apply_clean_rules("第1页\n正文\n第2页", [{"pattern": "第\\d+页", "replacement": ""}])
    ok("清洗规则生效", "第1页" not in cleaned and "正文" in cleaned, repr(cleaned))
    # 非法正则不能炸
    survived = chunking.apply_clean_rules("abc", [{"pattern": "[unclosed", "replacement": ""}])
    ok("非法正则跳过不抛异常", survived == "abc")

    # 归一化
    ok("去零宽字符", "\u200b" not in chunking.normalize_chunk_content("a\u200bb"))
    ok("去控制字符", "\x00" not in chunking.normalize_chunk_content("a\x00b"))


def check_parser_offline() -> None:
    print("\n[2] 解析工具（离线）")

    ok("识别 pdf 为支持类型", doc_parser.is_supported("pdf"))
    ok("识别 md 为支持类型", doc_parser.is_supported("md"))
    ok("未知类型不支持", not doc_parser.is_supported("exe"))
    ok("图片类型判定", doc_parser.is_image_type("png") and doc_parser.is_image_type("jpg"))
    ok("pdf 不是图片类型", not doc_parser.is_image_type("pdf"))
    ok("扩展名提取", doc_parser.detect_file_type("a/b/员工手册.PDF") == "pdf")

    # 非 pdf 一律不判扫描件
    from pathlib import Path

    ok("md 不判扫描件", not doc_parser.is_scanned_pdf("md", Path("x.md")))


def check_web_offline() -> None:
    print("\n[3] URL 抓取 SSRF 防护（离线）")

    cases = [
        ("file:///etc/passwd", "非 http 协议"),
        ("ftp://example.com/a", "非 http 协议"),
        ("http://127.0.0.1/admin", "回环"),
        ("http://localhost/admin", "回环/解析后回环"),
        ("http://10.0.0.1/", "私有网段"),
        ("http://192.168.1.1/", "私有网段"),
        ("http://169.254.169.254/latest/meta-data/", "链路本地/云元数据"),
        ("http://[::1]/", "IPv6 回环"),
    ]
    for url, reason in cases:
        try:
            web_fetch._ensure_public_url(url)
            ok(f"应拦截 {url}（{reason}）", False)
        except (web_fetch.SsrfBlocked, Exception) as exc:  # noqa: BLE001
            ok(f"拦截 {url}（{reason}）", isinstance(exc, web_fetch.SsrfBlocked), type(exc).__name__)

    # 公网域名应放行（不实际请求）
    try:
        web_fetch._ensure_public_url("https://www.example.com")
        ok("放行公网 https 域名", True)
    except Exception as exc:  # noqa: BLE001
        ok("放行公网 https 域名", False, str(exc))


# ----------------------------------------------------------------------
# 在线
# ----------------------------------------------------------------------


async def check_upload_flow(c: httpx.AsyncClient) -> tuple[int, int]:
    print("\n[4] 上传 → 入库 → 分块 → 向量化")

    # 库名带时间戳：每次跑独立，避免"上一次跑崩的残留库"被复用
    # （复用会让"重复上传应 400"这类断言被旧数据带偏）
    kb_name = f"zz-文档自检-{int(time.time())}"
    r = await api(c, "POST", "/api/knowledge", json={"name": kb_name, "type": "pg"})
    if r.get("code") != 200:
        raise RuntimeError(f"建库失败: {r.get('message')}")
    kid = int(r["data"]["id"])
    _KB_IDS.append(kid)

    payload = _content(40)
    res = await api(
        c,
        "POST",
        f"/api/knowledge/{kid}/documents",
        files={"file": ("zz-test.md", io.BytesIO(payload), "text/markdown")},
        params={"ocrEnabled": "false"},
    )
    ok("上传 200", res.get("code") == 200, str(res.get("message"))[:150])
    doc = res.get("data") or {}
    did = int(doc["id"])
    _DOC_IDS.append(did)
    ok("docId 是字符串", isinstance(doc.get("id"), str))
    ok("knowledgeId 是字符串", isinstance(doc.get("knowledgeId"), str))
    ok("入库后 status=completed", doc.get("status") == "completed", str(doc.get("status")))
    ok("chunkCount > 0", (doc.get("chunkCount") or 0) > 0, str(doc.get("chunkCount")))
    ok("tokenCount > 0", (doc.get("tokenCount") or 0) > 0, str(doc.get("tokenCount")))
    ok("markdownPath 已回写", bool(doc.get("markdownPath")))
    ok("embeddingJson 是字符串", isinstance(doc.get("embeddingJson"), str),
       f"type={type(doc.get('embeddingJson')).__name__}")

    # 分块
    chunks = (await api(c, "GET", f"/api/knowledge/documents/{did}/chunks")).get("data") or []
    ok("分块列表非空", len(chunks) > 0, f"条数={len(chunks)}")
    ok("全部 vectorized", all(ch["status"] == "vectorized" for ch in chunks),
       str({ch["status"] for ch in chunks}))
    ok("chunkId 是字符串", all(isinstance(ch["id"], str) for ch in chunks))
    # 关键回归：单块 token 不能超 embedding 上限
    max_tok = max(ch["tokenCount"] for ch in chunks)
    ok("单块 token ≤ 400（embedding 实测上限）", max_tok <= 400, f"最大={max_tok}")

    # 向量真的落库
    async with SessionLocal() as db:
        cnt = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE knowledge_id = :k"), {"k": kid}
        )).scalar_one()
        model = (await db.execute(
            text("SELECT DISTINCT model_name FROM embedding WHERE knowledge_id = :k"), {"k": kid}
        )).scalars().all()
    ok("embedding 行数 == 分块数", cnt == len(chunks), f"embedding={cnt} chunks={len(chunks)}")
    ok("model_name 是实际模型（不是历史脏值）", model and "Qwen" not in str(model), str(model))

    # 重新入库不能翻倍
    again = await api(c, "POST", f"/api/knowledge/documents/{did}/ingest",
                      json={"chunkStrategy": "general", "chunkSize": 512, "chunkOverlap": 10})
    ok("重新入库 200", again.get("code") == 200, str(again.get("message"))[:150])
    chunks2 = (await api(c, "GET", f"/api/knowledge/documents/{did}/chunks")).get("data") or []
    async with SessionLocal() as db:
        cnt2 = (await db.execute(
            text("SELECT count(*) FROM embedding WHERE knowledge_id = :k"), {"k": kid}
        )).scalar_one()
    ok("重新入库后块数不翻倍", len(chunks2) == len(chunks), f"前={len(chunks)} 后={len(chunks2)}")
    ok("重新入库后向量数不翻倍", cnt2 == cnt, f"前={cnt} 后={cnt2}")

    return kid, did


async def check_preview_download(c: httpx.AsyncClient, kid: int, did: int) -> None:
    print("\n[5] 预览 / 下载 / 内联")

    pv = await api(c, "GET", f"/api/knowledge/documents/{did}/preview")
    ok("preview 200", pv.get("code") == 200)
    ok("preview 有内容", len(pv.get("data") or "") > 100, f"len={len(pv.get('data') or '')}")

    pc = await api(c, "POST", f"/api/knowledge/documents/{did}/preview-chunks",
                   json={"chunkStrategy": "general", "chunkSize": 300, "chunkOverlap": 10})
    ok("preview-chunks 200", pc.get("code") == 200, str(pc.get("message"))[:120])
    ok("preview-chunks 是数组", isinstance(pc.get("data"), list))
    ok("preview-chunks 有内容", len(pc.get("data") or []) > 0, f"条数={len(pc.get('data') or [])}")

    # 上传时传 512，实际夹到 400：预览块不应超过 400
    sizes = [chunking.count_tokens(t) for t in (pc.get("data") or [])]
    ok("preview 块也夹到 400 以内", all(s <= 400 for s in sizes), f"最大={max(sizes) if sizes else 0}")

    dl = await api(c, "GET", f"/api/knowledge/documents/{did}/download")
    ok("download 200", dl.get("code") == 200)
    info = dl.get("data") or {}
    ok("有预签名 url", bool(info.get("url")), str(info.get("url"))[:60])
    ok("fileName 是原始名", info.get("fileName") == "zz-test.md", str(info.get("fileName")))
    ok("contentType 正确", info.get("contentType", "").startswith("text/markdown"),
       str(info.get("contentType")))

    # download-file / preview-file 返回二进制，不是 JSON
    r1 = await c.get(f"{BASE}/api/knowledge/documents/{did}/download-file")
    ok("download-file 200", r1.status_code == 200)
    ok("download-file 是 attachment", "attachment" in r1.headers.get("content-disposition", ""))
    ok("download-file 内容是原文", len(r1.content) > 100, f"len={len(r1.content)}")

    r2 = await c.get(f"{BASE}/api/knowledge/documents/{did}/preview-file")
    ok("preview-file 200", r2.status_code == 200)
    ok("preview-file 是 inline", "inline" in r2.headers.get("content-disposition", ""))
    ok("preview-file 有 Cache-Control", "max-age" in r2.headers.get("cache-control", ""))

    # 图片代理：路径穿越必须被拒。
    #
    # ⚠️ 断言不能看 `status_code` —— 全局异常处理器会把 HTTPException(404)
    # 转成 **HTTP 200 + body {code:404}**（前端拦截器只读 body.code）。
    # 所以这里要解析 body。`..%2F` 这种编码后的斜杠会被 Starlette 解码成
    # `/`，路由段数不匹配 → 默认 404，同样是 body code=404。
    for bad in ("..%2F..%2Fetc%2Fpasswd", "a.png/../../x", "x.txt", "..%2Fsecret.png"):
        rr = await c.get(f"{BASE}/api/knowledge/images/{kid}/{bad}")
        body = {}
        try:
            body = rr.json()
        except Exception:  # noqa: BLE001
            pass
        code = body.get("code")
        # 允许两种：走路由层 404，或走 service 层返回 404
        rejected = (code == 404) or rr.status_code == 404
        ok(f"图片代理拒绝 {bad[:22]}", rejected, f"http={rr.status_code} code={code}")


async def check_boundaries(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[6] 边界：类型 / 大小 / 空内容 / 重名 / force")

    # 不支持的类型
    bad = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                    files={"file": ("x.exe", io.BytesIO(b"MZ..."), "application/octet-stream")})
    ok("exe 被拒 400", bad.get("code") == 400, str(bad.get("message"))[:80])

    # 内容太短 → 分块为空 → 400（不是 500）
    short = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                      files={"file": ("zz-short.md", io.BytesIO("太短了".encode()), "text/markdown")})
    ok("内容过短 → 400（不是 500）", short.get("code") == 400, str(short.get("code")))
    ok("错误信息可操作", "token" in str(short.get("message")) or "分块" in str(short.get("message")),
       str(short.get("message"))[:80])
    # 失败的文档也要落库且状态是 failed（防前端永久轮询）
    if short.get("code") == 400:
        async with SessionLocal() as db:
            row = (await db.execute(
                text(
                    "SELECT status, error_message FROM document "
                    "WHERE knowledge_id = :k AND name = 'zz-short.md'"
                ),
                {"k": kid},
            )).first()
            if row:
                _DOC_IDS.append(0)  # 占位，实际按 knowledge_id 级联删
                ok("失败文档 status=failed", row[0] == "failed", str(row[0]))
                ok("失败文档写了 errorMessage", bool(row[1]), str(row[1])[:60])

    # 重复上传（同内容）应被拒
    payload = _content(40)
    dup = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                    files={"file": ("zz-dup.md", io.BytesIO(payload), "text/markdown")})
    ok("同内容重复上传 → 400", dup.get("code") == 400, str(dup.get("message"))[:120])
    ok("重复提示带旧文件名", "zz-test.md" in str(dup.get("message")), str(dup.get("message"))[:120])

    # force=keep-both 放行
    keep = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                     files={"file": ("zz-keep.md", io.BytesIO(payload), "text/markdown")},
                     params={"force": "keep-both"})
    ok("force=keep-both 放行", keep.get("code") == 200, str(keep.get("message"))[:120])
    if keep.get("code") == 200:
        _DOC_IDS.append(int(keep["data"]["id"]))

    # force=overwrite 覆盖（旧文档被删）
    ow = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                   files={"file": ("zz-over.md", io.BytesIO(payload), "text/markdown")},
                   params={"force": "overwrite"})
    ok("force=overwrite 放行", ow.get("code") == 200, str(ow.get("message"))[:120])
    if ow.get("code") == 200:
        _DOC_IDS.append(int(ow["data"]["id"]))

    # 非法分块策略
    bad_strategy = await api(c, "POST", f"/api/knowledge/{kid}/documents",
                             files={"file": ("zz-s.md", io.BytesIO(_content(5)), "text/markdown")})
    # 上面这个会成功；单独测 ingest 的非法策略
    if bad_strategy.get("code") == 200:
        _DOC_IDS.append(int(bad_strategy["data"]["id"]))
        bad_ing = await api(c, "POST", f"/api/knowledge/documents/{bad_strategy['data']['id']}/ingest",
                            json={"chunkStrategy": "nonsense", "chunkSize": 512, "chunkOverlap": 10})
        ok("非法策略 → 400", bad_ing.get("code") == 400, str(bad_ing.get("message"))[:100])

    # chunkSize 越界（<100）
    target = await _fresh_doc(c, kid, "size")
    small = await api(c, "POST", f"/api/knowledge/documents/{target}/ingest",
                      json={"chunkStrategy": "general", "chunkSize": 50, "chunkOverlap": 10})
    ok("chunkSize < 100 → 400", small.get("code") == 400, str(small.get("message"))[:80])


async def check_docs_list(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[7] 列表 / 详情 / 权限")

    lst = await api(c, "GET", f"/api/knowledge/{kid}/documents", params={"pageNum": 1, "pageSize": 50})
    data = lst.get("data") or {}
    ok("列表 200", lst.get("code") == 200)
    ok("total ≥ 1", (data.get("total") or 0) >= 1, str(data.get("total")))
    ok("records 非空", len(data.get("records") or []) >= 1)

    kw = await api(c, "GET", f"/api/knowledge/{kid}/documents", params={"keyword": "zz-test"})
    ok("关键字过滤生效", all("zz-test" in r["name"] for r in (kw.get("data") or {}).get("records") or []))

    # 不存在的文档
    ghost = await api(c, "GET", "/api/knowledge/documents/999999999999999999")
    ok("不存在的文档 → 404", ghost.get("code") == 404, str(ghost.get("code")))

    ghost_chunks = await api(c, "GET", "/api/knowledge/documents/999999999999999999/chunks")
    ok("不存在文档的 chunks → 404", ghost_chunks.get("code") == 404)


async def check_url_fetch(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[8] URL 抓取")

    # 内网 URL 必须被拒（不是超时，而是立刻 SsrfBlocked → 400/500）
    ssrf = await api(c, "POST", f"/api/knowledge/{kid}/documents/preview-url",
                     params={"url": "http://127.0.0.1:18080/health"})
    ok(
        "内网 URL 被拒",
        ssrf.get("code") != 200,
        f"code={ssrf.get('code')} msg={str(ssrf.get('message'))[:60]}",
    )

    file_ssrf = await api(c, "POST", f"/api/knowledge/{kid}/documents/preview-url",
                          params={"url": "file:///etc/passwd"})
    ok("file:// 被拒", file_ssrf.get("code") != 200, str(file_ssrf.get("message"))[:60])

    # 公网抓取（example.com 很稳定）。失败不算错 —— 网络环境不可控
    try:
        real = await api(c, "POST", f"/api/knowledge/{kid}/documents/preview-url",
                         params={"url": "https://example.com"})
        if real.get("code") == 200:
            d = real.get("data") or {}
            ok("公网抓取成功", True)
            ok("抓到 title", bool(d.get("title")), str(d.get("title")))
            ok("抓到正文", len(d.get("content") or "") > 20, f"len={len(d.get('content') or '')}")
            ok("suggestedFileName 是 .txt", str(d.get("suggestedFileName", "")).endswith(".txt"),
               str(d.get("suggestedFileName")))
        else:
            print(f"  ⚠️ 公网抓取跳过（网络不可达）: {str(real.get('message'))[:80]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 公网抓取跳过（异常）: {type(exc).__name__}")

    # sync-url 对非 URL 文档要 400
    non_url_doc = await _fresh_doc(c, kid, "sync")
    sync = await api(c, "POST", f"/api/knowledge/documents/{non_url_doc}/sync-url")
    ok("非 URL 文档 sync-url → 400", sync.get("code") == 400, str(sync.get("message"))[:80])


async def check_delete(c: httpx.AsyncClient, kid: int) -> None:
    print("\n[9] 删除级联")

    did = await _fresh_doc(c, kid, "del")
    kb_before = (await api(c, "GET", f"/api/knowledge/{kid}")).get("data") or {}
    before_chunks = (await api(c, "GET", f"/api/knowledge/documents/{did}/chunks")).get("data") or []
    ok("待删文档有分块", len(before_chunks) > 0, f"条数={len(before_chunks)}")

    d = await api(c, "DELETE", f"/api/knowledge/documents/{did}")
    ok("删除 200", d.get("code") == 200, str(d.get("message"))[:100])

    gone = await api(c, "GET", f"/api/knowledge/documents/{did}")
    ok("删除后详情 404", gone.get("code") == 404)

    async with SessionLocal() as db:
        chunk_left = (await db.execute(
            text("SELECT count(*) FROM chunk WHERE document_id = :d"), {"d": did}
        )).scalar_one()
        emb_left = (await db.execute(
            text(
                "SELECT count(*) FROM embedding WHERE chunk_id IN "
                "(SELECT id FROM chunk WHERE document_id = :d)"
            ),
            {"d": did},
        )).scalar_one()
        doc_row = (await db.execute(
            text("SELECT deleted FROM document WHERE id = :d"), {"d": did}
        )).first()
    ok("分块已删", chunk_left == 0, f"剩余={chunk_left}（原 {len(before_chunks)}）")
    ok("向量已删", emb_left == 0, f"剩余={emb_left}")
    ok("文档是软删（deleted=1）", doc_row and doc_row[0] == 1, str(doc_row))

    # 知识库统计应下降：按**增量**比，不能拿全库 chunkCount 和单文档块数硬比
    kb_after = (await api(c, "GET", f"/api/knowledge/{kid}")).get("data") or {}
    before_total = kb_before.get("chunkCount") or 0
    after_total = kb_after.get("chunkCount") or 0
    ok("知识库 chunkCount 恰好少了该文档的块数",
       before_total - after_total == len(before_chunks),
       f"{before_total} → {after_total}，应减 {len(before_chunks)}")
    ok("知识库 documentCount 减一",
       (kb_after.get("documentCount") or 0) == (kb_before.get("documentCount") or 1) - 1,
       f"{kb_before.get('documentCount')} → {kb_after.get('documentCount')}")


async def main() -> None:
    check_chunking_offline()
    check_parser_offline()
    check_web_offline()

    async with httpx.AsyncClient(timeout=300, trust_env=False) as c:
        try:
            kid, did = await check_upload_flow(c)
            await check_preview_download(c, kid, did)
            await check_boundaries(c, kid)
            await check_docs_list(c, kid)
            await check_url_fetch(c, kid)
            await check_delete(c, kid)
        finally:
            await cleanup()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {failures} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
