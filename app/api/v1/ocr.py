"""OCR 健康检查：`GET /ocr/health`。

对照 Java `OcrController` + `OcrUtil.healthCheck()`。

出参字段：`healthy` / `modelPath` / `message` / `modelExists`。

⚠️ **这个接口必须永远是 200**。
它是给"配置页显示 OCR 状态"用的，Java 的 `healthCheck()` 内部 try/catch 兜住了
引擎初始化异常，返回 `healthy=false` 而不是抛错。如果改成 500，
前端拿不到 body，状态徽标会直接不显示 —— 用户不知道是"没装 OCR"
还是"服务挂了"。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.schemas.common import Result
from app.schemas.ocr import OcrHealthOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ocr", tags=["ocr"])


@router.get("/health", response_model=Result[OcrHealthOut], summary="OCR 健康检查")
async def ocr_health() -> Result[OcrHealthOut]:
    """不主动初始化引擎：RapidOCR 加载 onnxruntime + 模型要 1~2 秒，
    轮询这个接口的页面不该每次都付这个代价。只检查**能不能加载**。
    """
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
    except ImportError as exc:
        logger.info("[OCR] 引擎不可用：%s", exc)
        return Result.ok(
            OcrHealthOut(
                healthy=False,
                model_path="",
                model_exists=False,
                message="OCR 引擎未安装：rapidocr-onnxruntime 不在依赖里",
            )
        )
    except Exception as exc:  # noqa: BLE001
        return Result.ok(
            OcrHealthOut(
                healthy=False,
                model_path="",
                model_exists=False,
                message=f"OCR 引擎导入失败：{exc}",
            )
        )

    model_path = ""
    try:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
        # RapidOCR 内部版本号/模型目录：拿得到就展示，拿不到不影响 healthy
        for attr in ("params", "SESSION_OPTIONS", "config"):
            if hasattr(engine, attr):
                model_path = f"{RapidOCR.__module__} ({attr} 已加载)"
                break
        return Result.ok(
            OcrHealthOut(
                healthy=True,
                model_path=model_path or RapidOCR.__module__,
                model_exists=True,
                message="OCR 服务正常",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[OCR] 引擎初始化失败: %s", exc)
        return Result.ok(
            OcrHealthOut(
                healthy=False,
                model_path=model_path,
                model_exists=False,
                message=f"引擎初始化失败: {exc}",
            )
        )
