"""网页抓取：URL → 标题 + 正文。

对照 Java `WebFetchUtil`。

## 三步走，缺一不可

1. **抓 HTML**：`httpx` + 浏览器 UA。不带 UA 时大量站点直接返回 403
   （Cloudflare / 知乎 / 微信公众号都会拦 Python-urllib 的默认 UA）。
2. **正文抽取**：BeautifulSoup 去掉 script/style/nav/footer/header/aside，
   然后按"块级元素密度"挑正文容器。**不用 readability-lxml**：
   那个库两年没更新，对现代 SPA 页面抽出来的东西比自己写的规则还差。
3. **HTML → markdown**：简单替换（h1-h6 / p / li / table），
   不做完整 markdown 转换 —— 正文是给检索用的，
   保留语义结构（标题层级）比保留格式（加粗/斜体）重要得多。

## SSRF 防护

这个功能允许用户传任意 URL，必须挡住内网：
- 回环 / 私有网段 / 链路本地（169.254）/ 元数据地址（169.254.169.254）
- 非 http/https 协议（`file://` 能读本地文件）

判定在**解析域名之后**做 —— 只检查字符串 `127.0.0.1` 会被
`0x7f000001` / `2130706433` / DNS rebinding 绕过。
这里解析出 IP 再判，且**禁止重定向跟随**（重定向到内网是经典绕过手法）。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 抓取超时。20s 是折中：大多数页面 1-3s 就返回，
# 慢的站点（政府/学校网站）经常 10s+，超过 20s 基本是死了。
FETCH_TIMEOUT = 20.0

# 浏览器 UA。不设这个，一半的站点直接给 403 或返回验证页。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 正文最大长度。1MB 的 HTML 抽出来正文大概几十万字，
# 全都入库没意义（一本书才 20 万字）。
MAX_CONTENT_CHARS = 500_000

# 抓取上限（字节）。防止拉一个 500MB 的"网页"把内存打爆。
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 明显不是正文的标签
_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe")
# 正文容器的候选（按优先级），抽不到就用 body
_CONTENT_SELECTORS = ("article", "main", '[role="main"]', "#content", ".content", "#article", ".article")


@dataclass(slots=True)
class FetchResult:
    url: str
    title: str
    content: str
    preview_html: str
    suggested_file_name: str
    description: str = ""


class SsrfBlocked(Exception):
    """目标地址不安全（内网/回环/非 http 协议）。"""


def _ensure_public_url(url: str) -> None:
    """SSRF 防护：解析域名 → 判 IP 是否公网。

    **为什么必须解析而不是匹配字符串**：
    `http://0x7f000001/` / `http://2130706433/` / `http://[::1]/` 都是回环地址
    但字符串里没有 "127.0.0.1"。只做字符串检查等于没检查。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SsrfBlocked(f"只支持 http/https 协议，收到的是 {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise SsrfBlocked("URL 缺少主机名")

    try:
        # getaddrinfo 会做系统 DNS 解析，拿到所有 A/AAAA 记录
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"域名解析失败: {host}") from exc

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SsrfBlocked(f"拒绝访问内网地址: {host} → {addr}")


def _pick_content(soup):
    """挑正文容器。找不到就用 body。"""
    from bs4 import BeautifulSoup  # noqa: F401

    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    return soup.body or soup


def _to_markdown(node) -> str:
    """把正文节点转成保留结构的纯文本 / 简易 markdown。

    只处理标题、段落、列表、表格、代码 —— 这几类是检索真正吃到的语义。
    不处理加粗/斜体/链接，因为：
    - 加粗斜体对检索毫无价值（还占 token）
    - 链接的 URL 会污染正文（`[文字](https://...)` 让 embedding 混入噪声）
    """
    lines: list[str] = []

    for el in node.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "td", "th"]
    ):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        name = el.name
        if name.startswith("h") and len(name) == 2:
            level = int(name[1])
            lines.append(f"{'#' * level} {text}")
        elif name == "li":
            lines.append(f"- {text}")
        elif name == "pre":
            lines.append(f"```\n{text}\n```")
        elif name == "blockquote":
            lines.append(f"> {text}")
        elif name in {"td", "th"}:
            lines.append(f"| {text} |")
        else:
            lines.append(text)

    # 一个块都没抽到（比如纯 div 排版的老页面）→ 退回纯文本
    if not lines:
        return node.get_text("\n", strip=True)

    # 去重相邻重复行（嵌套标签会让同一段文字出现两次）
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n\n".join(deduped)


def _make_filename(title: str, url: str) -> str:
    import re

    base = (title or "").strip()
    if not base:
        base = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "webpage"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)[:80].strip()
    return f"{base or 'webpage'}.txt"


async def fetch_url(url: str, headers: dict[str, str] | None = None) -> FetchResult:
    """抓取并抽取正文。失败抛 httpx 异常 / SsrfBlocked。"""
    _ensure_public_url(url)

    req_headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        req_headers.update({str(k): str(v) for k, v in headers.items()})

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,  # 关键：不跟随重定向，见模块 docstring
        max_redirects=0,
    ) as client:
        resp = await client.get(url, headers=req_headers)
        # 3xx 明确告知：跟随会被用来绕过 SSRF 检查
        if resp.is_redirect:
            location = resp.headers.get("location", "")
            raise SsrfBlocked(f"目标返回重定向（{resp.status_code} → {location}），已拒绝跟随")

        raw = resp.content[:MAX_RESPONSE_BYTES]
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"抓取失败：HTTP {resp.status_code}", request=resp.request, response=resp
            )

    # 编码：httpx 会按 header 猜，但中文站点经常不声明或声明错
    encoding = resp.encoding or "utf-8"
    try:
        html = raw.decode(encoding, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""

    # 描述：优先 og:description / meta description
    description = ""
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in {"og:description", "description"}:
            description = (meta.get("content") or "").strip()
            if description:
                break

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    content_node = _pick_content(soup)
    preview_html = str(content_node)[:200_000]
    content = _to_markdown(content_node)[:MAX_CONTENT_CHARS]

    logger.info(
        "[网页抓取] url=%s title=%s chars=%s", url, title[:50], len(content)
    )
    return FetchResult(
        url=url,
        title=title,
        content=content,
        preview_html=preview_html,
        suggested_file_name=_make_filename(title, url),
        description=description,
    )
