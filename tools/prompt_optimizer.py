#!/usr/bin/env python3
"""
P0-17: Prompt Optimizer — DSPy MIPROv2-inspired auto-optimization

对标:
  - DSPy MIPROv2 (Stanford NLP, ICLR'24): 贝叶斯搜索指令 + 示例联合优化
  - DSPy Bootstrap: 自动从端到端标注生成中间步骤的 few-shot 示例

核心思想: "Program, don't prompt" — 定义输入输出签名，自动生成最优 prompt

适应 Hermes 的简化版:
  1. 指令候选生成 — 多种风格（简洁/创意/角色扮演/高风险场景）
  2. 评估指标 — 工具选择准确率、任务完成度、输出格式合规率
  3. 贝叶斯搜索 — 加权评分 + 自动选择最优组合
  4. A/B 测试 — 并行测试两个版本，数据驱动选优

使用场景:
  - 优化 tool description（让 Agent 更准确选择工具）
  - 优化 skill instruction（让 Agent 更精确遵循指令）
  - 优化 system prompt 片段
"""

import json
import os
import re
import time
import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Optional, Callable
from collections import defaultdict

# ─── 指令生成策略 ────────────────────────────────────────

INSTRUCTION_STYLES = {
    "simple": "保持指令清晰简洁，去除所有冗余表述。",
    "creative": "用创意性的语言表达，可以适当使用比喻。",
    "description": "确保指令信息丰富且描述性强。",
    "high_stakes": "在指令中包含高风险场景，强调错误后果。",
    "persona": "包含与任务相关的角色设定。",
    "step_by_step": "将指令分解为清晰的步骤，逐步引导。",
    "examples_first": "先给出具体示例，再给出通用规则。",
    "concise_bullet": "用项目符号列表，去除完整句式。",
}


def generate_instruction_candidates(
    original: str,
    task_context: str = "",
    num_candidates: int = 5,
) -> list[dict]:
    """
    生成多个指令变体，每个变体采用不同风格。

    Args:
        original: 原始指令/描述
        task_context: 任务场景描述（帮助生成更相关的候选）
        num_candidates: 生成的候选数量

    Returns: [{style, instruction, rationale}, ...]
    """
    # 选择风格组合
    styles = list(INSTRUCTION_STYLES.items())
    # 确保多样性：优先选择不同类型的风格
    selected = []
    used_types = set()

    for name, guidance in styles:
        # 简单分类
        if "简洁" in guidance or "simple" in name or "bullet" in name:
            stype = "concise"
        elif "创意" in guidance or "比喻" in guidance:
            stype = "creative"
        elif "角色" in guidance or "persona" in name:
            stype = "persona"
        elif "步骤" in guidance or "step" in name:
            stype = "structured"
        elif "示例" in guidance or "example" in name:
            stype = "example_driven"
        elif "风险" in guidance or "stakes" in name:
            stype = "risk_aware"
        else:
            stype = "descriptive"

        if stype not in used_types:
            selected.append((name, guidance))
            used_types.add(stype)

        if len(selected) >= num_candidates:
            break

    # 用规则变换生成每个候选（代替 LLM 调用，更快更可预测）
    candidates = []
    for style_name, guidance in selected:
        variant = _apply_style_transform(original, style_name, guidance, task_context)
        candidates.append({
            "style": style_name,
            "instruction": variant,
            "guidance": guidance,
        })

    # 始终包含原始版本作为 baseline
    candidates.insert(0, {
        "style": "original",
        "instruction": original,
        "guidance": "原始版本",
    })

    return candidates


