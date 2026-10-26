"""工具管理：CRUD + 测试执行 + IO schema。

对照 Java：`ToolServiceImpl`（598 行）。Python 侧短很多，因为：
- 不需要 `resolveToolCallbacks`（AgentScope 的回调注册机制），Python 直接用
  `tools/registry.py` + `tools/executor.py`
- 不需要 Redis 缓存失效（没有工具缓存）

**但业务规则必须一条不落**，尤其是「哪些工具能改」：
- `KNOWLEDGE` 类型：**不可编辑、不可删除、不可禁用** —— 它们由知识库注册器
  自动生成，改了下一次同步就被覆盖，所以直接挡住
- `BUILTIN` 类型：**只能改限流字段** —— 名称/描述/schema 由 `BuiltinToolRegistrar`
  从代码注解同步，用户改了会被覆盖，同样挡住

这两条如果不做，用户改完保存"成功了"，下次启动同步一跑又变回去 ——
数据看起来没坏，但用户的修改静默丢失，这种最难查。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import new_id
from app.models.entities import Tool
from app.schemas.common import PageResult
from app.schemas.tool import ToolIn, ToolOut

logger = logging.getLogger(__name__)

# 这两类工具由注册器托管，用户不能随便改（见模块 docstring）
_PROTECTED_TYPES = {"knowledge"}
# 内置工具只允许改这两个字段
_BUILTIN_EDITABLE = {"rate_limit_enabled", "rate_limit_config"}


def _parse_json_field(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return default if default is not None else {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("工具字段 JSON 解析失败，按默认值处理: %s", text[:100])
        return default if default is not None else {}


def _is_implemented(name: str | None) -> bool:
    """这个工具在当前 Python 实现里是否可执行。

    库里 32 个工具，Python 只实现了十几个（calculator / 文件 / exec / web / ocr）。
    前端「测试」按钮如果对未实现工具开放，用户点一次 400 一次，
    而且报错是"工具未注册"，很容易被理解成"系统坏了"。
    所以列表里带上这个标记，前端可以禁用按钮 + 显示"未实现"。
    """
    if not name:
        return False
    from app.tools.registry import registry as tool_registry

    return tool_registry.get(name) is not None


class ToolService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def page(
        self,
        page_num: int,
        page_size: int,
        keyword: str | None = None,
        tool_type: str | None = None,
        tag: str | None = None,
    ) -> PageResult[ToolOut]:
        """列表 + 过滤。

        `tag` 用 **JSONB `@>` 包含操作符**，对齐 Java 的
        `.apply("tags @> '[\\"{0}\\"]'", tag)`。用 `.contains()` 生成的也是 @>，
        但只能作用于 jsonb 列、且要传 JSON 值，这里显式写 SQL 更直观。
        """
        conditions = [Tool.deleted == 0]
        if keyword:
            like = f"%{keyword}%"
            conditions.append(or_(Tool.name.ilike(like), Tool.display_name.ilike(like)))
        if tool_type:
            conditions.append(Tool.tool_type == tool_type)
        if tag:
            # 用 JSONB 自带的 `.contains()`（生成 `@>`），**不要**手写
            # `op("@>")(func.cast(json.dumps([tag]), JSONB))`。
            # 那个写法看着"更显式"，实测命中 0 条：cast 出来的值被 asyncpg
            # 当 text 绑定，PG 再 cast 成 jsonb 时和列里的数组对不上。
            # 同一个查询用原生 SQL `tags @> $1`（参数传 JSON 字符串）能命中，
            # 说明是 ORM 那层的参数类型问题 —— `.contains()` 会按 jsonb 正确绑定。
            conditions.append(Tool.tags.contains([tag]))

        total = (
            await self.db.execute(select(func.count()).select_from(Tool).where(*conditions))
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    select(Tool)
                    .where(*conditions)
                    .order_by(Tool.create_time.desc(), Tool.id.desc())
                    .offset((page_num - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult.of(
            records=[self._to_out(r) for r in rows],
            total=int(total),
            current=page_num,
            size=page_size,
        )

    async def get(self, tool_id: int) -> ToolOut:
        return self._to_out(await self._require(tool_id))

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    async def create(self, dto: ToolIn) -> ToolOut:
        await self._ensure_name_free(dto.name, exclude_id=None)

        tool = Tool(
            id=new_id(),
            user_id=None,  # builtin/自定义工具都是全局的，Java 侧创建时也没设 user_id
            name=dto.name,
            display_name=dto.display_name,
            description=dto.description,
            tool_type=dto.tool_type or "api",
            input_schema=_parse_json_field(dto.input_schema),
            output_schema=_parse_json_field(dto.output_schema),
            output_example=_parse_json_field(dto.output_example),
            config=_parse_json_field(dto.config),
            endpoint_url=dto.endpoint_url,
            auth_type=dto.auth_type,
            auth_config=_parse_json_field(dto.auth_config),
            tags=dto.tags or [],
            icon=dto.icon,
            status="active",
            rate_limit_enabled=bool(dto.rate_limit_enabled),
            rate_limit_config=_parse_json_field(dto.rate_limit_config, default=None)
            if dto.rate_limit_config
            else None,
            create_time=datetime.now(),
            update_time=datetime.now(),
            deleted=0,
        )
        self.db.add(tool)
        await self.db.commit()
        await self.db.refresh(tool)
        logger.info("创建工具 %s（type=%s）", tool.name, tool.tool_type)
        return self._to_out(tool)

    async def update(self, dto: ToolIn) -> ToolOut:
        if not dto.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少 id")
        tool = await self._require(int(dto.id))

        if tool.tool_type == "knowledge":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="知识库工具由知识库自动管理，不能手动编辑",
            )

        if tool.tool_type == "builtin":
            # 内置工具：只落限流字段，其余字段**静默忽略**（不是报错）
            # 为什么静默而不报错：前端表单是所有字段一起提交的，报错会让用户
            # 以为"什么都没保存成功"，实际上限流是存下来的。
            tool.rate_limit_enabled = bool(dto.rate_limit_enabled)
            tool.rate_limit_config = (
                _parse_json_field(dto.rate_limit_config)
                if dto.rate_limit_config
                else tool.rate_limit_config
            )
            tool.update_time = datetime.now()
            await self.db.commit()
            await self.db.refresh(tool)
            logger.info("更新内置工具 %s（仅限流字段）", tool.name)
            return self._to_out(tool)

        # 自定义工具：全字段更新
        if dto.name and dto.name != tool.name:
            await self._ensure_name_free(dto.name, exclude_id=tool.id)
            tool.name = dto.name
        tool.display_name = dto.display_name
        tool.description = dto.description
        tool.icon = dto.icon
        tool.input_schema = _parse_json_field(dto.input_schema)
        tool.output_schema = _parse_json_field(dto.output_schema)
        tool.output_example = _parse_json_field(dto.output_example)
        tool.config = _parse_json_field(dto.config)
        tool.endpoint_url = dto.endpoint_url
        tool.auth_type = dto.auth_type
        tool.auth_config = _parse_json_field(dto.auth_config)
        tool.tags = dto.tags or []
        tool.rate_limit_enabled = bool(dto.rate_limit_enabled)
        tool.rate_limit_config = (
            _parse_json_field(dto.rate_limit_config) if dto.rate_limit_config else None
        )
        if dto.status:
            tool.status = dto.status
        tool.update_time = datetime.now()

        await self.db.commit()
        await self.db.refresh(tool)
        logger.info("更新自定义工具 %s", tool.name)
        return self._to_out(tool)

    async def delete(self, tool_id: int) -> None:
        tool = await self._require(tool_id)
        if tool.tool_type == "knowledge":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="知识库工具由知识库自动管理，不能删除",
            )
        tool.deleted = 1
        tool.update_time = datetime.now()
        await self.db.commit()
        logger.info("删除工具 %s（软删）", tool.name)

    async def set_enabled(self, tool_id: int, enabled: bool) -> None:
        tool = await self._require(tool_id)
        if tool.tool_type == "knowledge":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="知识库工具由知识库自动管理，不能禁用",
            )
        tool.status = "active" if enabled else "disabled"
        tool.update_time = datetime.now()
        await self.db.commit()

    # ------------------------------------------------------------------
    # 测试执行
    # ------------------------------------------------------------------

    async def test(self, tool_id: int, args: Any) -> str:
        """真跑一次工具，返回原始输出。

        复用 `tools/executor.py`，**不另写一套调用逻辑** —— 参数校验、错误包装、
        超时都在那里。分开写的结果就是"聊天里能跑的工具，测试按钮跑出不一样的结果"。
        """
        from app.tools.executor import execute_tool_call
        from app.tools.registry import registry as tool_registry

        tool = await self._require(tool_id)
        spec = tool_registry.get(tool.name or "")
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"工具 {tool.name} 尚未在 Python 版实现，无法测试执行",
            )

        if isinstance(args, (dict, list)):
            args_json = json.dumps(args, ensure_ascii=False)
        else:
            text = (args or "").strip()
            args_json = text or "{}"

        try:
            json.loads(args_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"参数不是合法 JSON: {exc}",
            ) from exc

        result, ok = await execute_tool_call(spec, tool.name or "", args_json)
        logger.info("测试工具 %s ok=%s len=%d", tool.name, ok, len(result or ""))
        if not ok:
            # Java 侧把失败输出也当正常返回（Result.ok），让前端展示原文。
            # 这里保持一致：不抛 500，把错误文本原样给前端。
            logger.warning("工具 %s 测试失败: %s", tool.name, (result or "")[:200])
        return result or ""

    # ------------------------------------------------------------------
    # IO schema / 示例参数
    # ------------------------------------------------------------------

    async def io_schema(self, tool_id: int) -> dict[str, Any]:
        """给工作流节点配置用的输入/输出参数列表。

        照抄 Java `ToolIoSchemaUtil.buildSchema`：从 `inputSchema.properties`
        提 inputs（带 required 标记），从 `outputSchema.properties` 提 outputs，
        输出为空时给一个默认的 `output`。
        """
        tool = await self._require(tool_id)
        inputs = _extract_params(tool.input_schema, include_required=True)
        outputs = _extract_params(tool.output_schema, include_required=False)
        if not outputs:
            outputs = [
                {
                    "key": "output",
                    "type": "String",
                    "description": "工具原始返回（JSON 字符串）",
                }
            ]
        # ask_user 的输出里还有 answer（HITL resume 后才注入），Java 侧特判补上
        if tool.name == "ask_user" and not any(o["key"] == "answer" for o in outputs):
            outputs.append(
                {
                    "key": "answer",
                    "type": "String",
                    "description": "用户回答（工作流 resume 后注入）",
                }
            )
        return {
            "toolId": str(tool.id),
            "toolName": tool.name,
            "displayName": tool.display_name,
            "inputs": inputs,
            "outputs": outputs,
        }

    async def example_params(self, tool_id: int) -> dict[str, Any]:
        """从 `config.exampleParams` 取示例参数（前端"测试"弹窗预填）。"""
        tool = await self._require(tool_id)
        config = tool.config or {}
        if isinstance(config, dict):
            example = config.get("exampleParams")
            if isinstance(example, dict):
                return example
        return {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require(self, tool_id: int) -> Tool:
        row = (
            await self.db.execute(
                select(Tool).where(Tool.id == tool_id, Tool.deleted == 0)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工具不存在")
        return row

    async def _ensure_name_free(self, name: str, exclude_id: int | None) -> None:
        stmt = select(func.count()).select_from(Tool).where(
            Tool.name == name, Tool.deleted == 0
        )
        if exclude_id is not None:
            stmt = stmt.where(Tool.id != exclude_id)
        if int((await self.db.execute(stmt)).scalar_one()) > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"工具名 {name} 已存在",
            )

    def _to_out(self, row: Tool) -> ToolOut:
        out = ToolOut.model_validate(row)
        out.implemented = _is_implemented(row.name)
        return out



def _extract_params(schema: Any, *, include_required: bool) -> list[dict[str, Any]]:
    """从 JSON Schema 里提参数列表。

    `type` 转成前端认识的 5 种（String/Number/Boolean/Array/Object）——
    直接给 JSON Schema 原始类型（string/integer）会让前端映射表对不上，
    显示成一堆 "string" 而不是"文本"。
    """
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = schema.get("required") if include_required else None
    required_keys = set(required) if isinstance(required, list) else set()

    rows: list[dict[str, Any]] = []
    for key, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        row: dict[str, Any] = {
            "key": key,
            "type": _capitalize_type(prop.get("type")),
        }
        if prop.get("description"):
            row["description"] = str(prop["description"])
        if include_required:
            row["required"] = key in required_keys
        rows.append(row)
    return rows


def _capitalize_type(raw: Any) -> str:
    text = (str(raw) if raw is not None else "string").lower()
    return {
        "integer": "Number",
        "number": "Number",
        "boolean": "Boolean",
        "array": "Array",
        "object": "Object",
    }.get(text, "String")
