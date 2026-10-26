"""问答对 / 检索测试的 schema。

## ⚠️ 一个必须照抄的"不一致"（Java 遗留契约）

`QaPairServiceImpl.toVO` 里这两行是**不同口径**的：

```java
vo.setSource(qaPair.getSource().getCode());   // "manual" / "import" / "ai"
vo.setStatus(qaPair.getStatus().getDesc());   // "待向量化" / "生效" / "失败"
```

即：**source 出英文 code，status 出中文 desc**。看起来像 bug（两边本该一致），
但前端 `QAPairsTab.vue` 就是照这个写的：

```js
const map = { manual: '手动', import: '导入', ai: 'AI' }        // ← 英文 key
const map = { 待向量化: 'gold', 向量化中: 'processing', ... }     // ← 中文 key
```

Python 版必须**原样保留这个不一致**：改成统一 code（status 出 "pending"）
会让图标全部落到 `v-else` 那个兜底灰图标上 —— 不报错，只是状态永远显示不出来。

**库里存的都是 code**（`@EnumValue` 在 code 字段上），desc 只在出参时转换。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel


# ----------------------------------------------------------------------
# 问答对
# ----------------------------------------------------------------------


class QaPairIn(CamelModel):
    """创建问答对。Java `QaPairCreateDTO`：question / answer 都不能为空、≤2000 字。"""

    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=2000)


class QaPairUpdateIn(CamelModel):
    """更新问答对。

    **两个字段都可选**（Java `QaPairUpdateDTO` 没有 required 标注）：
    只传 answer 时不重算向量，**这点很重要** —— 改答案不需要重新 embedding。
    """

    question: str | None = Field(default=None, max_length=2000)
    answer: str | None = Field(default=None, max_length=2000)


class QaPairOut(CamelModel):
    id: str
    knowledge_id: str
    question: str
    answer: str
    # 见模块 docstring：source 是 code，status 是 desc
    source: str
    status: str
    token_count: int = 0
    create_time: datetime | None = None
    update_time: datetime | None = None


# ----------------------------------------------------------------------
# 检索测试
# ----------------------------------------------------------------------


class KnowledgeSearchIn(CamelModel):
    """`POST /knowledge/{id}/search` 的 body。

    Java 收的是裸 `Map<String,Object>`，字段全靠约定（`question` + `overrides`）。
    这里显式建模，好处是非法值在 422 层就被拦住并给出可读 message
    （记着：全局 422 已被转成 `{code:400}`，前端能弹提示）。
    """

    question: str = Field(min_length=1)
    overrides: dict[str, Any] | None = None


class RagSearchResultOut(CamelModel):
    """对齐 Java `RagSearchResultVO`。

    `result_type`：chunk（文档分块）/ qa_pair（问答对）。
    问答对排在前面（`qa_priority`，默认 true），且它的 `document_name` 是硬编码的
    **"问答对"** 三个字 —— 前端靠这个区分，别改。
    """

    content: str
    rank: int
    score: float | None = None
    document_name: str | None = None
    document_id: str | None = None
    result_type: str | None = None
    original_score: float | None = None
