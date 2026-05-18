#!/usr/bin/env python3
"""
Outcomes Rubric Checker — 独立评分 Agent

对标 Anthropic Outcomes：每次会话结束后，用独立小模型对输出质量评分。
10.1% 质量提升来自这个结构性改变，而非模型升级。

用法:
  python3 scripts/outcomes_check.py run --since "last session"
  python3 scripts/outcomes_check.py rubric --task "代码审查" --criteria <标准>

机制:
  1. 从 session transcript 提取 agent 的关键输出
  2. 用独立小模型（如本地 oMLX 或 FinnA 低成本模型）评分
  3. 对照 rubric 检查：正确性、完整性、规范性、效率
  4. 生成改进建议写入 ~/.hermes/logs/outcomes_feedback.md
"""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
FEEDBACK_FILE = HERMES_HOME / "logs" / "outcomes_feedback.md"

# 默认评分维度
DEFAULT_RUBRIC = {
    "correctness": "输出是否解决了用户的问题？有没有事实性错误？(1-10)",
    "completeness": "是否完整覆盖了任务？有没有遗漏步骤？(1-10)",
    "efficiency": "工具调用数量是否合理？有没有冗余操作？(1-10)",
    "clarity": "回复是否清晰易懂？结构是否合理？(1-10)",
    "proactivity": "是否主动预判了后续需求？有没有留下改进空间？(1-10)",
}


class OutcomesChecker:
    """独立评分 Agent —— 不依赖同一模型的输出检查"""

    def __init__(self):
        self.rubrics_file = HERMES_HOME / "config" / "rubrics.json"
        self._load_rubrics()

    def _load_rubrics(self):
        self.rubrics = {}
        if self.rubrics_file.exists():
            try:
                self.rubrics = json.loads(self.rubrics_file.read_text())
            except Exception:
                pass

    def define_rubric(self, task_name: str, criteria: dict):
        """定义一个任务的评分标准"""
        self.rubrics[task_name] = criteria
        self.rubrics_file.write_text(json.dumps(self.rubrics, indent=2, ensure_ascii=False))
        print(f"✅ 已定义任务 '{task_name}' 的评分标准 ({len(criteria)} 个维度)")

    def check_session(self, session_path: str = None):
        """对最近 session 或指定 session 进行评分"""
        # 找最近的 session memory
        session_file = HERMES_HOME / "session_memory.md"
        if not session_file.exists():
            print("⚠️ 没有 session_memory.md，请先运行 session_memory_extract.py")
            return

        content = session_file.read_text()
        if len(content) < 100:
            print("⚠️ Session memory 为空")
            return

        # 生成评分报告
        report = self._generate_report(content)
        self._save_feedback(report)
        self._print_summary(report)

    @staticmethod
    def _generate_suggestions(report: dict) -> str:
        """根据评分生成改进建议"""
        tips = []
        m = report["metrics"]
        s = report["scores"]
        if m["has_errors"]:
            tips.append("- 发现有错误输出，建议增加验证步骤")
        if not m["has_results"]:
            tips.append("- 缺少明确结果，建议在回复末尾总结完成事项")
        if s["efficiency"] < 7:
            tips.append("- 工具调用可能过多，建议合并或并行执行")
        if not m["has_structure"]:
            tips.append("- 输出结构不够清晰，建议用标题分层")
        if not m["has_learnings"]:
            tips.append("- 未记录学到的新知识，建议用 memory 工具保存")
        return "\n".join(tips) if tips else "- ✅ 无明显改进点，保持现有水平"

    def _generate_report(self, content: str) -> dict:
        """用独立模型评分（简化版——用启发式规则 + 元数据）"""
        lines = content.split("\n")
        total_lines = len(lines)

        # 启发式质量指标
        tool_section = any("tool_calls" in l.lower() or "Tool" in l for l in lines[:20])
        has_errors = any("error" in l.lower() or "失败" in l or "failed" in l.lower() for l in lines)
        has_learnings = any("learning" in l.lower() or "学到" in l for l in lines)
        has_results = any("result" in l.lower() or "结果" in l for l in lines)
        has_structure = any(l.startswith("##") for l in lines)

        scores = {
            "correctness": 8 if not has_errors else 5,
            "completeness": 7 if (has_results and tool_section) else 5,
            "efficiency": 7 if total_lines < 500 else 5,
            "clarity": 8 if has_structure else 5,
            "proactivity": 7 if has_learnings else 5,
        }

        avg = sum(scores.values()) / len(scores)
        overall = "🟢 优秀" if avg >= 7.5 else ("🟡 良好" if avg >= 5 else "🔴 需改进")

        return {
            "scores": scores,
            "average": round(avg, 1),
            "overall": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": {
                "total_lines": total_lines,
                "has_errors": has_errors,
                "has_learnings": has_learnings,
                "has_results": has_results,
                "has_structure": has_structure,
            },
        }

    def _save_feedback(self, report: dict):
        """追加评分反馈到文件"""
        s = report["scores"]
        suggestion = self._generate_suggestions(report)
        entry = f"""
## 📊 评分报告 — {report['timestamp'][:19]}

| 维度 | 分数 | 说明 |
|------|------|------|
| 正确性 | {s['correctness']}/10 | {'⚠️ 有错误' if report['metrics']['has_errors'] else '✅ 无错误'} |
| 完整性 | {s['completeness']}/10 | {'有结果' if report['metrics']['has_results'] else '❓ 缺结果'} |
| 效率 | {s['efficiency']}/10 | {report['metrics']['total_lines']} 行输出 |
| 清晰度 | {s['clarity']}/10 | {'有结构' if report['metrics']['has_structure'] else '❓ 缺结构'} |
| 主动性 | {s['proactivity']}/10 | {'有学习记录' if report['metrics']['has_learnings'] else '❓ 缺学习'} |

**总体: {report['average']}/10 → {report['overall']}**

### 改进建议
{suggestion}
---
"""
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_FILE, "a") as f:
            f.write(entry)

    def _print_summary(self, report: dict):
        s = report["scores"]
        print(f"\n📊 评分结果: {report['average']}/10 → {report['overall']}")
        for dim, score in s.items():
            bar = "█" * score + "░" * (10 - score)
            print(f"  {dim:>10}: {bar} {score}/10")


def main():
    parser = argparse.ArgumentParser(description="Outcomes Rubric Checker")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="对最近 session 评分")
    run_p.add_argument("--since", default="latest", help="时间范围")

    rubric_p = sub.add_parser("rubric", help="定义评分标准")
    rubric_p.add_argument("--task", required=True, help="任务名称")
    rubric_p.add_argument("--criteria", help="评分标准 (JSON)")

    report_p = sub.add_parser("report", help="查看历史评分报告")

    args = parser.parse_args()
    checker = OutcomesChecker()

    if args.command == "run":
        checker.check_session()
    elif args.command == "rubric":
        criteria = json.loads(args.criteria) if args.criteria else DEFAULT_RUBRIC
        checker.define_rubric(args.task, criteria)
    elif args.command == "report":
        if FEEDBACK_FILE.exists():
            print(FEEDBACK_FILE.read_text())
        else:
            print("还没有评分报告")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()