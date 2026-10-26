"""外部能力节点：api（HTTP 调用）/ script（脚本）/ tool（内置工具）。

单独放一个文件而不是塞进 `nodes.py`：这三个节点的共同点是**要访问外部世界**
（网络、子进程、工具注册表），而 `nodes.py` 里那些是纯内存的数据流转。
混在一起会让"哪些节点有副作用"看不出来。

对照 Java：`ApiNodeProcessor` / `ScriptNodeProcessor` / `ToolNodeProcessor`。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from app.workflow.nodes import NodeResult, WorkflowContext
from app.workflow.script_runner import run_script
from app.workflow.state import ExecState, render

logger = logging.getLogger(__name__)

# api 节点默认超时。Java 侧 connect/read 分开配，这里简化为整体超时 ——
# httpx 的 timeout 参数本身就能分别设，但工作流节点配置只给了一个 `timeout`。
DEFAULT_API_TIMEOUT = 30.0


# ----------------------------------------------------------------------
# SSRF 防护
# ----------------------------------------------------------------------

# 这些网段一律不许访问。刻意用"黑名单 + 显式拒绝"而不是"白名单"：
# 白名单会把正常的公网 API 全挡在外面，用户配一个 URL 就报错，体验不可用。
_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 云厂商元数据服务（169.254.169.254）
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 唯一本地地址
    ipaddress.ip_network("fe80::/10"),  # IPv6 链路本地
]


def validate_url(url: str, allow_private: bool = False) -> str | None:
    """校验 URL 是否允许访问。返回错误文案，None 表示通过。

    为什么必须做：api 节点是**用户可配置任意 URL** 的入口。没有校验，任何能编辑
    工作流的人都能让服务端去请求内网地址 —— 典型的 SSRF，能打通云元数据服务
    （169.254.169.254 拿临时凭证）、内网 Redis/PG、甚至 K8s API。

    `allow_private=True` 时跳过内网检查，只保留协议校验。这是给**自托管部署**
    用的：老板的工作流很可能要调自家内网服务，硬禁等于这个节点没法用。
    开关在 `WorkflowContext.allow_private_network`，由部署配置决定，
    **不能**做成节点字段 —— 那等于让用户在画布上自己关掉防护。

    **残余风险（已知且刻意不改）**：这里先解析域名再放行，httpx 之后会**再解析一次**。
      两次解析之间 DNS 可以变（DNS rebinding），把公网 IP 换成内网 IP。
      彻底修需要自己建连到固定 IP 并伪造 Host 头，Java 侧同样没做。
      对内部的低信任场景够用；真开放给外部租户时要补。
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"仅允许 http/https 协议（当前: {scheme or '空'}）"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"

    if allow_private:
        return None

    if host in ("localhost", "localhost.localdomain"):
        return "禁止访问 localhost"

    # 字面量 IP 直接判；域名要解析
    candidates: list[str] = []
    try:
        candidates.append(str(ipaddress.ip_address(host)))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            # 解析不了就交给后续 HTTP 请求去报错 —— 这里直接拒会把
            # "临时 DNS 抖动"变成"工作流失败"，没必要
            logger.warning("api 节点 DNS 解析失败（放行由 HTTP 层处理）: %s %s", host, exc)
            return None
        for info in infos:
            addr = info[4][0]
            if addr not in candidates:
                candidates.append(addr)

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        for net in _BLOCKED_NETS:
            if ip.version == net.version and ip in net:
                return f"禁止访问内网/保留地址: {addr}（{net}）"
    return None


# ----------------------------------------------------------------------
# api 节点
# ----------------------------------------------------------------------


