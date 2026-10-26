"""一键回归：把已有的自检脚本按依赖顺序全跑一遍，最后汇总通过/失败。

为什么要有这个：自检脚本越写越多（tools / tools_ext / rag / workflow / hitl /
smoke / e2e），每次改完代码要记得跑哪几个全靠记忆。这个脚本把顺序固化下来。

注意：**e2e_chat_check 会真的花钱调模型**，所以默认不跑，加 --llm 才跑。

用法：
    python -m scripts.verify_all           # 只跑不花钱的
    python -m scripts.verify_all --llm     # 连端到端对话一起跑
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# (模块名, 说明, 是否要花钱)
SUITES: list[tuple[str, str, bool]] = [
    # 纯逻辑、不连库的先跑：它们失败时后面的集成测试没有意义
    ("scripts.tool_events_check", "tool_events 规则（截断/瘦身/字段名，离线）", False),
    ("scripts.tool_schema_check", "ToolCall 实体 vs DDL 列一致性（离线）", False),
    ("scripts.smoke_test", "ORM → schema → JSON 三层对齐", False),
    ("scripts.tools_check", "基础工具：schema 校验 + 执行 + 路径穿越防护", False),
    ("scripts.tools_ext_check", "扩展工具：execute_code / web_search / ocr（真跑）", False),
    ("scripts.rag_check", "RAG 检索链路 + 未向量化诊断", False),
    ("scripts.model_provider_check", "模型供应商 15 接口（序列化契约 + CRUD，不碰外网）", False),
    ("scripts.tools_api_check", "工具管理 9 接口（builtin/knowledge 保护 + JSONB tag 过滤）", False),
    ("scripts.mcp_api_check", "MCP 服务器 10 接口（7 实现 + 3 明确报错）", False),
    ("scripts.system_config_check", "系统配置 11 接口（providerId 精度 + upsert，会备份恢复）", False),
    ("scripts.agents_api_check", "Agent 管理 27 接口（绑定上限 + 软删过滤 + 克隆）", False),
    ("scripts.prompt_subagent_check", "Prompt 13 + SubAgent 16 接口（版本四步 / 双命名风格）", False),
    ("scripts.chat_ext_check", "反馈 5 + 会话扩展 6 + RAG 引用（toggle 三态 / 越权）", False),
    ("scripts.eval_check", "评测集 14 + 评估器 12 + 实验 9（分数归一 / 异步执行）", False),
    ("scripts.workflow_check", "工作流 DAG 执行", False),
    ("scripts.nodes_ext_check", "工作流 api/script/tool 节点 + 未知节点必报错", False),
    ("scripts.workflow_editor_check", "工作流编排 16 接口（校验规则 + 草稿发布 + SSE 调试）", False),
    ("scripts.hitl_check", "工作流 CONFIRM 挂起 / 恢复", False),
    ("scripts.knowledge_check", "知识库 CRUD 25 接口（成员/配置/示例问题/Advisor）", False),
    # ⚠️ 这一组会真的调 embedding API、真的写 MinIO（≈1min），放在不花钱组靠后
    ("scripts.knowledge_doc_check", "文档 18 接口（上传→分块→向量化→预览→删除级联）", False),
    ("scripts.knowledge_qa_check", "问答对 9 + OCR 1（出参口径 / 删除级联 / 检索两路）", False),
    ("scripts.skill_check", "Skill 20 接口（内置保护 / 路径穿越 / ZIP 顶层目录剥离）", False),
    ("scripts.misc_check", "零散 28 接口（枚举口径 / Dashboard 键名 / Key 明文 / 写回不抹列）", False),
    ("scripts.knowledge_eval_check", "RAG 评估 10 接口（JSONL 往返 / 异步任务 / 检索指标真跑）", False),
    ("scripts.task_check", "任务中心 8 接口（active 聚合筛选 / 61002 / 终态才可删）", False),
    ("scripts.auth_check", "认证扩展 8 接口（bcrypt 互通 / 头像框白名单 / 永久头像 URL）", False),
    ("scripts.admin_check", "管理员 6 接口（出参剔除 password / 自我保护 / 软删）", False),
    ("scripts.user_memory_check", "用户记忆 5 接口（三口径 / 自动关键词 / 15 条裁剪 / 向量直写）", False),
    ("scripts.token_budget_check", "Token 预算 5 接口（自然日切窗口 / 排行仅管理员 / 配置校验）", False),
    ("scripts.document_edit_check", "文档编辑 5 接口（乐观锁 60014 / 快照顺序 / 回滚先备份）", False),
    ("scripts.misc2_check", "零碎 9 接口（模型/todos 快照/附件边界）", False),
    ("scripts.session_files_check", "会话文件 4 接口（分区白名单 / 目录推导 / 顶级目录不可删）", False),
    ("scripts.agent_ext_check", "Agent 扩展 4 + 同步对话（publish 版本链 / 头像 URL / 404 防枚举）", False),
    ("scripts.auth_gate_check", "认证收口（401 门 / BCrypt 登录 / token 吊销 / Key 凭据）", False),
    ("scripts.builtin_check", "内置注册器（幂等 / 漂移恢复 / 重建 / SKILL.md 往返）", False),
    ("scripts.token_budget_counter_check", "预算计数器（INCR+TTL / 三道闸 / fail-open）", False),
    ("scripts.graph_check", "图谱 26 接口（假向量 / MERGE 幂等 / 社区检测真跑 / 标签隔离）", False),
    ("scripts.task_recovery_check", "任务持久化（僵尸收割 CAS / 心跳豁免 / 启动恢复重跑）", False),
    ("scripts.apikey_quota_check", "API Key 配额（原子扣减 / 跨天重置 / 限流 429 / 作用域 403）", False),
    ("scripts.tool_persist_check", "工具调用三处落库（tool_events / tool_calls / 详情接口）", True),
    ("scripts.e2e_chat_check", "端到端对话（真的调模型，花钱）", True),
]


def run_one(module: str, label: str, timeout: int = 420) -> tuple[bool, float]:
    print(f"\n{'=' * 70}\n▶ {label}  [{module}]\n{'=' * 70}", flush=True)
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        elapsed = time.time() - t0
        # 只打印尾部：每个自检脚本自己已经把细节打完了
        tail = "\n".join(proc.stdout.splitlines()[-12:])
        if tail:
            print(tail)
        if proc.returncode != 0:
            print("---- stderr 尾部 ----\n" + "\n".join(proc.stderr.splitlines()[-15:]))
        return proc.returncode == 0, elapsed
    except subprocess.TimeoutExpired:
        return False, time.time() - t0


def main() -> None:
    with_llm = "--llm" in sys.argv
    # 远程库（localhost）会瞬断：DNS 解析到废 IP 或 TCP 直接失败，
    # 脚本 2~3 秒就退出。这种情况报"回归失败"会让人去翻代码，白费时间。
    # 允许重试一次；真 bug 重试还是失败，不影响结论。
    retry = "--no-retry" not in sys.argv
    results: list[tuple[str, bool, float]] = []
    for module, label, cost in SUITES:
        if cost and not with_llm:
            print(f"\n- 跳过 {label}（加 --llm 才跑，会调模型花钱）")
            continue
        ok, elapsed = run_one(module, label)
        if not ok and retry:
            print(f"\n↻ {label} 首次失败，重试一次（远程库抖动？）")
            ok2, elapsed2 = run_one(module, label)
            if ok2:
                ok, elapsed = True, elapsed + elapsed2
                label += "（重试后通过）"
            else:
                elapsed += elapsed2
        results.append((label, ok, elapsed))

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}")
    bad = 0
    for label, ok, elapsed in results:
        flag = "✅" if ok else "❌"
        if not ok:
            bad += 1
        print(f"  {flag} {label:<46} {elapsed:6.1f}s")
    print(f"\n共 {len(results)} 项，失败 {bad} 项")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