def _apply_style_transform(
    text: str, style: str, guidance: str, context: str
) -> str:
    """应用风格变换规则"""

    if style == "simple":
        # 去除修饰词，精简为最核心信息
        result = text
        # 去除冗余开头
        result = re.sub(
            r"^(请|You should|You must|Make sure to|Remember to)\s*", "", result
        )
        # 去除冗余短语（含标点）
        result = re.sub(
            r"[。；，,\s]*(请注意|需要说明的是|重要的是|Note that|Important:)\s*[，,]?\s*", "。", result
        )
        result = re.sub(
            r"[。；，,\s]*(值得一提的是|众所周知|当然|显而易见|总的来说|顾名思义)\s*[，,]?\s*", "。", result
        )
        # 合并多余标点和空格
        result = re.sub(r"[。，,]{2,}", "。", result)
        result = re.sub(r"。\s*。", "。", result)
        result = re.sub(r"，\s*。", "。", result)
        result = re.sub(r"。\s*，", "。", result)
        # 缩短长句
        result = re.sub(r"；\s*", "。", result)
        result = re.sub(r"，\s*(并且|而且|同时)", "。", result)
        # 清理开头句号
        result = re.sub(r"^[。，,\s]+", "", result)
        # 保持一行
        result = re.sub(r"\n\s*\n", "\n", result)
        return result.strip()

    elif style == "concise_bullet":
        # 拆解为要点列表
        sentences = re.split(r"[。；\n]", text)
        bullets = []
        for s in sentences:
            s = s.strip()
            if len(s) > 5:
                # 提取核心动词
                s = re.sub(r"^(请|需要|应该|必须|可以|You should|You must)", "", s).strip()
                bullets.append(f"- {s}")
        return "\n".join(bullets[:8])

    elif style == "step_by_step":
        # 分解为步骤
        sentences = re.split(r"[。；\n]", text)
        steps = []
        for i, s in enumerate(sentences, 1):
            s = s.strip()
            if len(s) > 5:
                s = re.sub(r"^(请|需要|应该|必须|可以)", "", s).strip()
                steps.append(f"步骤 {i}: {s}")
        return "\n".join(steps[:8])

    elif style == "high_stakes":
        # 添加严重后果提醒
        prefix = (
            "⚠️ 关键提示：以下操作可能产生严重后果，请严格遵循：\n\n"
            if "关键" not in text
            else ""
        )
        return prefix + text

    elif style == "persona":
        # 如果有上下文，生成角色设定
        if "code" in context.lower() or "代码" in context:
            prefix = "你是一位资深软件工程师，在代码审查和调试方面有 15 年经验。\n\n"
        elif "data" in context.lower() or "数据" in context:
            prefix = "你是一位数据分析专家，擅长从复杂数据中提取洞察。\n\n"
        elif "deploy" in context.lower() or "部署" in context:
            prefix = "你是一位 DevOps 架构师，负责确保系统稳定可靠。\n\n"
        else:
            prefix = "你是一位经验丰富的 AI 助手，拥有广泛的知识和技能。\n\n"
        return prefix + text

    elif style == "examples_first":
        # 先给示例（这里简化，后续 P0-18 会生成真正示例）
        return text  # placeholder, will be enhanced by P0-18

    elif style == "creative":
        # 保持原文但增加一句引导
        return text  # 创意版本需要 LLM，这里保留原文

    elif style == "description":
        # 丰富描述信息
        return text  # 描述版需要 LLM，这里保留原文

    return text


# ─── 评估指标 ────────────────────────────────────────────


@dataclass
class EvalMetric:
    """评估指标定义"""
    name: str
    weight: float = 1.0
    description: str = ""


DEFAULT_METRICS = [
    EvalMetric("clarity", 0.25, "指令清晰度 — 是否一目了然"),
    EvalMetric("completeness", 0.20, "信息完整度 — 是否包含所有关键信息"),
    EvalMetric("conciseness", 0.20, "简洁度 — 无冗余表述"),
    EvalMetric("actionability", 0.20, "可执行性 — 是否给出了明确的操作指引"),
    EvalMetric("safety", 0.15, "安全性 — 是否包含必要的安全提醒"),
]


def _score_clarity(text: str) -> float:
    """评估清晰度"""
    score = 7.0  # baseline
    # 加分项
    if len(text) < 300:
        score += 1.0
    if "步骤" in text or "step" in text.lower():
        score += 0.5
    if "例如" in text or "示例" in text or "example" in text.lower():
        score += 0.5
    # 减分项
    if len(text) > 1000:
        score -= 2.0
    if text.count("\n") < 1 and len(text) > 200:
        score -= 1.0  # 长段落不易读
    return min(10, max(0, score))