async def handle_api(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """HTTP 调用节点。

    输出三个变量（对齐 Java `ApiNodeProcessor`）：
        statusCode: int    HTTP 状态码
        body:       str    响应体原文
        result:     str    等于 body（老工作流用 {{result}} 取响应）

    **注意 body 是字符串不是对象**。下游 script 节点要靠 `JSON.parse(params.body)`
    自己解析 —— 这是 Java 侧就有的约定，别"顺手优化"成自动解析成 dict：
    响应可能是非 JSON（HTML 报错页、纯文本），自动解析会抛异常把工作流搞挂。
    """
    url = render(str(data.get("url") or ""), state)
    url = str(url or "").strip()
    if not url:
        raise ValueError("API 节点未配置 url")

    err = validate_url(url, allow_private=ctx.allow_private_network)
    if err:
        raise ValueError(err)

    method = str(data.get("method") or "GET").strip().upper()
    timeout_raw = data.get("timeout")
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_API_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_API_TIMEOUT

    headers = _parse_headers(data.get("headers"), state)
    body = render(data.get("body") or "", state)
    body_text = "" if body is None else str(body)

    if body_text and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"

    logger.info("api 节点: %s %s (timeout=%ss)", method, url, timeout)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, content=body_text or None)
    except httpx.TimeoutException as exc:
        raise ValueError(f"HTTP 请求超时（>{timeout:g}s）: {url}") from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"HTTP 请求失败: {type(exc).__name__}: {exc}") from exc

    text = resp.text
    outputs: dict[str, Any] = {
        "statusCode": resp.status_code,
        "body": text,
        "result": text,
    }
    state.variables.update(outputs)
    logger.info("api 节点返回 %s，响应体 %d 字符", resp.status_code, len(text))
    return NodeResult(output=outputs)


