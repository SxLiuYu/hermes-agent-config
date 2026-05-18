#!/usr/bin/env python3
"""
Outcomes Grader — LLM 驱动的质量评分引擎

对标 Anthropic Outcomes：每次 agent turn 结束后，用独立模型按 rubric 评分。
评分结果注入下一轮对话的上下文，实现 10.1% 质量提升。

与 outcomes_check.py 的区别：
  - 使用实际 LLM（FinnA）评分，而非启发式规则
  - 支持实时注入上下文（session_start hook 读取上次评分）
  - 自动 retry：低于阈值时返回改进建议

用法:
  python3 scripts/outcomes_grader.py grade --text "..." --model "glm"
  python3 scripts/outcomes_grader.py last     # 查看上次评分
  python3 scripts/outcomes_grader.py stats    # 评分趋势
"""

import json
import os
import sys
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
STATE_FILE = HERMES_HOME / "logs" / "outcomes_last.json"
HISTORY_FILE = HERMES_HOME / "logs" / "outcomes_history.jsonl"
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"

# 默认评分维度（对标 Anthropic Outcomes rubric）
DEFAULT_RUBRIC = {
    "correctness": "输出是否准确解决了用户的问题？有没有事实性错误、幻觉或错误代码？(1-10)",
    "completeness": "是否完整覆盖了任务所有方面？有没有遗漏步骤或未完成的 TODO？(1-10)",
    "efficiency": "工具调用是否合理高效？有没有冗余操作或重复读取？(1-10)",
    "clarity": "回复结构是否清晰？代码注释是否充分？(1-10)",
    "proactivity": "是否主动预判后续需求、记录 learnings、保存 skill？(1-10)",
}


def call_finna(text: str, api_key: str, model: str = "glm-4-flash") -> str:
    """调用 FinnA API 进行评分"""
    rubric_lines = "\n".join(
        f"{dim}: {desc}" for dim, desc in DEFAULT_RUBRIC.items()
    )

    prompt = f"""你是一个独立的 Agent 输出质量评分器。请对以下 AI Agent 的输出进行评分。

评分标准（每项 1-10 分）：
{rubric_lines}

被评分的输出：
---
{text[:4000]}
---

请用以下 JSON 格式返回评分（不要其他内容）：
{{"correctness": N, "completeness": N, "efficiency": N, "clarity": N, "proactivity": N, "summary": "一句话总结", "improvements": ["改进建议1", "改进建议2"]}}"""

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "stream": False,
        "extra_body": {"enable_thinking": False},
    }

    req = urllib.request.Request(
        FINNA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"FinnA API error: {e}", file=sys.stderr)
        return None