# 噪声词汇列表（不参与完整性计算）
_NOISE_WORDS = {
    "请注意", "需要说明", "值得一提", "总的来说", "顾名思义",
    "众所周知", "当然", "显而易见", "需要强调", "特别说明",
    "需要注意", "需要了解", "重要的是", "需要知道", "首先",
    "其次", "最后", "另外", "此外", "同时", "并且",
    "因此", "所以", "不过", "然而", "但是",
    "please", "note", "important", "worth", "mentioning",
}

def _extract_substantive_keywords(text: str) -> set:
    """提取实质性关键词（过滤噪声词汇）"""
    words = set(re.findall(r"[\u4e00-\u9fff]{2,}", text.lower()))
    # 过滤噪声
    return {w for w in words if w not in _NOISE_WORDS}


def _score_completeness(text: str, original: str) -> float:
    """评估信息完整度（对比原始版本，过滤噪声词汇）"""
    orig_keywords = _extract_substantive_keywords(original)
    new_keywords = _extract_substantive_keywords(text)

    if not orig_keywords:
        return 7.0

    retention = len(orig_keywords & new_keywords) / max(len(orig_keywords), 1)
    return retention * 10


def _score_conciseness(text: str) -> float:
    """评估简洁度"""
    score = 7.0
    # 检测冗余模式
    redundant = [
        r"请注意", r"需要说明的是", r"值得注意的是",
        r"Please note", r"It is important to",
    ]
    for pat in redundant:
        if re.search(pat, text):
            score -= 0.5

    # 字符效率
    if len(text) < 100:
        score += 1.0
    elif len(text) < 300:
        score += 0.5
    elif len(text) > 800:
        score -= 2.0

    return min(10, max(0, score))


def _score_actionability(text: str) -> float:
    """评估可执行性"""
    score = 5.0
    # 检测动作导向词汇
    action_words = [
        "调用", "使用", "执行", "运行", "检查", "读取", "写入",
        "use", "call", "run", "check", "read", "write", "execute",
        "返回", "输出", "return", "output",
    ]
    for word in action_words:
        if word in text.lower():
            score += 0.3

    # 步骤化
    if re.search(r"(步骤|Step)\s*\d", text):
        score += 1.5

    return min(10, max(0, score))


def _score_safety(text: str) -> float:
    """评估安全性"""
    score = 5.0
    safety_words = [
        "注意", "警告", "小心", "确认", "危险",
        "warning", "caution", "danger", "confirm",
        "不要", "避免", "禁止", "never", "avoid", "don't",
    ]
    for word in safety_words:
        if word in text.lower():
            score += 0.5

    # [CRITICAL] 标记加分
    if re.search(r"\[CRITICAL\]|\[!IMPORTANT\]|\[WARNING\]", text):
        score += 1.0

    return min(10, max(0, score))


def evaluate_candidate(
    candidate: dict, original: str, metrics: list[EvalMetric] = None
) -> dict:
    """
    评估一个候选指令。

    Returns: {total_score, metric_scores, ...}
    """
    if metrics is None:
        metrics = DEFAULT_METRICS

    text = candidate["instruction"]
    scores = {}

    for metric in metrics:
        if metric.name == "clarity":
            scores[metric.name] = _score_clarity(text)
        elif metric.name == "completeness":
            scores[metric.name] = _score_completeness(text, original)
        elif metric.name == "conciseness":
            scores[metric.name] = _score_conciseness(text)
        elif metric.name == "actionability":
            scores[metric.name] = _score_actionability(text)
        elif metric.name == "safety":
            scores[metric.name] = _score_safety(text)

    # 加权总分
    total = sum(
        scores.get(m.name, 0) * m.weight for m in metrics
    ) / max(sum(m.weight for m in metrics), 0.001)

    return {
        "total_score": round(total, 2),
        "metric_scores": scores,
        "style": candidate["style"],
        "instruction_preview": text[:100],
    }


