"""Landing 页配置（对照 Java `LandingController`）。

两个反直觉的点：

1. **`GET` 是公开的**（首页没登录也要渲染），`PUT` 才要求管理员。
   Java 里 GET 不加 `checkAdmin()`，Python 也不能加 —— 否则未登录打开首页直接 401。
2. **PUT 收的是对象，存的是字符串**。
   Java 签名是 `@RequestBody String config`，Spring 会把整个 JSON body 当字符串接住；
   而前端 `SettingsView.saveLandingConfig` 发的是**结构化对象**
   （`{title, subtitles, features...}`），不是 JSON.stringify 后的文本。
   所以 Python 必须两种都吃，落库一律转成字符串，
   才能和老库里已有的 `landing_config` 行、以及 GET 的 `JSON.parse` 对得上。

长度校验也照抄：主标题 30、副标题 30/条、描述 200、功能标题 20、功能描述 40、
GitHub 200、版权 100。这是防"首页排版被撑爆"的最后一道闸。
"""

from __future__ import annotations

import json
import logging

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.api.deps import DbSession
from app.schemas.common import Result
from app.services.system_config_service import SystemConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/landing", tags=["landing"])

KEY_LANDING = "landing_config"
DEFAULT_CONFIG = "{}"

MAX_TITLE = 30
MAX_SUBTITLE = 30
MAX_DESCRIPTION = 200
MAX_FEATURE_TITLE = 20
MAX_FEATURE_DESC = 40
MAX_GITHUB = 200
MAX_COPYRIGHT = 100


@router.get("/config", response_model=Result[str], summary="获取 Landing 页面配置")
async def get_config(db: DbSession) -> Result[str]:
    raw = await SystemConfigService(db).get_raw(KEY_LANDING)
    return Result.ok(raw if raw else DEFAULT_CONFIG)


@router.put("/config", response_model=Result[None], summary="更新 Landing 页面配置")
async def update_config(
    db: DbSession, payload: Any | None = Body(default=None)
) -> Result[None]:
    """接受三种 body，落库统一转成字符串：

    - 结构化对象（前端实际发的）：`{"title": "...", "features": [...]}`
    - JSON 字符串（Java 客户端直传）：`'{"title": "..."}'`
    - 空 / 非法：400
    """
    if payload is None:
        raise HTTPException(status_code=400, detail="配置内容不能为空")

    if isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
        root = payload
    elif isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise HTTPException(status_code=400, detail="配置内容不能为空")
        try:
            root = json.loads(text)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="配置格式错误") from None
    else:
        raise HTTPException(status_code=400, detail="配置格式错误")

    errors = _validate_root(root)
    if errors:
        raise HTTPException(status_code=400, detail="；".join(errors))

    await SystemConfigService(db).set_raw(KEY_LANDING, text, "Landing 页面配置")
    return Result.ok(None)


def _validate_root(root: Any) -> list[str]:
    """返回错误列表（空=通过）。照抄 Java `validateLandingConfig` 的阈值。"""
    if not isinstance(root, dict):
        return ["配置格式错误"]

    errors: list[str] = []
    if len(str(root.get("title", ""))) > MAX_TITLE:
        errors.append(f"主标题不能超过{MAX_TITLE}字")
    if len(str(root.get("description", ""))) > MAX_DESCRIPTION:
        errors.append(f"描述文字不能超过{MAX_DESCRIPTION}字")
    if len(str(root.get("github", ""))) > MAX_GITHUB:
        errors.append(f"GitHub 地址不能超过{MAX_GITHUB}字符")
    if len(str(root.get("copyright", ""))) > MAX_COPYRIGHT:
        errors.append(f"版权信息不能超过{MAX_COPYRIGHT}字")

    subtitles = root.get("subtitles")
    if isinstance(subtitles, list):
        for sub in subtitles:
            if len(str(sub)) > MAX_SUBTITLE:
                errors.append(f"副标题不能超过{MAX_SUBTITLE}字")
                break

    features = root.get("features")
    if isinstance(features, list):
        for feat in features:
            if not isinstance(feat, dict):
                continue
            if len(str(feat.get("title", ""))) > MAX_FEATURE_TITLE:
                errors.append(f"功能标题不能超过{MAX_FEATURE_TITLE}字")
            if len(str(feat.get("desc", ""))) > MAX_FEATURE_DESC:
                errors.append(f"功能描述不能超过{MAX_FEATURE_DESC}字")
    return errors
