"""OCR 工具 `ocr_parse_file` —— 对齐 Java 的 `OcrParseFileTool`（RapidOCR 引擎）。

结果写入工作区 `ocr/{stem}.md`，只返回路径 + 短预览（1200 字），
完整文本让模型用 `local_read_file` 再读一次 —— 这样一次工具调用不会把
几万字识别结果全灌进上下文。

返回结构照抄 Java（注意这里是 **snake_case**，和其它工具的 camelCase 不同，
Java 那边就这样，别"顺手改统一"）：

    {source_path, parsed_path, char_count, preview, truncated}

实现上的取舍：
- **PDF 先取文本层**。pymupdf 能直接抽出电子版 PDF 的文字，比 OCR 又快又准；
  只有"整页几乎没有文字"的页才渲染成图走 OCR（扫描件才需要）。
- OCR 引擎**懒加载**。RapidOCR 初始化要加载 onnxruntime + 模型，约 1~2 秒；
  没人调 OCR 的对话不该付这个代价。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from app.core.config import settings
from app.tools.base import field, tool
from app.tools.builtin_fs import _safe_path

logger = logging.getLogger(__name__)

PREVIEW_LIMIT = 1200
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
# 一页抽出来的文字少于这个字数，就认为它是扫描页，走 OCR
TEXT_LAYER_MIN_CHARS = 20
# PDF 渲染成图的分辨率：150 dpi 对正文识别够用，再高只是变慢
RENDER_DPI = 150


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


@lru_cache(maxsize=1)
def _engine():
    """RapidOCR 单例。首次调用加载模型，之后复用。"""
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _ocr_image(path: Path) -> str:
    """对单张图片做 OCR，返回按行拼接的文本。"""
    result, _elapse = _engine()(str(path))
    if not result:
        return ""
    # result 的每项是 [box, text, score]
    return "\n".join(str(item[1]) for item in result if len(item) >= 2)


def _parse_pdf(path: Path) -> str:
    import pymupdf

    parts: list[str] = []
    with pymupdf.open(str(path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            if len(text) >= TEXT_LAYER_MIN_CHARS:
                parts.append(text)
                continue

            # 扫描页：渲染成 PNG 再 OCR（临时文件放在系统临时目录）
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                pix = page.get_pixmap(dpi=RENDER_DPI)
                pix.save(str(tmp_path))
                ocr_text = _ocr_image(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)

            if ocr_text.strip():
                parts.append(f"<!-- 第 {page_no} 页（扫描件，OCR 识别） -->\n{ocr_text}")
            else:
                logger.info("第 %d 页没识别出文字", page_no)
    return "\n\n".join(parts)


async def ocr_bytes(data: bytes, file_type: str) -> str:
    """对内存里的图片/PDF 字节做 OCR。

    与 `ocr_parse_file` 的区别：那个走工作区路径沙箱（给 Agent 用），
    这个直接吃 bytes（给知识库入库用 —— 文件在 MinIO，不该先落工作区）。

    **注意 PDF 会逐页判定**：有文本层的页直接取文本（快），
    没文本层的页才渲染 + OCR（慢，约 0.5-2s/页）。一本书 500 页全是扫描件
    会跑 10 分钟以上 —— 这是 pptocr 的物理下限，不是 bug。
    """
    import asyncio
    import tempfile

    def _work() -> str:
        suffix = f".{file_type.lower().lstrip('.')}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            if suffix == ".pdf":
                return _parse_pdf(tmp_path)
            return _ocr_image(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    # OCR 是 CPU 密集（onnxruntime 推理），必须丢线程池，
    # 否则整个事件循环停摆，其他请求全部超时
    return await asyncio.to_thread(_work)


@tool(
    "ocr_parse_file",
    "将工作区中的 PDF 或图片文件 OCR 解析为文本。用于用户上传了 PDF/图片附件需要提取文字，"
    "或工作区中已有此类文件需转为可读文本。支持 pdf/jpg/jpeg/png/bmp/tiff。"
    "结果会写入工作区 ocr/ 目录，工具只返回结果文件路径和短预览，"
    "完整文本请用 local_read_file 读取结果文件。",
)
async def ocr_parse_file(
    path: Annotated[
        str, field("待解析的文件名或工作区相对路径。直接传文件名即可（如 report.pdf）")
    ],
) -> str:
    try:
        target = _safe_path(path)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc))

    if not target.is_file():
        return _err(f"文件不存在: {path}")

    ext = target.suffix.lower()
    if ext not in SUPPORTED:
        return _err(f"不支持的文件类型: {ext or '(无扩展名)'}，仅支持 pdf/jpg/jpeg/png/bmp/tiff")

    try:
        text = _parse_pdf(target) if ext == ".pdf" else _ocr_image(target)
    except ImportError as exc:
        return _err(f"OCR 依赖缺失：{exc}（需要 pymupdf / rapidocr-onnxruntime）")
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR 失败: %s", path)
        return _err(f"OCR 解析失败: {exc}")

    if not text.strip():
        return _err(
            "没有识别出任何文字。可能原因：图片过于模糊 / 分辨率过低 / PDF 是空白页。"
        )

    # 结果落盘：工作区 ocr/{stem}.md
    out_rel = f"ocr/{target.stem}.md"
    out_path = Path(settings.workspace_root) / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# {target.name}\n\n> 由 ocr_parse_file 自动识别，共 {len(text)} 字符\n\n"
    out_path.write_text(header + text, encoding="utf-8")

    truncated = len(text) > PREVIEW_LIMIT
    preview = text[:PREVIEW_LIMIT].rstrip()

    return json.dumps(
        {
            "success": True,
            "source_path": path,
            "parsed_path": out_rel,
            "char_count": len(text),
            "preview": preview,
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


OCR_TOOLS = [ocr_parse_file]
