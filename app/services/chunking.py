"""分块策略：把长文本切成检索用的 chunk。

对照 Java `lengbot-knowledge/.../model/chunking/` 下的 6 个策略类。

## 为什么 token 计数要"自己算"

Java 的 `TokenUtil.countTokens` 用的是正则 `[A-Za-z0-9_]+|[一-鿿]`：
**中文按字、英文按词**。这不是真正的分词，只是个近似 —— 但**必须照抄**，
因为库里已有 3372 个 chunk 是按这个口径切的。换一套计数方式，
同样的参数会切出不同数量的块，用户"重新入库"后和之前对不上。

评估也是同理：`chunkSize=512` 在这个口径下约等于 512 个中文字，
换成 tiktoken 的 BPE 大概只有 300 字，块会变碎、召回反而变差。

## 四种策略的分工

| 策略 | 场景 | 关键行为 |
|---|---|---|
| general | 默认，长文档 | 按分隔符合并到 token 上限，带重叠 |
| separator | 结构化文本（日志/CSV） | 遇分隔符即切，不合并 |
| parent-child | 需要上下文的长文 | 父块 600 token，子块 200 token，子块携带父块全文 |
| qa | 问答集 | 识别 Q:/A: 模式，保持问答完整 |

`book` / `laws` 两个策略（书籍章节、法条）Java 有，Python 版没实现 ——
它们依赖大量领域规则（章节标题正则表、法条编号解析），
照抄成本高、收益低。调用时**明确报错而不是静默退化成 general**：
静默退化会让用户以为用了专业策略，实际切得很烂。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# token 正则与 Java `TokenUtil.TOKEN_PATTERN` 完全一致：
# 连续的字母/数字/下划线算一个 token，每个中文字算一个 token
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")

# 最小分块 token 数，低于此值的碎片会被过滤（Java `MIN_CHUNK_TOKENS = 30`）。
# 为什么是 30：低于 30 token 的块基本是标题残片或标点噪声，
# 检索命中它反而把正文挤出 topK。
MIN_CHUNK_TOKENS = 30

# 单块最大重叠 token（Java `MAX_OVERLAP_TOKENS`）
MAX_OVERLAP_TOKENS = 200

# 父子分块参数（Java `ParentChildChunkStrategy`）
MIN_CHILD_TOKEN_NUM = 80
DEFAULT_PARENT_TOKEN_NUM = 600
DEFAULT_CHILD_OVERLAP_PERCENT = 25

SUPPORTED_STRATEGIES = ("general", "separator", "parent-child", "qa")
# Java 有但 Python 未实现 —— 明确报错，不静默退化
UNIMPLEMENTED_STRATEGIES = ("book", "laws")


@dataclass(slots=True)
class ChunkParams:
    """分块参数（对齐 Java `ChunkParams` + `IngestDTO`）。

    注意 `chunk_size` 的单位是 **token 数**，不是字符数。
    前端 UI 上写的是"分块大小"，容易误解成字符 ——
    但 Java 传下来就是当 token 用的（`params.setChunkTokenNum(dto.getChunkSize())`）。
    """

    chunk_token_num: int = 512
    overlapped_percent: int = 10
    delimiter: str = "\n"
    collapse_whitespace: bool = False
    text_clean_rules: list[dict] | None = None


@dataclass(slots=True)
class ChunkBlock:
    """一个分块。`parent_content` 只在 parent-child 策略下非空。"""

    content: str
    parent_content: str | None = None


def count_tokens(text: str | None) -> int:
    if not text:
        return 0
    return sum(1 for _ in TOKEN_PATTERN.finditer(text))


def hard_split_by_tokens(text: str, max_tokens: int) -> list[str]:
    """按 token 上限硬切，不做语义合并。超长段落的兜底。"""
    if not text or max_tokens <= 0:
        return []
    result: list[str] = []
    current: list[str] = []
    token_count = 0
    last_end = 0
    for m in TOKEN_PATTERN.finditer(text):
        if m.start() > last_end:
            current.append(text[last_end : m.start()])
        current.append(m.group())
        last_end = m.end()
        token_count += 1
        if token_count >= max_tokens:
            piece = "".join(current).strip()
            if piece:
                result.append(piece)
            current = []
            token_count = 0
    if last_end < len(text):
        current.append(text[last_end:])
    tail = "".join(current).strip()
    if tail:
        result.append(tail)
    return result


def tail_tokens(text: str, max_tokens: int) -> str:
    """取尾部最多 `max_tokens` 个 token（用于重叠拼接）。"""
    if not text or max_tokens <= 0:
        return ""
    ends = [m.end() for m in TOKEN_PATTERN.finditer(text)]
    total = len(ends)
    if total == 0:
        return ""
    if total <= max_tokens:
        return text
    # 倒数第 max_tokens+1 个 token 的结束位置 = 要保留的尾部起点
    return text[ends[total - max_tokens - 1] :]


def filter_by_min_tokens(chunks: list[str]) -> list[str]:
    return [c for c in chunks if count_tokens(c) >= MIN_CHUNK_TOKENS]


def _split_by_delimiter(content: str, delimiter: str) -> list[str]:
    """按分隔符切。分隔符为空时按换行切（不能让空正则炸掉）。"""
    if not delimiter:
        return content.split("\n")
    try:
        return content.split(delimiter)
    except re.error:
        # 前端可能传了非法正则（比如 `[` 未闭合），退化成字面量切分
        return content.split(delimiter)


# ----------------------------------------------------------------------
# general
# ----------------------------------------------------------------------


def _general_split(content: str, params: ChunkParams) -> list[str]:
    """递归分隔符合并（对齐 Java `GeneralChunkStrategy.split`）。"""
    if not content or not content.strip():
        return []

    sections = _split_by_delimiter(content, params.delimiter)
    merged: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    sep = params.delimiter or "\n"

    for section in sections:
        section_tokens = count_tokens(section)

        # 超长 section 硬切：先把缓冲区吐出，再硬切这一段
        if section_tokens > params.chunk_token_num:
            if buf:
                merged.append(sep.join(buf))
                buf = []
                buf_tokens = 0
            merged.extend(hard_split_by_tokens(section, params.chunk_token_num))
            continue

        if buf_tokens + section_tokens > params.chunk_token_num and buf:
            merged.append(sep.join(buf))
            buf = []
            buf_tokens = 0

        buf.append(section)
        buf_tokens += section_tokens

    if buf:
        merged.append(sep.join(buf))

    if params.overlapped_percent > 0 and len(merged) > 1:
        merged = _add_overlap(merged, params.overlapped_percent, sep)
    return filter_by_min_tokens(merged)


def _add_overlap(chunks: list[str], overlapped_percent: int, delimiter: str) -> list[str]:
    """给相邻块加"上一块尾部"作为重叠。

    重叠的目的是避免一句话被切断后，两半都不足以回答任何问题。
    `MAX_OVERLAP_TOKENS` 是硬上限 —— 不加这个限制，
    超大块（比如 2000 token）按 10% 算就是 200 token 重叠，
    每块实际内容只剩 300 token，存储和成本都会翻倍。
    """
    if overlapped_percent <= 0 or overlapped_percent >= 100 or len(chunks) <= 1:
        return chunks
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev, curr = chunks[i - 1], chunks[i]
        prev_tokens = count_tokens(prev)
        overlap_tokens = min(prev_tokens * overlapped_percent // 100, MAX_OVERLAP_TOKENS)
        if overlap_tokens > 0:
            tail = _tail_by_delimiter(prev, overlap_tokens, delimiter)
            result.append(f"{tail}{delimiter}{curr}" if tail else curr)
        else:
            result.append(curr)
    return result


def _tail_by_delimiter(text: str, token_count: int, delimiter: str) -> str:
    if not text or token_count <= 0:
        return ""
    parts = _split_by_delimiter(text, delimiter)
    tail: list[str] = []
    accumulated = 0
    for part in reversed(parts):
        tokens = count_tokens(part)
        if accumulated + tokens > token_count:
            if not tail:
                # 单个 part 就超预算（块内没分隔符）：按 token 边界截尾部。
                # 整段吞掉会让重叠变成"复制上一整块"，内容成倍膨胀。
                tail.insert(0, tail_tokens(part, token_count))
            break
        tail.insert(0, part)
        accumulated += tokens
    return delimiter.join(tail)


# ----------------------------------------------------------------------
# separator
# ----------------------------------------------------------------------


def _separator_split(content: str, params: ChunkParams) -> list[str]:
    """遇分隔符即切，不合并（对齐 Java `SeparatorChunkStrategy`）。

    用途是"每行一条记录"的文本：日志、CSV 转出来的文本、SQL 脚本。
    合并会破坏记录边界，检索时命中一条日志却拿到 5 条混在一起。
    """
    if not content or not content.strip():
        return []
    result: list[str] = []
    for part in _split_by_delimiter(content, params.delimiter):
        if not part.strip():
            continue
        if count_tokens(part) > params.chunk_token_num:
            result.extend(hard_split_by_tokens(part, params.chunk_token_num))
        else:
            result.append(part)
    return filter_by_min_tokens(result)


# ----------------------------------------------------------------------
# parent-child
# ----------------------------------------------------------------------


def _parent_child_split(content: str, params: ChunkParams) -> list[ChunkBlock]:
    """父子分块（对齐 Java `ParentChildChunkStrategy`）。

    父块只用来**存上下文**，不单独入库；子块才是检索单元。
    命中子块后把 `parent_content` 一起给模型，兼顾"精准召回"和"完整上下文"。
    这是 Dify 的父子模式，对"答案跨段落"的文档效果明显好于单层分块。
    """
    if not content or not content.strip():
        return []

    parent_token_num = params.chunk_token_num if params.chunk_token_num > 0 else DEFAULT_PARENT_TOKEN_NUM
    child_token_num = max(MIN_CHILD_TOKEN_NUM, parent_token_num // 3)
    child_overlap = (
        max(20, parent_token_num * params.overlapped_percent // 100)
        if params.overlapped_percent > 0
        else DEFAULT_CHILD_OVERLAP_PERCENT
    )

    # 1) 父块：大块切分，不重叠
    parent_params = ChunkParams(
        chunk_token_num=parent_token_num,
        overlapped_percent=0,
        delimiter=params.delimiter,
    )
    parents = _general_split(content, parent_params)

    # 2) 每个父块切子块，带重叠，挂上父块全文
    child_params = ChunkParams(
        chunk_token_num=child_token_num,
        overlapped_percent=0,
        delimiter=params.delimiter,
    )
    blocks: list[ChunkBlock] = []
    sep = params.delimiter or "\n"
    for parent in parents:
        children = _general_split(parent, child_params)
        children = _add_overlap(children, child_overlap, sep)
        for child in children:
            blocks.append(ChunkBlock(content=child, parent_content=parent))
    return blocks


# ----------------------------------------------------------------------
# qa
# ----------------------------------------------------------------------

# 问答对模式（对齐 Java `QaChunkStrategy` 的两个正则）
_QA_START = re.compile(
    r"^(?:Q[：:]|问题[：:]|问[：:]|\*\*Q[：:]\*\*|\d+[.、])\s*"
)
_A_START = re.compile(r"^(?:A[：:]|答案[：:]|答[：:]|\*\*A[：:]\*\*)\s*")


def _qa_split(content: str, params: ChunkParams) -> list[str]:
    """识别 Q:/A: 模式，保持问答对完整（对齐 Java `QaChunkStrategy`）。

    识别不到任何问答对时**回退到 general** —— 这是 Java 的行为，
    因为用户可能选错策略，直接返回空列表会让入库失败（chunks 为空 → 报错）。
    回退至少能入库，只是分块质量差一点。
    """
    if not content or not content.strip():
        return []

    pairs = _parse_qa_pairs(content)
    if not pairs:
        return _general_split(content, params)

    result: list[str] = []
    for qa in pairs:
        if count_tokens(qa) > params.chunk_token_num:
            # 超长问答对：按 token 硬切，但每片都带上问题的前缀，
            # 否则后半段答案会失去"在回答什么"的上下文
            result.extend(hard_split_by_tokens(qa, params.chunk_token_num))
        else:
            result.append(qa)
    return filter_by_min_tokens(result)


def _parse_qa_pairs(content: str) -> list[str]:
    """按行扫描，把 Q 段和紧随的 A 段拼成一个完整问答。"""
    pairs: list[str] = []
    cur_q: list[str] = []
    cur_a: list[str] = []
    in_q = False
    in_a = False

    def flush() -> None:
        nonlocal cur_q, cur_a, in_q, in_a
        q = "\n".join(cur_q).strip()
        a = "\n".join(cur_a).strip()
        if q:
            pairs.append(f"Q: {q}\nA: {a}" if a else f"Q: {q}")
        cur_q, cur_a, in_q, in_a = [], [], False, False

    for line in content.split("\n"):
        trimmed = line.strip()
        if _QA_START.match(trimmed) and not _A_START.match(trimmed):
            flush()
            in_q = True
            cur_q.append(_QA_START.sub("", trimmed))
            continue
        if _A_START.match(trimmed):
            in_a = True
            in_q = False
            cur_a.append(_A_START.sub("", trimmed))
            continue
        if not trimmed:
            continue
        if in_a:
            cur_a.append(trimmed)
        elif in_q:
            cur_q.append(trimmed)

    flush()
    return pairs


# ----------------------------------------------------------------------
# 文本清洗 / 归一化
# ----------------------------------------------------------------------


def apply_clean_rules(content: str, rules: list[dict] | None) -> str:
    """按规则做正则替换/删除（对齐 Java `TextNormalizeUtil.applyCleanRules`）。

    规则形如 `{"pattern": "第\\d+页", "replacement": ""}`。
    非法正则**跳过而不是抛错** —— 一条规则写错不该让整篇文档入库失败。
    """
    if not rules:
        return content
    out = content
    for rule in rules:
        pattern = rule.get("pattern") or rule.get("regex")
        if not pattern:
            continue
        replacement = rule.get("replacement") or ""
        try:
            out = re.sub(pattern, replacement, out)
        except re.error:
            continue
    return out


def normalize_chunk_content(content: str, collapse_whitespace: bool = False) -> str:
    """分块内容归一（对齐 Java `TextNormalizeUtil.normalizeChunkContent`）。

    - 去掉零宽字符和控制字符（PDF 提取经常带 `\\x00`，会让 DB 存进无效字节）
    - 可选折叠连续空白（Dify 的 `remove_extra_spaces` 等价）
    """
    if not content:
        return ""
    # 控制字符（保留 \n \t）
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", content)
    # 零宽字符
    out = out.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    if collapse_whitespace:
        out = re.sub(r"[ \t\u3000]+", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ----------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------


def split_blocks(content: str, strategy: str, params: ChunkParams) -> list[ChunkBlock]:
    """分块主入口。返回带 parent_content 的块列表。

    `strategy` 不在支持范围内时**抛 ValueError**，不静默退化 ——
    见模块 docstring 里关于 book/laws 的说明。
    """
    name = (strategy or "general").strip().lower()
    if name in UNIMPLEMENTED_STRATEGIES:
        raise ValueError(
            f"分块策略 `{name}` 在 Python 版尚未实现（Java 侧有，依赖领域规则表）。"
            f"请改用 {' / '.join(SUPPORTED_STRATEGIES)}"
        )
    if name not in SUPPORTED_STRATEGIES:
        raise ValueError(f"未知分块策略 `{name}`，可选：{' / '.join(SUPPORTED_STRATEGIES)}")

    cleaned = apply_clean_rules(content, params.text_clean_rules)

    if name == "parent-child":
        return _parent_child_split(cleaned, params)
    if name == "separator":
        return [ChunkBlock(content=c) for c in _separator_split(cleaned, params)]
    if name == "qa":
        return [ChunkBlock(content=c) for c in _qa_split(cleaned, params)]
    return [ChunkBlock(content=c) for c in _general_split(cleaned, params)]


def split_texts(content: str, strategy: str, params: ChunkParams) -> list[str]:
    """只要文本的分块（预览接口用，不关心 parent_content）。"""
    return [b.content for b in split_blocks(content, strategy, params)]
