"""阶段 5 新增工具的自检：execute_code / web_search / ocr_parse_file。

用法：uv run python -m scripts.tools_ext_check

会真的执行代码、真的联网、真的跑 OCR —— 目的是验证"能跑"，不是单元测试。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.core.config import settings
from app.tools.registry import registry


def show(label: str, raw: str, limit: int = 400) -> dict:
    print(f"\n--- {label} ---")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw[:limit])
        return {}
    text = json.dumps(data, ensure_ascii=False)
    print(text[:limit] + ("…" if len(text) > limit else ""))
    return data


async def check_execute_code() -> None:
    spec = registry.get("execute_code")
    assert spec is not None

    show(
        "python: 计算 + print",
        await spec.handler(
            code="def main():\n    print('hello from python')\n    return {'sum': sum(range(10))}",
            language="python",
        ),
    )

    show(
        "javascript: 返回对象",
        await spec.handler(
            code="function main(){ var d = new Date(); return {ok: true, iso: d.toISOString()}; }",
            language="javascript",
        ),
    )

    show(
        "java: 方法体（无需 import）",
        await spec.handler(
            code="return java.time.LocalDate.now().toString();",
            language="java",
        ),
    )

    show(
        "java: 无 return（打印型）",
        await spec.handler(
            code="System.out.println(\"java prints ok\");",
            language="java",
        ),
    )

    show(
        "超时保护（sleep 10s → 5s 被杀）",
        await spec.handler(
            code="def main():\n    import time\n    time.sleep(10)\n    return 1",
            language="python",
        ),
    )

    show(
        "报错回传（不抛异常）",
        await spec.handler(code="def main():\n    return 1/0", language="python"),
    )


async def check_artifacts() -> None:
    """产物自动落到 workspace/outputs/。"""
    spec = registry.get("execute_code")
    assert spec is not None
    data = show(
        "python 生成文件 → artifacts",
        await spec.handler(
            code=(
                "def main():\n"
                "    with open('report.txt', 'w', encoding='utf-8') as f:\n"
                "        f.write('产物测试\\n')\n"
                "    return 'done'\n"
            ),
            language="python",
        ),
        600,
    )
    if data.get("artifacts"):
        path = Path(settings.workspace_root) / data["artifacts"][0]["path"]
        print(f"  落盘校验: {path} 存在={path.exists()} 内容={path.read_text(encoding='utf-8').strip()!r}")


async def check_web_search() -> None:
    spec = registry.get("web_search")
    assert spec is not None
    data = show(
        "web_search('今天北京天气')",
        await spec.handler(query="今天北京天气", maxResults=3),
        700,
    )
    results = data.get("results") or []
    print(f"  结果条数: {len(results)}")
    for r in results[:3]:
        print(f"   - {r.get('title', '')[:40]} | {r.get('url', '')[:50]}")


async def check_ocr() -> None:
    spec = registry.get("ocr_parse_file")
    assert spec is not None

    from PIL import Image, ImageDraw, ImageFont

    root = Path(settings.workspace_root)
    img_path = root / "ocr_src.png"
    # 自检脚本是同步上下文里跑的（main 直接 await 各 check_*），
    # mkdir 阻塞几毫秒无所谓；ruff 的 ASYNC240 在这里属于误报。
    root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    # 造一张带清晰中文的图。注意必须指定支持中文的字体 ——
    # PIL 默认位图字体没有汉字字形，画出来是空白，OCR 自然啥也识别不到。
    img = Image.new("RGB", (720, 160), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ):
        try:
            font = ImageFont.truetype(candidate, 40)
            break
        except OSError:
            continue
    if font is None:
        print("  没找到中文字体，测试图退化为英文")
    draw.text((30, 50), "OCR 测试：lengbot 2026", fill="black", font=font)
    img.save(img_path)
    print(f"\n测试图片已生成: {img_path}")

    data = show("ocr_parse_file('ocr_src.png')", await spec.handler(path="ocr_src.png"), 500)
    if data.get("parsed_path"):
        out = root / data["parsed_path"]
        print(f"  结果文件: {out} 存在={out.exists()}")
        if out.exists():
            print(f"  首行: {out.read_text(encoding='utf-8').splitlines()[:6]}")

    # 电子版 PDF（有文本层，应该走快路径而不是 OCR）
    try:
        import pymupdf

        pdf_path = root / "ocr_text_layer.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "This PDF has a real text layer. No OCR needed.", fontsize=14)
        doc.save(str(pdf_path))
        doc.close()
        show("ocr_parse_file('ocr_text_layer.pdf')", await spec.handler(path="ocr_text_layer.pdf"), 400)
    except Exception as exc:  # noqa: BLE001
        print(f"PDF 用例跳过: {exc}")


async def main() -> None:
    print("=" * 70)
    print("阶段 5 工具自检：3 个新工具")
    print("=" * 70)

    await check_execute_code()
    await check_artifacts()
    await check_web_search()
    await check_ocr()

    print("\n" + "=" * 70)
    print("自检结束（每段是否成功看 success 字段）")


if __name__ == "__main__":
    asyncio.run(main())