# ─── 贝叶斯搜索 ───────────────────────────────────────────


def bayesian_select(
    candidates: list[dict],
    original: str,
    metrics: list[EvalMetric] = None,
    top_k: int = 3,
) -> list[dict]:
    """
    贝叶斯风格的选择：评估所有候选，加权选择最优 top_k。

    返回按评分降序排列的 top_k 候选。
    """
    scored = []
    for c in candidates:
        result = evaluate_candidate(c, original, metrics)
        scored.append({**c, **result})

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    return scored[:top_k]


# ─── A/B 测试框架 ────────────────────────────────────────


@dataclass
class ABTest:
    """A/B 测试定义"""
    test_id: str
    variant_a: str  # 变体 A 的指令
    variant_b: str  # 变体 B 的指令
    task_type: str  # 测试场景
    runs_per_variant: int = 3
    results: list[dict] = field(default_factory=list)

    def record(self, variant: str, success: bool, score: float, notes: str = ""):
        self.results.append({
            "variant": variant,
            "success": success,
            "score": score,
            "notes": notes,
            "timestamp": time.time(),
        })

    def winner(self) -> Optional[str]:
        """判断哪个变体更好"""
        a_results = [r for r in self.results if r["variant"] == "A"]
        b_results = [r for r in self.results if r["variant"] == "B"]

        if len(a_results) < 2 or len(b_results) < 2:
            return None

        a_success = sum(1 for r in a_results if r["success"]) / len(a_results)
        b_success = sum(1 for r in b_results if r["success"]) / len(b_results)

        a_avg_score = sum(r["score"] for r in a_results) / len(a_results)
        b_avg_score = sum(r["score"] for r in b_results) / len(b_results)

        # 如果差距 > 10% 且有统计显著性（此处简化）
        if b_success - a_success > 0.1 or b_avg_score - a_avg_score > 0.5:
            return "B"
        elif a_success - b_success > 0.1 or a_avg_score - b_avg_score > 0.5:
            return "A"

        return "tie"  # 无显著差异


# ─── 优化流水线 ───────────────────────────────────────────


def optimize_instruction(
    original: str,
    task_context: str = "",
    num_candidates: int = 5,
    top_k: int = 2,
    metrics: list[EvalMetric] = None,
) -> dict:
    """
    完整优化流水线:
    1. 生成 N 个指令候选
    2. 多维度评估
    3. 贝叶斯选择最优 top_k

    Returns: {original, candidates, top_results, recommendation}
    """
    # Step 1: 生成候选
    candidates = generate_instruction_candidates(
        original, task_context, num_candidates
    )

    # Step 2: 评估 + 选择
    top_results = bayesian_select(candidates, original, metrics, top_k)

    # Step 3: 生成推荐
    if top_results:
        best = top_results[0]
        improvement = (
            f"评分 {best['total_score']}/10 (风格: {best['style']})"
        )
        recommendation = {
            "best_instruction": best["instruction"],
            "best_score": best["total_score"],
            "best_style": best["style"],
            "metric_scores": best["metric_scores"],
            "all_candidates": candidates,
            "top_candidates": top_results,
            "improvement_summary": improvement,
        }
    else:
        recommendation = {
            "best_instruction": original,
            "best_score": 0,
            "best_style": "original",
            "improvement_summary": "无法生成有效候选",
        }

    return recommendation


def optimize_tool_schema(
    schema: dict, task_context: str = ""
) -> dict:
    """
    优化单个工具 schema 的 description。
    """
    if "description" not in schema or not schema["description"]:
        return schema

    original_desc = schema["description"]
    task_ctx = task_context or schema.get("name", "")

    result = optimize_instruction(
        original_desc,
        task_context=f"tool:{task_ctx}",
        num_candidates=4,
        top_k=2,
    )

    optimized = schema.copy()
    optimized["description"] = result["best_instruction"]
    optimized["_optimization"] = {
        "original_description": original_desc,
        "score": result["best_score"],
        "style": result["best_style"],
    }

    return optimized