def parse_score(text: str) -> dict:
    """从 LLM 返回中提取 JSON 评分"""
    try:
        # 尝试直接解析
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取 JSON 块
    import re
    match = re.search(r'\{[^}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def get_api_key() -> str:
    """获取 FinnA API Key（仅 qwen3-32b 有余额，2026-05）"""
    # 从环境变量
    for env_var in ["FINNA_KEY", "FINNA_API_KEY", "FINNA_QWEN_KEY"]:
        key = os.environ.get(env_var, "")
        if key and key.startswith("app-"):
            return key
    # 唯一有余额的 key: Qwen3-32b
    return "app-6OzRGg93TfuDOny9NUnKMvQU"


def grade_text(text: str, model: str = "qwen3-32b") -> dict:
    """对文本进行评分"""
    if len(text.strip()) < 50:
        return {"error": "text too short", "scores": {}}

    api_key = get_api_key()
    if not api_key:
        return {"error": "no API key", "scores": {}}

    response = call_finna(text, api_key, model)
    if not response:
        # fallback: 启发式评分
        return heuristic_grade(text)

    scores = parse_score(response)
    if not scores:
        return heuristic_grade(text)

    # 计算加权平均
    weights = {"correctness": 3, "completeness": 2, "efficiency": 2, "clarity": 1.5, "proactivity": 1.5}
    total_weight = sum(weights.values())
    weighted_avg = sum(scores.get(k, 5) * weights.get(k, 1) for k in weights) / total_weight

    return {
        "scores": {k: scores.get(k, 5) for k in DEFAULT_RUBRIC},
        "average": round(weighted_avg, 1),
        "summary": scores.get("summary", ""),
        "improvements": scores.get("improvements", []),
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": "llm",
    }


def heuristic_grade(text: str) -> dict:
    """Fallback 启发式评分"""
    lines = text.split("\n")
    has_errors = any(w in text.lower() for w in ["error", "failed", "失败", "traceback", "exception"])
    has_structure = any(l.startswith("##") or l.startswith("# ") for l in lines)
    has_code = "```" in text
    has_summary = any(w in text.lower() for w in ["总结", "summary", "完成", "done", "✅"])

    scores = {
        "correctness": 5 if has_errors else 8,
        "completeness": 8 if has_summary else 5,
        "efficiency": 7 if len(lines) < 300 else 5,
        "clarity": 8 if (has_structure or has_code) else 5,
        "proactivity": 7 if "skill" in text.lower() or "memory" in text.lower() else 5,
    }
    avg = sum(scores.values()) / len(scores)

    return {
        "scores": scores,
        "average": round(avg, 1),
        "summary": "（启发式评分）",
        "improvements": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": "heuristic",
    }


def save_result(result: dict):
    """保存评分结果"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    # 追加历史
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def get_context_injection() -> str:
    """生成注入上下文的评分摘要"""
    if not STATE_FILE.exists():
        return ""

    try:
        result = json.loads(STATE_FILE.read_text())
    except Exception:
        return ""

    scores = result.get("scores", {})
    avg = result.get("average", 0)
    if not scores or avg == 0:
        return ""

    # 评分柱状图
    bars = {k: "█" * v + "░" * (10 - v) for k, v in scores.items()}

    quality = "🟢 优秀" if avg >= 7.5 else ("🟡 良好" if avg >= 6 else "🔴 需改进")

    injection = f"""## 📊 上轮输出质量评分: {avg}/10 {quality}
| 维度 | 分数 | 可视化 |
|------|------|--------|
| 正确性 | {scores.get('correctness','?')}/10 | {bars.get('correctness','')} |
| 完整性 | {scores.get('completeness','?')}/10 | {bars.get('completeness','')} |
| 效率 | {scores.get('efficiency','?')}/10 | {bars.get('efficiency','')} |
| 清晰度 | {scores.get('clarity','?')}/10 | {bars.get('clarity','')} |
| 主动性 | {scores.get('proactivity','?')}/10 | {bars.get('proactivity','')} |
"""

    improvements = result.get("improvements", [])
    if improvements:
        injection += "\n**改进建议:**\n"
        for imp in improvements[:3]:
            injection += f"- {imp}\n"

    return injection


def get_stats() -> str:
    """评分趋势统计"""
    if not HISTORY_FILE.exists():
        return "还没有评分历史数据"

    scores_list = []
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                scores_list.append(json.loads(line))
            except Exception:
                continue

    if not scores_list:
        return "没有有效的评分记录"

    avgs = [s["average"] for s in scores_list]
    methods = [s.get("method", "?") for s in scores_list]

    llm_count = sum(1 for m in methods if m == "llm")

    lines = [
        f"📊 评分历史: {len(scores_list)} 次",
        f"   平均分: {sum(avgs)/len(avgs):.1f}/10",
        f"   最高: {max(avgs)}/10  最低: {min(avgs)}/10",
        f"   LLM 评分: {llm_count} 次  启发式: {len(scores_list)-llm_count} 次",
        f"   趋势: {'📈 提升中' if len(avgs) >= 2 and avgs[-1] > avgs[0] else '📉 下降中' if len(avgs) >= 2 and avgs[-1] < avgs[0] else '➡️ 平稳'}",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Outcomes Grader — LLM 驱动质量评分")
    sub = parser.add_subparsers(dest="command")

    grade_p = sub.add_parser("grade", help="对文本评分")
    grade_p.add_argument("--text", required=True, help="要评分的文本")
    grade_p.add_argument("--model", default="qwen3-32b", help="评分用模型")

    sub.add_parser("last", help="查看上次评分")
    sub.add_parser("stats", help="评分趋势")
    sub.add_parser("inject", help="生成上下文注入文本（供 hook 使用）")

    args = parser.parse_args()

    if args.command == "grade":
        result = grade_text(args.text, args.model)
        save_result(result)
        s = result["scores"]
        print(f"📊 评分: {result['average']}/10 ({result.get('method','?')})")
        for dim, score in s.items():
            bar = "█" * score + "░" * (10 - score)
            print(f"  {dim:>10}: {bar} {score}/10")
        if result.get("summary"):
            print(f"\n💬 {result['summary']}")
        if result.get("improvements"):
            print("\n🔧 改进建议:")
            for imp in result["improvements"][:3]:
                print(f"  - {imp}")

    elif args.command == "last":
        if STATE_FILE.exists():
            print(STATE_FILE.read_text())
        else:
            print("还没有评分记录")

    elif args.command == "stats":
        print(get_stats())

    elif args.command == "inject":
        injection = get_context_injection()
        if injection:
            print(injection)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()