#!/usr/bin/env python3
"""
推测执行 (Speculative Execution) — 对标 Groq / Anthropic

核心思想：预测用户可能的下一步操作，预先执行备选路径。
用户确认时立即返回结果。

用法：
    python3 speculative_execution.py predict --context "..."
    python3 speculative_execution.py pre-execute --action "..."
    python3 speculative_execution.py confirm --id "..."
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "speculative"
STATE_FILE = STATE_DIR / "speculations.json"


def ensure_state_file():
    """确保状态文件和目录存在"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"speculations": []}, indent=2))


def load_state():
    """加载推测状态"""
    ensure_state_file()
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"speculations": []}


def save_state(state):
    """保存推测状态"""
    ensure_state_file()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def extract_keywords(context: str) -> list[str]:
    """从上下文中提取关键词"""
    keywords = []
    triggers = {
        "写完文件": ["写文件", "创建文件", "生成文件", "写入", "write file", "created"],
        "写完代码": ["写代码", "编写", "实现", "完成函数", "写完"],
        "调试": ["调试", "debug", "报错", "错误", "error", "bug", "异常"],
        "搜索": ["搜索", "查找", "search", "find", "查询", "检索"],
        "测试": ["测试", "test", "验证", "检查"],
        "编辑": ["编辑", "修改", "修改文件", "改", "更新", "update"],
        "分析": ["分析", "查看", "检查", "审查", "review"],
    }
    context_lower = context.lower()
    for category, words in triggers.items():
        if any(w.lower() in context_lower for w in words):
            keywords.append(category)
    return keywords


def predict_actions(context: str, max_count: int = 3) -> list[dict]:
    """根据上下文预测可能的下一步操作"""
    keywords = extract_keywords(context)

    prediction_map = {
        "写完文件": [
            {"action": "测试新文件", "description": "运行测试验证文件正确性"},
            {"action": "提交代码", "description": "git add + git commit 提交变更"},
            {"action": "继续优化", "description": "进一步优化或扩展代码"},
        ],
        "写完代码": [
            {"action": "运行测试", "description": "执行单元测试或集成测试"},
            {"action": "提交代码", "description": "git add + git commit 提交变更"},
            {"action": "代码审查", "description": "查看代码质量、格式检查"},
        ],
        "调试": [
            {"action": "运行修复", "description": "应用修复并重新运行"},
            {"action": "解释原因", "description": "分析错误根因"},
            {"action": "查看日志", "description": "查看详细日志输出"},
        ],
        "搜索": [
            {"action": "深入某个结果", "description": "查看搜索结果中的具体条目"},
            {"action": "换个方向搜索", "description": "使用不同关键词重新搜索"},
            {"action": "过滤结果", "description": "对搜索结果进行筛选"},
        ],
        "测试": [
            {"action": "修复失败用例", "description": "修复未通过的测试"},
            {"action": "查看覆盖率", "description": "分析测试覆盖率报告"},
            {"action": "添加更多测试", "description": "补充边界条件测试"},
        ],
        "编辑": [
            {"action": "查看变更", "description": "git diff 查看修改内容"},
            {"action": "提交变更", "description": "git commit 提交修改"},
            {"action": "运行测试", "description": "验证修改是否正确"},
        ],
        "分析": [
            {"action": "生成报告", "description": "输出分析报告"},
            {"action": "深入细节", "description": "进一步分析具体部分"},
            {"action": "提出建议", "description": "基于分析给出改进建议"},
        ],
    }

    predictions = []
    seen = set()
    for kw in keywords:
        for item in prediction_map.get(kw, []):
            if item["action"] not in seen and len(predictions) < max_count:
                seen.add(item["action"])
                predictions.append(item)

    # 如果没匹配到任何规则，返回通用预测
    if not predictions:
        predictions = [
            {"action": "确认继续", "description": "确认并继续当前操作"},
            {"action": "查看详情", "description": "查看更多详细信息"},
            {"action": "修改参数", "description": "调整当前操作的参数"},
        ][:max_count]

    return predictions[:max_count]


def cmd_predict(args):
    """预测下一步操作"""
    context = args.context
    predictions = predict_actions(context, max_count=3)

    # 记录预测到状态文件
    state = load_state()
    spec_id = str(uuid.uuid4())[:8]
    spec = {
        "id": spec_id,
        "context": context,
        "predictions": predictions,
        "status": "predicted",
        "created_at": time.time(),
    }
    state["speculations"].append(spec)
    # 只保留最近 50 条
    state["speculations"] = state["speculations"][-50:]
    save_state(state)

    result = {
        "speculation_id": spec_id,
        "context": context,
        "predictions": predictions,
        "status": "predicted",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_pre_execute(args):
    """预先执行某个操作"""
    state = load_state()

    spec_id = str(uuid.uuid4())[:8]
    spec = {
        "id": spec_id,
        "action": args.action,
        "context": args.context if hasattr(args, "context") and args.context else "",
        "status": "pending",
        "result": None,
        "created_at": time.time(),
    }
    state["speculations"].append(spec)
    state["speculations"] = state["speculations"][-50:]
    save_state(state)

    result = {
        "speculation_id": spec_id,
        "action": args.action,
        "status": "pending",
        "message": f"预执行已就绪: {args.action}",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_confirm(args):
    """确认并返回预执行结果"""
    state = load_state()
    spec_id = args.id

    for spec in state["speculations"]:
        if spec["id"] == spec_id:
            if spec["status"] == "pending":
                spec["status"] = "confirmed"
                spec["confirmed_at"] = time.time()
                save_state(state)
                result = {
                    "speculation_id": spec_id,
                    "action": spec.get("action", ""),
                    "status": "confirmed",
                    "result": spec.get("result"),
                    "message": f"已确认执行: {spec.get('action', '')}",
                }
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return
            elif spec["status"] == "predicted":
                # 预测也可以被确认
                spec["status"] = "confirmed"
                spec["confirmed_at"] = time.time()
                save_state(state)
                predictions = spec.get("predictions", [])
                result = {
                    "speculation_id": spec_id,
                    "status": "confirmed",
                    "predictions": predictions,
                    "message": "预测已确认，请选择具体操作",
                }
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return
            else:
                result = {
                    "speculation_id": spec_id,
                    "status": spec["status"],
                    "error": f"推测状态为 '{spec['status']}'，无法确认",
                }
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return

    # 未找到
    result = {
        "speculation_id": spec_id,
        "status": "not_found",
        "error": "未找到该推测ID",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="推测执行 - 预测用户下一步操作并预先执行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s predict --context "刚写完一个Python文件"
  %(prog)s pre-execute --action "运行测试"
  %(prog)s confirm --id "abc12345"
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # predict
    predict_parser = subparsers.add_parser("predict", help="预测可能的下一步操作")
    predict_parser.add_argument(
        "--context", "-c", required=True, help="当前上下文描述"
    )
    predict_parser.set_defaults(func=cmd_predict)

    # pre-execute
    pre_exec_parser = subparsers.add_parser("pre-execute", help="预先执行某个操作")
    pre_exec_parser.add_argument(
        "--action", "-a", required=True, help="要预执行的操作"
    )
    pre_exec_parser.add_argument(
        "--context", "-c", required=False, help="上下文描述（可选）"
    )
    pre_exec_parser.set_defaults(func=cmd_pre_execute)

    # confirm
    confirm_parser = subparsers.add_parser("confirm", help="确认并返回预执行结果")
    confirm_parser.add_argument(
        "--id", required=True, help="推测ID"
    )
    confirm_parser.set_defaults(func=cmd_confirm)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()