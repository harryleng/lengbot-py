"""system-config 默认模型配置（11 个接口）验证。

⚠️ 这些 PUT 会改**真实配置**，所以脚本必须：先备份 → 改 → 验证 → 恢复。
   万一中途崩了，`finally` 里也会恢复。别省这一步。

重点验证：
1. `providerId` 出参是**字符串**（19 位雪花 ID，数字会被 JS 吃掉精度）
2. 4 个 default-* 各自独立
3. `default-ai` 等价于 chat（老接口兼容）
4. 配置键不存在时能 upsert（Java 那边是静默失败）

用法：python -m scripts.system_config_check
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.db import SessionLocal
from app.main import app

BASE = "http://test"
failures = 0
KEYS = [
    "default_ai_provider",
    "default_chat_model",
    "default_embedding_model",
    "default_tts_model",
    "default_rerank_model",
]


def ok(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  → {detail}" if detail else ""))


async def api(client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    resp = await client.request(method, BASE + path, **kw)
    return resp.json()


async def backup() -> dict[str, str | None]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text("SELECT config_key, config_value FROM system_config WHERE config_key = ANY(:ks)"),
                {"ks": KEYS},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def restore(saved: dict[str, str | None]) -> None:
    """把备份写回。原本不存在的键就删掉。"""
    async with SessionLocal() as db:
        for key, value in saved.items():
            if value is None:
                await db.execute(
                    text("DELETE FROM system_config WHERE config_key = :k"), {"k": key}
                )
            else:
                await db.execute(
                    text(
                        "UPDATE system_config SET config_value = :v, update_time = now() "
                        "WHERE config_key = :k"
                    ),
                    {"v": value, "k": key},
                )
        await db.commit()


async def main() -> None:
    saved = await backup()
    print(f"已备份 {len(saved)} 个配置键")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=30) as c:
            print("\n" + "=" * 70)
            print("1. default-models 合并接口")
            print("=" * 70)

            body = await api(c, "GET", "/api/system-config/default-models")
            data = body["data"]
            ok("返回 4 个键", set(data) == {"chat", "embedding", "tts", "rerank"},
               str(list(data)))
            for kind in ("chat", "embedding", "tts", "rerank"):
                cfg = data[kind] or {}
                ok(f"{kind} 有 providerId/modelId",
                   "providerId" in cfg and "modelId" in cfg, str(cfg))

            # providerId 必须是字符串或 None（不能是数字）
            bad_types = [
                (k, type(v.get("providerId")).__name__)
                for k, v in data.items()
                if v.get("providerId") is not None and not isinstance(v["providerId"], str)
            ]
            ok("providerId 是字符串（不是数字）", not bad_types, str(bad_types))

            print("\n" + "=" * 70)
            print("2. 单个 GET + default-ai 等价于 chat")
            print("=" * 70)

            chat = (await api(c, "GET", "/api/system-config/default-chat-model"))["data"]
            ai = (await api(c, "GET", "/api/system-config/default-ai"))["data"]
            ok("default-ai == default-chat-model", ai == chat, f"{ai} vs {chat}")

            for path in ("default-embedding-model", "default-tts-model", "default-rerank-model"):
                r = await api(c, "GET", f"/api/system-config/{path}")
                ok(f"GET {path} 正常", r.get("code") == 200, str(r.get("data")))

            print("\n" + "=" * 70)
            print("3. PUT 后能读回（providerId 用 19 位雪花 ID 测精度）")
            print("=" * 70)

            big_pid = "2090277031821160450"  # 真实存在的 provider，19 位
            r = await api(c, "PUT", "/api/system-config/default-chat-model",
                          json={"providerId": big_pid, "modelId": "deepseek-chat"})
            ok("PUT default-chat-model 成功", r.get("code") == 200, str(r.get("message")))

            got = (await api(c, "GET", "/api/system-config/default-chat-model"))["data"]
            ok("providerId 原样读回（精度没丢）", got["providerId"] == big_pid,
               f"{got['providerId']} vs {big_pid}")
            ok("modelId 原样读回", got["modelId"] == "deepseek-chat", str(got["modelId"]))
            ok("providerId 是字符串类型", isinstance(got["providerId"], str),
               type(got["providerId"]).__name__)

            # 落库形态检查
            async with SessionLocal() as db:
                raw = (
                    await db.execute(
                        text("SELECT config_value FROM system_config "
                             "WHERE config_key = 'default_chat_model'")
                    )
                ).scalar()
            ok("落库是 JSON 文本且含 providerId", raw is not None and big_pid in raw,
               str(raw)[:80])

            # 其它三个各自独立
            await api(c, "PUT", "/api/system-config/default-embedding-model",
                      json={"providerId": "2094705335903604737",
                            "modelId": "BAAI/bge-large-zh-v1.5"})
            merged = (await api(c, "GET", "/api/system-config/default-models"))["data"]
            ok("embedding 更新不影响 chat",
               merged["chat"]["modelId"] == "deepseek-chat", str(merged["chat"]))
            ok("embedding 已更新",
               merged["embedding"]["modelId"] == "BAAI/bge-large-zh-v1.5",
               str(merged["embedding"]))

            print("\n" + "=" * 70)
            print("4. 边界：清空配置 / 不存在的键 upsert")
            print("=" * 70)

            await api(c, "PUT", "/api/system-config/default-tts-model",
                      json={"providerId": None, "modelId": ""})
            tts = (await api(c, "GET", "/api/system-config/default-tts-model"))["data"]
            ok("空值被归一成 None", tts["providerId"] is None and tts["modelId"] is None,
               str(tts))

            # upsert：先物理删掉 tts 的键，再 PUT
            async with SessionLocal() as db:
                await db.execute(
                    text("DELETE FROM system_config WHERE config_key = 'default_tts_model'")
                )
                await db.commit()
            r = await api(c, "PUT", "/api/system-config/default-tts-model",
                          json={"providerId": "123", "modelId": "tts-x"})
            ok("键不存在时 PUT 不报错", r.get("code") == 200, str(r.get("message")))
            got2 = (await api(c, "GET", "/api/system-config/default-tts-model"))["data"]
            ok("键不存在时也能建（upsert，Java 会静默失败）",
               got2["modelId"] == "tts-x", str(got2))

    finally:
        await restore(saved)
        print("\n已恢复原配置")

    print()
    if failures:
        print(f"❌ 失败 {failures} 项")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
