"""文档解析：把各种文件转成纯文本 / markdown。

对照 Java：`TikaUtil`（Apache Tika 统一兜底）+ `DocIngestUtil`（OCR 判定）。

## Python 版为什么不用 Tika

Java 侧用 Apache Tika 一把梭（PDF/Word/Excel/PPT/HTML 全靠它）。
Python 侧没有等价的单包方案，但**每个格式都有更合适的库**，
而且都是纯 Python 或轻量 C 扩展：
- pdf  → pymupdf（`fitz`），比 Tika 的 PDFBox 快且文本层质量更好
- docx → python-docx
- xlsx → openpyxl
- pptx → python-pptx
- html → beautifulsoup4 + lxml
- 纯文本类 → 直接读

**不用 tika-python 的原因**：它要起一个 Java 子进程（`tika-server`），
在 Windows 上是另一个 JVM，启动 10s+ 还可能被防火墙拦。
为了"统一"引入一个需要 JVM 的依赖不值得。

## 扫描版 PDF 的坑（Java 侧踩过，这里必须照抄判定逻辑）

扫描版 PDF **没有文本层**，pymupdf 只能抠出目录页的几行字。
如果直接拿这点字去分块，整本书会变成 3 个 chunk —— 看起来"入库成功"，
实际检索永远查不到内容。

Java 的 `DocIngestUtil.isScannedPdf` 判定逻辑：
**PDF 且平均每页字符数 < 阈值**（默认 50）→ 认为是扫描件，转 OCR。
这个判定必须保留，否则扫描件会静默变成 3 块垃圾数据。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 支持的扩展名（对齐前端 `accept` 属性里列的那一大串）
TEXT_EXTENSIONS = {
    "md", "markdown", "txt", "text", "csv", "tsv", "json", "jsonl", "ndjson",
    "xml", "yaml", "yml", "ini", "conf", "cfg", "toml", "properties", "env",
    "editorconfig", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "py", "java",
    "go", "rs", "rb", "php", "c", "h", "cpp", "hpp", "cc", "cs", "kt", "kts",
    "scala", "swift", "sql", "sh", "bash", "zsh", "gradle",
}
HTML_EXTENSIONS = {"html", "htm"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff"}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS
    | HTML_EXTENSIONS
    | IMAGE_EXTENSIONS
    | {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt"}
)

# 扫描件判定阈值：平均每页字符数低于这个值就认为是扫描件。
# Java `DocIngestUtil` 用 50。调高会误伤（图文混排的书），
# 调低会漏判（扫描件带一点 OCR 水印文本）。
SCANNED_PDF_CHARS_PER_PAGE = 50

# 提取文本上限。一本 1000 页的书纯文本能有 10MB，
# 全塞进 DB 的 text 字段没意义 —— 分块后才是有效数据。
# 这里只限制"单次解析的返回长度"，不影响分块（分块读的是完整文本）。
MAX_TEXT_CHARS = 20_000_000


def is_image_type(file_type: str | None) -> bool:
    return (file_type or "").lower() in IMAGE_EXTENSIONS


def is_supported(file_type: str | None) -> bool:
    return (file_type or "").lower().lstrip(".") in SUPPORTED_EXTENSIONS


def _decode(raw: bytes) -> str:
    """文本解码：UTF-8 → GBK 兜底。

    为什么要 GBK 兜底：知识库里大量中文 txt/csv 是 GBK 编码，
    直接 `utf-8` 会抛 UnicodeDecodeError，用户看到"解析失败"
    但文件其实一点问题都没有。
    """
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_pdf(path: Path) -> str:
    import pymupdf  # 注意：不是 fitz，fitz 是已弃用的旧名

    parts: list[str] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)


def pdf_page_stats(path: Path) -> tuple[int, int]:
    """返回 (页数, 总字符数)。用于扫描件判定。

    单独抽出来是因为判定和解析都要开一次文件 —— 但如果只判定，
    不需要保留整本书的文本，可以边读边计数。
    """
    import pymupdf

    pages = 0
    chars = 0
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        for page in doc:
            chars += len(page.get_text())
    return pages, chars


def is_scanned_pdf(file_type: str | None, path: Path) -> bool:
    """扫描版 PDF 判定：平均每页字符数 < 阈值。

    真实例子：库里"高性价比人生指南.pdf" 2225 个 chunk 是正常书，
    而扫描版三国演义只有目录页文本 → 3 个 chunk。两者肉眼区分不了，
    只能靠字符密度判定。
    """
    if (file_type or "").lower() != "pdf":
        return False
    try:
        pages, chars = pdf_page_stats(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[文档解析] PDF 统计失败，跳过扫描件判定: %s", exc)
        return False
    if pages <= 0:
        return False
    density = chars / pages
    scanned = density < SCANNED_PDF_CHARS_PER_PAGE
    if scanned:
        logger.info(
            "[文档解析] 判定为扫描件: pages=%s chars=%s density=%.1f < %s",
            pages, chars, density, SCANNED_PDF_CHARS_PER_PAGE,
        )
    return scanned


def _parse_docx(path: Path) -> str:
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    lines = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    # 表格也要取 —— 员工手册、制度文件里大量关键信息在表格里，
    # 只读 paragraphs 会丢一半内容（Java 的 Tika 会带表格）。
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _parse_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in wb.worksheets:
            lines.append(f"## {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    lines.append(" | ".join(cells))
    finally:
        wb.close()
    return "\n".join(lines)


def _parse_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    lines: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        lines.append(f"## 第 {idx} 页")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        lines.append(text)
    return "\n".join(lines)


def _parse_html(path: Path) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_bytes(), "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _parse_legacy_office(path: Path) -> str:
    """`.doc` / `.xls` / `.ppt`（97-2003 二进制格式）。

    这些格式 python 生态没有纯 Python 解析器（都是 Java POI 的地盘）。
    如实返回空串 + 明确原因，让上层标 FAILED 并给出可操作的错误信息，
    **不要**假装解析成功返回乱码 —— 乱码分块比失败更糟，用户以为入库好了。
    """
    raise NotImplementedError(
        "Python 版不支持旧版 Office 二进制格式（.doc/.xls/.ppt），"
        "请先在本地另存为 .docx/.xlsx/.pptx 后重新上传"
    )


def extract_text(path: Path, file_type: str | None) -> str:
    """按扩展名分发解析。失败抛异常（由调用方决定标 FAILED 还是降级）。

    `file_type` 是扩展名（不带点），来自 `document.file_type`。
    """
    ext = (file_type or "").lower().lstrip(".")
    if not ext:
        ext = path.suffix.lower().lstrip(".")

    if ext == "pdf":
        text = _parse_pdf(path)
    elif ext == "docx":
        text = _parse_docx(path)
    elif ext == "xlsx":
        text = _parse_xlsx(path)
    elif ext == "pptx":
        text = _parse_pptx(path)
    elif ext in HTML_EXTENSIONS:
        text = _parse_html(path)
    elif ext in {"doc", "xls", "ppt"}:
        text = _parse_legacy_office(path)
    elif ext in TEXT_EXTENSIONS:
        text = _decode(path.read_bytes())
    elif ext in IMAGE_EXTENSIONS:
        # 图片走 OCR（调用方在 DocIngest 里决定，这里不import OCR 避免循环依赖）
        raise NotImplementedError(f"图片需要 OCR 处理: {ext}")
    else:
        raise NotImplementedError(f"不支持的文件类型: {ext}")

    if len(text) > MAX_TEXT_CHARS:
        logger.warning("[文档解析] 文本超长已截断: %s → %s", len(text), MAX_TEXT_CHARS)
        text = text[:MAX_TEXT_CHARS]
    return text


def is_zip_corrupted(path: Path) -> bool:
    """docx/xlsx/pptx 本质是 zip。校验能不能打开，提前给出"文件损坏"而不是解析堆栈。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is not None
    except (zipfile.BadZipFile, OSError):
        return True


def detect_file_type(filename: str) -> str:
    """从文件名取扩展名（不带点，小写）。"""
    return Path(filename).suffix.lower().lstrip(".")
