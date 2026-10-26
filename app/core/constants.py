"""跨模块共享的常量。

对照 Java：相当于 `ConfigKeys` / 实体里的 `public static final`。
抽出来是为了避免多个模块各自定义同一个值然后慢慢漂移 ——
`DEFAULT_TITLE` 尤其危险：标题生成逻辑靠"标题是否等于默认值"来决定要不要改，
两处不一致就会导致标题永远不更新（或者每次都被覆盖）。
"""

# 新会话的初始标题。与 Java ChatSession.DEFAULT_TITLE 完全一致。
DEFAULT_TITLE = "新对话"

# Agent 没配 system_prompt 时的兜底
DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的 AI 助手，请用简洁准确的中文回答。"

# 标题最大长度。Java 是 buildFallbackTitle(messages, 30)
TITLE_MAX_LEN = 30

# 送进模型的最近历史条数。Java 默认 maxContextMessages = 20
HISTORY_LIMIT = 20
