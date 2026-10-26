"""OCR 相关 schema。

## 一处 Java 遗留的不一致

`OcrHealthResultDTO` 是正常 camelCase（`modelPath` / `modelExists`），
但 `ocr_parse_file` 工具的输出是 **snake_case**（`source_path` / `parsed_path`）——
`builtin_ocr.py` 里照抄了 Java，别顺手"统一"成 camelCase，
前端/工具调用方已经在按 snake_case 消费。
"""

from __future__ import annotations

from app.schemas.common import CamelModel


class OcrHealthOut(CamelModel):
    """对齐 Java `OcrHealthResultDTO`。"""

    healthy: bool = False
    model_path: str = ""
    message: str = ""
    model_exists: bool = False
