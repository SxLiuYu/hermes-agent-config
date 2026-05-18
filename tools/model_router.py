#!/usr/bin/env python3
"""
Smart Model Router v2 — 对标 OpenRouter / Martian

任务驱动的模型选择，根据任务特征自动路由到最优模型。
支持成本追踪和使用报告。

对标:
  - OpenRouter: 统一 API 网关，按成本/延迟/能力路由
  - Martian: 自动模型选择 + 成本优化
  - Martian Model Router: 将每个 query 路由到最佳 LLM

架构:
  ~/.hermes/model_router/
    config.json       — 模型定义和路由规则
  ~/.hermes/logs/
    model_usage.jsonl — 使用日志 (token/cost)

用法:
  python3 model_router.py select --task "..."
  python3 model_router.py track --model finna-deepseek --tokens 500
  python3 model_router.py report
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
ROUTER_DIR = HERMES_HOME / "model_router"
CONFIG_FILE = ROUTER_DIR / "config.json"
USAGE_LOG = HERMES_HOME / "logs" / "model_usage.jsonl"

# ── 模型定义 ──────────────────────────────────────────────────────────────

MODEL_DEFINITIONS = {
    "omlx-qwen3.5-4b": {
        "provider": "local",
        "description": "OMLX Qwen3.5 4B — 本地轻量模型",
        "cost_per_1m_tokens": 0.0,       # 免费
        "cost_per_1m_output": 0.0,
        "tasks": ["simple", "chat", "translation", "summarization"],
        "max_tokens": 8192,
        "strengths": ["zero cost", "low latency", "offline"],
        "limitations": ["limited reasoning", "no vision"],
    },
    "finna-deepseek-v3.1": {
        "provider": "FinnA Flash",
        "description": "FinnA DeepSeek V3.1 — 快速编码/调试",
        "cost_per_1m_tokens": 0.28,      # ~$0.28/1M tokens
        "cost_per_1m_output": 0.28,
        "tasks": ["coding", "debug", "refactor", "review"],
        "max_tokens": 65536,
        "strengths": ["code generation", "fast turnaround", "large context"],
        "limitations": ["moderate reasoning depth"],
    },
    "finna-deepseek-v4-pro": {
        "provider": "FinnA Pro",
        "description": "FinnA DeepSeek V4 Pro — 复杂推理",
        "cost_per_1m_tokens": 1.10,      # ~$1.10/1M tokens
        "cost_per_1m_output": 1.10,
        "tasks": ["complex_reasoning", "math", "analysis", "architecture"],
        "max_tokens": 131072,
        "strengths": ["deep reasoning", "complex analysis", "large context"],
        "limitations": ["higher cost", "slower"],
    },
    "finna-qwen3-vl-32b": {
        "provider": "FinnA",
        "description": "FinnA Qwen3 VL 32B — 视觉理解",
        "cost_per_1m_tokens": 0.50,      # ~$0.50/1M tokens
        "cost_per_1m_output": 0.50,
        "tasks": ["vision", "image_analysis", "ocr", "chart_reading"],
        "max_tokens": 32768,
        "strengths": ["vision understanding", "multimodal"],
        "limitations": ["vision-only tasks", "no code generation"],
    },
    "finna-kimi-k2": {
        "provider": "FinnA",
        "description": "FinnA Kimi K2 — 深度研究",
        "cost_per_1m_tokens": 0.50,      # ~$0.50/1M tokens
        "cost_per_1m_output": 0.50,
        "tasks": ["research", "long_context", "document_analysis", "knowledge_synthesis"],
        "max_tokens": 131072,
        "strengths": ["long context", "deep research", "knowledge synthesis"],
        "limitations": ["not for real-time tasks"],
    },
}

# ── 任务→模型路由规则 ────────────────────────────────────────────────────

TASK_ROUTING = {
    "simple":         "omlx-qwen3.5-4b",
    "chat":           "omlx-qwen3.5-4b",
    "translation":    "omlx-qwen3.5-4b",
    "summarization":  "omlx-qwen3.5-4b",
    "coding":         "finna-deepseek-v3.1",
    "debug":          "finna-deepseek-v3.1",
    "refactor":       "finna-deepseek-v3.1",
    "review":         "finna-deepseek-v3.1",
    "complex_reasoning": "finna-deepseek-v4-pro",
    "complex":        "finna-deepseek-v4-pro",
    "math":           "finna-deepseek-v4-pro",
    "analysis":       "finna-deepseek-v4-pro",
    "architecture":   "finna-deepseek-v4-pro",
    "vision":         "finna-qwen3-vl-32b",
    "image_analysis": "finna-qwen3-vl-32b",
    "ocr":            "finna-qwen3-vl-32b",
    "chart":          "finna-qwen3-vl-32b",
    "research":       "finna-kimi-k2",
    "long_context":   "finna-kimi-k2",
    "document":       "finna-kimi-k2",
    "knowledge":      "finna-kimi-k2",
}


# ── 工具函数 ──────────────────────────────────────────────────────────────

def ensure_dirs():
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    default = {
        "routing": TASK_ROUTING,
        "models": MODEL_DEFINITIONS,
        "default_model": "finna-deepseek-v4-pro",
        "cost_savings": {
            "total_tokens_routed": 0,
            "total_cost": 0.0,
            "estimated_savings": 0.0,
        },
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
            # merge with defaults (defaults win for model defs)
            for k, v in default.items():
                if k not in loaded:
                    loaded[k] = v
            loaded["models"] = MODEL_DEFINITIONS  # always use latest definitions
            return loaded
        except (json.JSONDecodeError, IOError):
            pass
    return default


def save_config(config: dict):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def identify_task(user_input: str) -> str:
    """根据用户输入推断任务类型。"""
    text = user_input.lower()
    keywords = {
        "simple":         ["hello", "hi", "thanks", "what is your name", "简单", "聊天"],
        "coding":         ["write code", "implement", "function", "class", "api", "写代码", "实现"],
        "debug":          ["debug", "fix", "error", "bug", "修复", "报错", "调试"],
        "refactor":       ["refactor", "clean up", "restructure", "重构", "整理"],
        "review":         ["review", "code review", "检查", "审查"],
        "complex_reasoning": ["complex", "reasoning", "prove", "math", "logic", "复杂推理", "证明"],
        "math":           ["math", "equation", "calculate", "数学", "计算"],
        "analysis":       ["analyze", "analysis", "evaluate", "分析", "评估"],
        "architecture":   ["architecture", "design", "system design", "架构", "设计"],
        "vision":         ["image", "picture", "photo", "screenshot", "图片", "图像", "照片"],
        "research":       ["research", "study", "survey", "paper", "研究", "论文", "调查"],
        "summarization":  ["summarize", "summary", "tldr", "总结", "摘要"],
        "translation":    ["translate", "翻译", "译"],
    }
    for task, kws in keywords.items():
        for kw in kws:
            if kw in text:
                return task
    return "simple"


def select_model(task: str) -> dict:
    """根据任务选择最佳模型，返回 {model, provider, estimated_cost, ...}"""
    config = load_config()
    task_lower = task.strip().lower()

    # 先尝试精确匹配
    model_name = TASK_ROUTING.get(task_lower)

    # 再尝试关键词推断
    if model_name is None:
        inferred = identify_task(task_lower)
        model_name = TASK_ROUTING.get(inferred, config.get("default_model", "finna-deepseek-v4-pro"))

    model_def = MODEL_DEFINITIONS.get(model_name)
    if not model_def:
        model_name = config.get("default_model", "finna-deepseek-v4-pro")
        model_def = MODEL_DEFINITIONS[model_name]

    return {
        "model": model_name,
        "provider": model_def["provider"],
        "description": model_def["description"],
        "estimated_cost_per_1m": model_def["cost_per_1m_tokens"],
        "max_tokens": model_def["max_tokens"],
        "strengths": model_def["strengths"],
        "task": task,
    }


def track_usage(model: str, tokens: int, metadata: dict = None) -> dict:
    """记录使用情况到日志。"""
    ensure_dirs()
    model_def = MODEL_DEFINITIONS.get(model, {})
    cost_per_1m = model_def.get("cost_per_1m_tokens", 0)
    cost = (tokens / 1_000_000) * cost_per_1m

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tokens": tokens,
        "cost": round(cost, 6),
        "metadata": metadata or {},
    }

    with open(USAGE_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 更新配置中的累计数据
    config = load_config()
    config["cost_savings"]["total_tokens_routed"] += tokens
    config["cost_savings"]["total_cost"] += cost
    # 估算节省: 如果不用本地模型，都用 FinnA Pro 的价格
    if model != "omlx-qwen3.5-4b":
        pro_cost = (tokens / 1_000_000) * 1.10
        config["cost_savings"]["estimated_savings"] += (pro_cost - cost)
    save_config(config)

    return {
        "status": "logged",
        "model": model,
        "tokens": tokens,
        "cost": round(cost, 6),
        "total_cost_accumulated": round(config["cost_savings"]["total_cost"], 6),
    }


def generate_report() -> dict:
    """生成使用报告。"""
    config = load_config()

    # 从日志读取详细数据
    usage_by_model = {}
    total_tokens = 0
    total_cost = 0.0
    entry_count = 0

    if USAGE_LOG.exists():
        with open(USAGE_LOG, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    model = entry.get("model", "unknown")
                    tokens = entry.get("tokens", 0)
                    cost = entry.get("cost", 0)

                    if model not in usage_by_model:
                        usage_by_model[model] = {"tokens": 0, "cost": 0.0, "calls": 0}
                    usage_by_model[model]["tokens"] += tokens
                    usage_by_model[model]["cost"] += cost
                    usage_by_model[model]["calls"] += 1

                    total_tokens += tokens
                    total_cost += cost
                    entry_count += 1
                except json.JSONDecodeError:
                    pass

    # 每个模型的详细信息
    model_details = {}
    for name, defn in MODEL_DEFINITIONS.items():
        stats = usage_by_model.get(name, {"tokens": 0, "cost": 0.0, "calls": 0})
        model_details[name] = {
            "provider": defn["provider"],
            "calls": stats["calls"],
            "tokens": stats["tokens"],
            "cost": round(stats["cost"], 6),
            "cost_per_1m": defn["cost_per_1m_tokens"],
        }

    return {
        "summary": {
            "total_calls": entry_count,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "estimated_savings": round(config["cost_savings"].get("estimated_savings", 0), 6),
        },
        "by_model": model_details,
        "routing_rules": {
            task: model for task, model in sorted(TASK_ROUTING.items())
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Smart Model Router v2 — 任务驱动的模型选择与成本追踪"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # select
    sel = sub.add_parser("select", help="根据任务选择最优模型")
    sel.add_argument("--task", required=True, help="任务描述 (如 coding, debug, research)")
    sel.add_argument("--list-models", action="store_true", help="列出所有可用模型")

    # track
    trk = sub.add_parser("track", help="记录使用情况")
    trk.add_argument("--model", required=True, help="模型名称")
    trk.add_argument("--tokens", type=int, required=True, help="消耗的 token 数")
    trk.add_argument("--meta", default="{}", help="额外元数据 (JSON)")

    # report
    sub.add_parser("report", help="生成使用报告")

    args = parser.parse_args()

    if args.command == "select":
        if args.list_models or getattr(args, "list_models", False):
            result = {}
            for name, defn in MODEL_DEFINITIONS.items():
                result[name] = {
                    "provider": defn["provider"],
                    "cost_per_1m": defn["cost_per_1m_tokens"],
                    "tasks": defn["tasks"],
                    "max_tokens": defn["max_tokens"],
                }
        else:
            result = select_model(args.task)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "track":
        try:
            meta = json.loads(args.meta)
        except json.JSONDecodeError:
            meta = {}
        result = track_usage(args.model, args.tokens, meta)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "report":
        report = generate_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()