def optimize_skill_instruction(
    skill_content: str, task_context: str = ""
) -> dict:
    """
    优化 skill 中的关键指令段落。
    识别 SKILL.md 中的主要指令块并分别优化。
    """
    # 分割为段落
    paragraphs = skill_content.split("\n\n")
    optimized_paragraphs = []
    total_score = 0
    optimized_count = 0

    for para in paragraphs:
        para = para.strip()
        # 只优化有实质内容的段落（>50 字符，包含中文或英文指令）
        if len(para) > 50 and (
            re.search(r"[\u4e00-\u9fff]", para)
            or re.search(r"\b(use|call|run|check|must|should|always)\b", para.lower())
        ):
            result = optimize_instruction(
                para, task_context, num_candidates=3, top_k=1
            )
            optimized_paragraphs.append(result["best_instruction"])
            total_score += result["best_score"]
            optimized_count += 1
        else:
            optimized_paragraphs.append(para)

    avg_score = total_score / max(optimized_count, 1)

    return {
        "optimized_content": "\n\n".join(optimized_paragraphs),
        "avg_score": round(avg_score, 2),
        "optimized_paragraphs": optimized_count,
        "total_paragraphs": len(paragraphs),
    }


# ─── 批量优化 ─────────────────────────────────────────────


def batch_optimize_tools(
    tool_schemas: list[dict],
    task_context: str = "",
) -> list[dict]:
    """批量优化所有工具 schema"""
    results = []
    for schema in tool_schemas:
        optimized = optimize_tool_schema(schema, task_context)
        results.append(optimized)
    return results


# ─── 报告生成 ─────────────────────────────────────────────


def generate_report(recommendation: dict) -> str:
    """生成优化报告"""
    lines = [
        "## 提示优化报告",
        f"最优评分: {recommendation.get('best_score', 0)}/10",
        f"最优风格: {recommendation.get('best_style', 'original')}",
        "",
        "### 各维度评分",
    ]

    scores = recommendation.get("metric_scores", {})
    for metric, score in scores.items():
        bar = "█" * int(score) + "░" * (10 - int(score))
        lines.append(f"- {metric}: {bar} {score}/10")

    lines.extend([
        "",
        "### 最优指令",
        "```",
        recommendation.get("best_instruction", "")[:500],
        "```",
        "",
        "### 候选对比",
    ])

    for c in recommendation.get("top_candidates", []):
        lines.append(
            f"- [{c.get('style', '?')}] 评分 {c.get('total_score', 0)}: "
            f"{c.get('instruction', '')[:80]}..."
        )

    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  prompt_optimizer.py optimize <text> [context]")
        print("  prompt_optimizer.py optimize-tool <tool_schema.json>")
        print("  prompt_optimizer.py optimize-skill <skill.md> [context]")
        print("  prompt_optimizer.py candidates <text> [context]")
        print("  prompt_optimizer.py evaluate <text> [original]")
        print("  prompt_optimizer.py report <text> [context]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "optimize":
        text = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read().strip()
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        result = optimize_instruction(text, context)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "optimize-tool":
        if len(sys.argv) > 2:
            with open(sys.argv[2]) as f:
                schema = json.load(f)
        else:
            schema = json.load(sys.stdin)
        result = optimize_tool_schema(schema)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "optimize-skill":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        if path:
            with open(path) as f:
                content = f.read()
        else:
            content = sys.stdin.read()
        result = optimize_skill_instruction(content, context)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "candidates":
        text = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read().strip()
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        candidates = generate_instruction_candidates(text, context)
        for c in candidates:
            print(f"\n[{c['style']}] (guidance: {c['guidance'][:50]}...)")
            print(c["instruction"][:150])

    elif cmd == "evaluate":
        text = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read().strip()
        original = sys.argv[3] if len(sys.argv) > 3 else text
        result = evaluate_candidate(
            {"instruction": text, "style": "manual"}, original
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "report":
        text = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read().strip()
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        result = optimize_instruction(text, context)
        print(generate_report(result))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)