"""内置示例模板（评测集 4 个 + 评估器 4 个）。

逐字对齐 Java：
- `EvalDatasetExampleTemplates`（4 个评测集，各带样本）
- `EvalEvaluatorExampleTemplates`（4 个评估器，各带提示词 + 变量 + 模型配置）

**为什么硬编码而不是存库**：这些是"上手模板"，跟着代码版本走，
不应该让用户改、也不该在升级时被旧库数据覆盖。Java 也是硬编码在 util 类里。

`modelConfig` 里写的是 `{"temperature":0.1}`，**没有 providerId** ——
用户创建后必须自己去选模型。这是有意的：内置模板不能绑死某家的模型，
否则换个环境（没有那个 provider）就整个失效。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.eval import EvalDatasetOut

logger = logging.getLogger(__name__)

# 两列的标准配置：输入 + 参考答案
_COLUMNS_IO = [
    {"key": "input", "label": "输入", "type": "text"},
    {"key": "reference_output", "label": "参考答案", "type": "text"},
]


# ======================================================================
# 评测集示例
# ======================================================================
def _item(input_text: str, reference: str) -> dict[str, Any]:
    return {"input": input_text, "reference_output": reference}


DATASET_EXAMPLES: dict[str, dict[str, Any]] = {
    "prompt_quality": {
        "name": "Prompt 效果评测集",
        "description": "测试 Prompt 模板的回答质量，包含多样化的输入场景和参考答案",
        "tags": ["准确性", "完整性", "相关性"],
        "columnsConfig": _COLUMNS_IO,
        "items": [
            _item(
                "请解释什么是微服务架构",
                "微服务架构是一种将应用拆分为一组小型、独立部署的服务的架构风格，"
                "每个服务围绕业务能力构建，通过轻量级通信机制协作。",
            ),
            _item(
                "写一段Python代码实现冒泡排序",
                "def bubble_sort(arr):\n    n = len(arr)\n"
                "    for i in range(n):\n        for j in range(0, n-i-1):\n"
                "            if arr[j] > arr[j+1]:\n"
                "                arr[j], arr[j+1] = arr[j+1], arr[j]\n    return arr",
            ),
            _item(
                "帮我写一封请假邮件",
                "主题：请假申请\n\n尊敬的领导：\n您好！因个人原因，"
                "需请假一天（X月X日），届时工作已交接给同事XX，望批准。",
            ),
            _item(
                "用通俗语言解释量子计算",
                "量子计算利用量子力学的叠加和纠缠特性，让量子比特同时处于0和1的状态，"
                "从而并行处理大量计算任务，比传统计算机快指数级别。",
            ),
            _item(
                "推荐3本编程入门书籍",
                "1.Python编程从入门到实践-适合零基础\n"
                "2.Head First Java-图文并茂易理解\n"
                "3.JavaScript高级程序设计-前端必读经典",
            ),
        ],
    },
    "qa_accuracy": {
        "name": "问答准确性评测集",
        "description": "评测问答系统的回答准确性，适用于 RAG 知识库问答场景",
        "tags": ["事实正确", "无幻觉", "引用准确"],
        "columnsConfig": _COLUMNS_IO,
        "items": [
            _item(
                "公司的退货政策是什么？",
                "自购买之日起7天内，商品未使用且包装完好的情况下可申请无理由退货，"
                "退款将在3个工作日内到账。",
            ),
            _item(
                "如何重置账户密码？",
                "进入登录页点击忘记密码，输入注册邮箱，系统将发送重置链接，链接有效期30分钟。",
            ),
            _item(
                "产品支持哪些操作系统？",
                "支持 Windows 10及以上、macOS 12及以上、Ubuntu 20.04及以上版本。"
                "移动端支持 iOS 15+ 和 Android 12+。",
            ),
            _item(
                "数据备份的频率是多久？",
                "系统每天凌晨3点自动执行全量备份，每4小时执行增量备份，备份数据保留30天。",
            ),
            _item(
                "API的调用频率限制是多少？",
                "免费版每分钟60次请求，专业版每分钟600次，企业版可自定义限流策略。"
                "超出限制返回429状态码。",
            ),
        ],
    },
    "text_classification": {
        "name": "文本分类评测集",
        "description": "评测文本分类任务的准确率，包含正向/负向/中性三类样本",
        "tags": ["分类准确", "边界清晰"],
        "columnsConfig": [
            {"key": "input", "label": "文本", "type": "text"},
            {"key": "reference_output", "label": "分类", "type": "text"},
        ],
        "items": [
            _item("这个产品太好用了，强烈推荐给大家！", "正向"),
            _item("客服态度很差，等了两个小时才回复", "负向"),
            _item("产品功能基本符合预期，中规中矩", "中性"),
            _item("包装破损，商品有明显划痕，非常失望", "负向"),
            _item("物流很快，第二天就收到了，质量也不错", "正向"),
            _item("价格和描述一致，没有惊喜也没有失望", "中性"),
        ],
    },
    "summarization": {
        "name": "文本摘要评测集",
        "description": "评测文本摘要的质量，评估关键信息保留和语言流畅度",
        "tags": ["信息保留", "语言流畅", "长度适中"],
        "columnsConfig": _COLUMNS_IO,
        "items": [
            _item(
                "请总结：人工智能的发展经历了三次浪潮。第一次是20世纪50年代的符号主义，"
                "依靠逻辑推理；第二次是80年代的专家系统，依赖人工编写规则；"
                "第三次是2010年后的深度学习，依靠大数据和算力。"
                "当前大模型时代，参数规模从亿级增长到万亿级。",
                "人工智能经历符号主义、专家系统、深度学习三次浪潮，"
                "当前大模型参数规模已达万亿级。",
            ),
            _item(
                "请总结：远程办公的优势包括节省通勤时间、提高工作灵活性、"
                "降低企业办公成本；劣势包括沟通效率下降、团队协作难度增加、"
                "工作与生活边界模糊。企业需要建立异步沟通机制来平衡。",
                "远程办公节省成本、提升灵活性，但沟通协作效率下降，"
                "需靠异步沟通机制平衡。",
            ),
            _item(
                "请总结：向量数据库通过存储高维向量并建立近似最近邻索引（如 HNSW），"
                "实现语义级检索。与传统数据库的关键词匹配不同，"
                "它能理解语义相似性，广泛应用于 RAG、推荐系统、图像检索等场景。",
                "向量数据库用 HNSW 等索引做语义检索，与传统关键词匹配不同，"
                "常用于 RAG 和推荐系统。",
            ),
            _item(
                "请总结：微服务架构将单体应用拆分为多个独立服务，每个服务可独立部署、"
                "独立扩缩容。优点是技术栈灵活、故障隔离；"
                "缺点是分布式事务复杂、运维成本高。适合业务复杂度高的中大型团队。",
                "微服务拆分独立部署，优点是弹性与隔离，"
                "缺点是分布式事务与运维复杂，适合中大型团队。",
            ),
        ],
    },
}


def list_dataset_examples() -> list[dict[str, Any]]:
    """列表页要的摘要（不含样本内容，只给条数）。"""
    return [
        {
            "key": key,
            "name": data["name"],
            "description": data["description"],
            "tags": data["tags"],
            "itemCount": len(data["items"]),
        }
        for key, data in DATASET_EXAMPLES.items()
    ]


async def create_dataset_from_example(
    db: AsyncSession, key: str, user_id: int
) -> EvalDatasetOut:
    """从示例创建：建集 → 塞样本 → 建 v1.0 版本。

    名称冲突时追加 ` (2)` / ` (3)` ……（Java 用 `count+1`）。
    """
    from app.schemas.eval import EvalDatasetIn
    from app.services.eval_dataset_service import EvalDatasetService

    data = DATASET_EXAMPLES.get(key)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"示例不存在：{key}"
        )

    svc = EvalDatasetService(db)
    # 名称冲突自动加后缀：查已用名称（含软删的也算，避免和历史记录撞）
    base = data["name"]
    name = base
    existing = set()
    from sqlalchemy import select

    from app.models.entities import EvalDataset

    rows = (
        await db.execute(
            select(EvalDataset.name).where(EvalDataset.name.like(f"{base}%"))
        )
    ).scalars().all()
    existing = {str(x) for x in rows}
    suffix = 2
    while name in existing:
        name = f"{base} ({suffix})"
        suffix += 1

    dataset = await svc.create(
        EvalDatasetIn(
            name=name,
            description=data["description"],
            columns_config=json.dumps(data["columnsConfig"], ensure_ascii=False),
        ),
        user_id,
    )
    await svc.batch_add_items(int(dataset.id), list(data["items"]))
    # 自动建 v1.0 版本（Java 同）
    try:
        await svc.create_version(int(dataset.id), "v1.0")
    except HTTPException:
        # 样本为空理论上不会发生（内置模板都有数据），但别让整个创建失败
        logger.warning("示例 %s 建版本失败（样本为空？）", key)
    return dataset


# ======================================================================
# 评估器示例
# ======================================================================
EVALUATOR_EXAMPLES: dict[str, dict[str, Any]] = {
    "text_similarity": {
        "name": "文本相似度评估器",
        "description": "评估两段文本的语义相似度，输出 0-1 分数。适用于翻译质量、改写一致性等场景",
        "tags": ["相似度", "语义匹配"],
        "prompt": (
            "请评估以下两个文本的语义相似度。\n\n"
            "评分标准：\n"
            "- 1.0：完全相同或语义一致\n"
            "- 0.8：高度相似，仅有细微差异\n"
            "- 0.6：大部分相似，有少量不同\n"
            "- 0.4：部分相似，有明显差异\n"
            "- 0.2：略有相关，大部分不同\n"
            "- 0.0：完全不相关\n\n"
            "参考文本：{{reference_output}}\n\n"
            "待评估文本：{{actual_output}}\n\n"
            '请输出 JSON 格式：{"score": 分数, "reason": "评分理由"}'
        ),
        "variables": ["reference_output", "actual_output"],
        "modelConfig": {"temperature": 0.1},
    },
    "answer_completeness": {
        "name": "回答完整性评估器",
        "description": "评估 AI 回答是否完整覆盖了参考答案的关键信息点，适用于 RAG 问答和知识库场景",
        "tags": ["完整性", "信息覆盖", "RAG"],
        "prompt": (
            "请评估以下 AI 回答是否完整覆盖了参考答案中的关键信息。\n\n"
            "评分标准：\n"
            "- 1.0：完整覆盖所有关键信息点\n"
            "- 0.8：覆盖大部分关键信息，遗漏次要细节\n"
            "- 0.6：覆盖约一半关键信息\n"
            "- 0.4：仅覆盖少量关键信息\n"
            "- 0.2：几乎未覆盖关键信息\n"
            "- 0.0：完全未覆盖或回答错误\n\n"
            "用户问题：{{input}}\n\n"
            "参考答案：{{expected_output}}\n\n"
            "AI 回答：{{actual_output}}\n\n"
            '请输出 JSON 格式：{"score": 分数, "reason": "评分理由，列出覆盖和遗漏的信息点"}'
        ),
        "variables": ["input", "expected_output", "actual_output"],
        "modelConfig": {"temperature": 0.1},
    },
    "code_quality": {
        "name": "代码质量评估器",
        "description": "从可读性、效率和最佳实践三个维度评估代码质量，适用于代码生成场景",
        "tags": ["可读性", "效率", "最佳实践"],
        "prompt": (
            "请评估以下代码的质量。\n\n"
            "评分维度：\n"
            "1. 可读性（命名、结构、注释）\n"
            "2. 效率（时间/空间复杂度、是否有明显性能问题）\n"
            "3. 最佳实践（错误处理、边界条件、安全性）\n\n"
            "待评估代码：\n{{code}}\n\n"
            "评分要求：\n"
            "- 综合三个维度给出 0-1 的总分\n"
            "- 指出具体问题和改进建议\n\n"
            '请输出 JSON 格式：{"score": 分数, "reason": "评分理由，包含各维度分析"}'
        ),
        "variables": ["code"],
        "modelConfig": {"temperature": 0.2},
    },
    "sentiment_analysis": {
        "name": "情感分析评估器",
        "description": "分析文本的情感倾向，输出 -1（非常负面）到 1（非常正面）的情感分数",
        "tags": ["情感", "正负向", "舆情"],
        "prompt": (
            "请分析以下文本的情感倾向。\n\n"
            "评分标准：\n"
            "- 1.0：非常正面（赞美、满意、推荐）\n"
            "- 0.5：较为正面\n"
            "- 0.0：中性（客观陈述、无明显情感）\n"
            "- -0.5：较为负面\n"
            "- -1.0：非常负面（批评、愤怒、失望）\n\n"
            "待分析文本：{{text}}\n\n"
            '请输出 JSON 格式：{"score": 分数, "reason": "情感分析理由，'
            '引用文本中的关键情感词汇"}'
        ),
        "variables": ["text"],
        "modelConfig": {"temperature": 0.1},
    },
}


def list_evaluator_examples() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "name": data["name"],
            "description": data["description"],
            "tags": data["tags"],
        }
        for key, data in EVALUATOR_EXAMPLES.items()
    ]


async def create_evaluator_from_example(
    db: AsyncSession, key: str, user_id: int
) -> dict[str, Any]:
    """从示例创建评估器 + 首个版本 v1。"""
    from sqlalchemy import select

    from app.models.entities import EvalEvaluator
    from app.schemas.eval import EvalEvaluatorIn, EvalEvaluatorVersionIn
    from app.services.eval_evaluator_service import EvalEvaluatorService

    data = EVALUATOR_EXAMPLES.get(key)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"示例不存在：{key}"
        )

    svc = EvalEvaluatorService(db)
    base = data["name"]
    name = base
    rows = (
        await db.execute(
            select(EvalEvaluator.name).where(EvalEvaluator.name.like(f"{base}%"))
        )
    ).scalars().all()
    existing = {str(x) for x in rows}
    suffix = 2
    while name in existing:
        name = f"{base} ({suffix})"
        suffix += 1

    evaluator = await svc.create(
        EvalEvaluatorIn(
            name=name,
            description=data["description"],
            tags=",".join(data["tags"]),
        ),
        user_id,
    )
    # 内置模板的 modelConfig 没有 providerId —— 用户创建后自己选模型。
    # 这里直接建 v1 版本，让"点进评估器就能看到提示词"。
    await svc.create_version(
        EvalEvaluatorVersionIn(
            evaluator_id=evaluator.id,
            version="v1",
            prompt=data["prompt"],
            variables=json.dumps(data["variables"], ensure_ascii=False),
            modelConfig=json.dumps(data["modelConfig"], ensure_ascii=False),
        )
    )
    return evaluator.model_dump(by_alias=True, mode="json")


__all__ = [
    "DATASET_EXAMPLES",
    "EVALUATOR_EXAMPLES",
    "create_dataset_from_example",
    "create_evaluator_from_example",
    "list_dataset_examples",
    "list_evaluator_examples",
]