def _parse_headers(raw: Any, state: ExecState) -> dict[str, str]:
    """headers 配置可能是 JSON 字符串（可含 `{{}}`），也可能是 dict。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        resolved = render_dict_values(raw, state)
        return {str(k): "" if v is None else str(v) for k, v in resolved.items()}
    text = str(render(str(raw), state) or "").strip()
    if not text or text == "{}":
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("api 节点 headers 不是合法 JSON，已忽略: %s", text[:200])
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in parsed.items()}


def render_dict_values(data: dict, state: ExecState) -> dict:
    return {k: render(v, state) if isinstance(v, str) else v for k, v in data.items()}


# ----------------------------------------------------------------------
# script 节点
# ----------------------------------------------------------------------


async def handle_script(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """脚本节点：把 inputParams 渲染成 dict 传给脚本的 `main(params)`。

    输出规则（对齐 Java `normalizeOutputs`）：
    1. 脚本返回 dict → 逐个键平铺进变量池（`{{title}}` 直接可用）
    2. 返回其它值 → 放进 `result`
    3. `outputParams` 只在**返回值为空**时用来给第一个 key 占位

    失败策略：`errorStrategy == "defaultValue"` 时用 `defaultOutput`，否则抛异常
    中断工作流。为什么要有这个开关：脚本经常是"尽力解析"，解析不了也不想让
    整条流程挂掉，得让用户自己决定。
    """
    code = str(data.get("scriptContent") or "")
    language = str(data.get("scriptLanguage") or "javascript")
    params = _build_script_params(data, state, ctx)

    timeout_raw = data.get("timeout")
    try:
        timeout = float(timeout_raw) if timeout_raw else 30.0
    except (TypeError, ValueError):
        timeout = 30.0

    result = await run_script(code, language, params, timeout_sec=timeout)
    if not result.success:
        strategy = str(data.get("errorStrategy") or "").strip()
        if strategy == "defaultValue":
            fallback = _parse_default_output(data)
            logger.warning("脚本节点失败，按 defaultValue 策略降级: %s", result.error)
            state.variables.update(fallback)
            return NodeResult(output=fallback)
        raise ValueError(f"脚本执行失败: {result.error}")

    outputs = _normalize_outputs(result.value, data)
    state.variables.update(outputs)
    logger.info("脚本节点输出键: %s", list(outputs))
    return NodeResult(output=outputs)


def _build_script_params(
    data: dict, state: ExecState, ctx: WorkflowContext
) -> dict[str, Any]:
    """inputParams: [{key, value}] → {key: 渲染后的值}。

    `value` 常见写法是 `{{body}}` —— `render` 对"整串就是一个变量"会返回
    **原始类型**，所以传进去的是 dict/list 而不是 `"{'a': 1}"` 这种字符串。
    这对 script 节点很关键：脚本里可以直接 `params.chartData` 而不用再 parse。
    """
    params: dict[str, Any] = {}
    items = data.get("inputParams") or data.get("input_params") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            params[key] = render(item.get("value"), state)

    # 兜底：没配 inputParams 时给 input / query（对齐 Java 的 fallback 逻辑）。
    # 注意 `input` 在 variables 里通常就是用户原始问题（input 节点写入的）。
    if not params:
        fallback_input = state.variables.get("input")
        fallback_query = state.variables.get("query", fallback_input)
        params["query"] = fallback_query
        params["input"] = fallback_input
    return params


def _parse_default_output(data: dict) -> dict[str, Any]:
    raw = data.get("defaultOutput")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"result": raw}
        return dict(parsed) if isinstance(parsed, dict) else {"result": parsed}
    return {"result": ""}


def _normalize_outputs(value: Any, data: dict) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        # 返回 null：如果声明了 outputParams，按声明给空占位，
        # 让下游 `{{title}}` 至少渲染成空串而不是"变量不存在"
        params = data.get("outputParams") or []
        if isinstance(params, list) and params:
            first = params[0]
            key = str((first or {}).get("key") or "").strip() if isinstance(first, dict) else ""
            if key:
                return {key: None}
        return {"result": None}
    return {"result": value}


# ----------------------------------------------------------------------
# tool 节点
# ----------------------------------------------------------------------


async def handle_tool(data: dict, state: ExecState, ctx: WorkflowContext) -> NodeResult:
    """内置工具调用节点。

    复用 chat 链路的工具注册表与执行器 —— **不要在这里另写一套调用逻辑**：
    工具的参数校验、错误包装、超时处理都在 `tools/executor.py` 里，
    两边分叉的结果就是"聊天里能用的工具，工作流里行为不一样"。
    """
    from app.tools.executor import execute_tool_call
    from app.tools.registry import registry as tool_registry

    tool_name = str(data.get("toolName") or data.get("name") or "").strip()
    if not tool_name:
        raise ValueError("tool 节点未配置 toolName")

    spec = tool_registry.get(tool_name)
    if spec is None:
        available = ", ".join(tool_registry.implemented) or "（空）"
        raise ValueError(f"tool 节点引用了未注册的工具: {tool_name}（可用: {available}）")

    args = _build_tool_args(data, state)
    result, ok = await execute_tool_call(spec, tool_name, json.dumps(args, ensure_ascii=False))

    parsed = _maybe_json(result)
    outputs: dict[str, Any] = {
        "toolResult": parsed,
        "toolOk": ok,
    }
    # 结果是 dict 时把键也平铺出去，这样 `{{success}}` / `{{files}}`
    # 能直接取到，不用绕 `{{toolResult.files}}`
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            outputs.setdefault(str(k), v)
    state.variables.update(outputs)
    logger.info("tool 节点 %s 执行完成 ok=%s", tool_name, ok)
    return NodeResult(output=outputs)


def _build_tool_args(data: dict, state: ExecState) -> dict[str, Any]:
    """工具参数：优先 inputParams 列表，其次 args 对象。"""
    items = data.get("inputParams") or data.get("input_params") or []
    args: dict[str, Any] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            args[key] = render(item.get("value"), state)
    raw_args = data.get("args")
    if isinstance(raw_args, dict):
        for k, v in raw_args.items():
            args.setdefault(str(k), render(v, state))
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    args.setdefault(str(k), v)
        except json.JSONDecodeError:
            logger.warning("tool 节点 args 不是合法 JSON，已忽略: %s", raw_args[:200])
    return args


def _maybe_json(text: str | None) -> Any:
    if text is None:
        return None
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text
