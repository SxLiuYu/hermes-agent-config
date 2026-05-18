#!/usr/bin/env python3
"""
P0-19: Tool Selection Accuracy Test — 评估优化前后 Agent 工具选择质量

对标:
  - SWE-bench: 实证验证工具输出压缩对 Agent 性能的影响
  - MT-AgentRisk: 多轮工具使用 Agent 安全性基准

核心思想:
  用测试用例验证 Agent 在给定任务描述时是否选择了正确的工具。
  比较优化前后的 tool schema，量化改进效果。

测试方法:
  1. 定义测试集：任务描述 + 期望的工具集
  2. 用规则引擎（非 LLM）模拟工具选择逻辑
  3. 计算精度、召回率、F1
  4. 对比原始 schema vs 优化后 schema
"""

import json
import os
import re
from dataclasses import dataclass, field
from collections import defaultdict

# ─── 测试用例 ─────────────────────────────────────────────


@dataclass
class TestCase:
    """一个工具选择测试用例"""
    id: str
    task_description: str       # 用户任务描述
    expected_tools: list[str]  # 期望选择的工具
    forbidden_tools: list[str] = field(default_factory=list)  # 不应选择的工具
    category: str = "general"


# 预定义测试集 — 覆盖常见场景
DEFAULT_TEST_CASES = [
    # 代码修复
    TestCase(
        "fix-1", "修复 auth.py 中的登录错误",
        ["search_files", "read_file", "patch"], [],
        "fix"
    ),
    TestCase(
        "fix-2", "修复一个 Python 语法错误",
        ["search_files", "read_file", "patch", "terminal"], [],
        "fix"
    ),
    # 功能开发
    TestCase(
        "feat-1", "添加用户注册 API 接口",
        ["write_file", "terminal", "search_files"], [],
        "feature"
    ),
    TestCase(
        "feat-2", "实现一个新的 Flask 路由",
        ["search_files", "write_file", "patch", "terminal"], [],
        "feature"
    ),
    # 部署
    TestCase(
        "deploy-1", "将应用部署到阿里云服务器",
        ["terminal", "read_file", "write_file"], [],
        "deploy"
    ),
    TestCase(
        "deploy-2", "更新生产环境的 Docker 镜像",
        ["terminal", "read_file"], [],
        "deploy"
    ),
    # 配置
    TestCase(
        "config-1", "修改 nginx 配置文件",
        ["read_file", "patch"], ["write_file"],
        "config"
    ),
    TestCase(
        "config-2", "设置环境变量",
        ["terminal", "write_file"], [],
        "config"
    ),
    # 数据
    TestCase(
        "data-1", "分析 CSV 文件的数据",
        ["read_file", "execute_code", "terminal"], [],
        "data"
    ),
    TestCase(
        "data-2", "从数据库导出用户数据",
        ["terminal", "execute_code"], [],
        "data"
    ),
    # 研究/搜索
    TestCase(
        "research-1", "查找 Python asyncio 的最佳实践",
        ["web_search"], [],
        "research"
    ),
    TestCase(
        "research-2", "搜索 GitHub 上的开源项目",
        ["web_search", "web_extract"], [],
        "research"
    ),
    # 测试
    TestCase(
        "test-1", "运行单元测试并修复失败用例",
        ["terminal", "search_files", "read_file", "patch"], [],
        "test"
    ),
    TestCase(
        "test-2", "编写新的集成测试",
        ["write_file", "terminal", "search_files"], [],
        "test"
    ),
    # 审查
    TestCase(
        "review-1", "审查这个 PR 的代码变更",
        ["read_file", "search_files"], [],
        "code_review"
    ),
    # 内存/会话
    TestCase(
        "memory-1", "保存用户的偏好设置",
        ["memory"], [],
        "memory"
    ),
    # 消息
    TestCase(
        "msg-1", "给团队发送一条通知",
        ["send_message"], [],
        "message"
    ),
    # 文档
    TestCase(
        "doc-1", "更新 README 文档",
        ["read_file", "write_file"], [],
        "doc"
    ),
    # 复杂任务
    TestCase(
        "complex-1", "创建一个新的 REST API 端点，包含测试和文档",
        ["search_files", "write_file", "terminal", "read_file", "patch"], [],
        "feature"
    ),
]


# ─── 工具选择模拟器 ──────────────────────────────────────


class ToolSelector:
    """
    模拟 Agent 的工具选择逻辑。
    基于关键词匹配——对标 Agent 根据 tool description 选择工具的过程。
    """

    def __init__(self, tool_schemas: list[dict] = None):
        self.tools = tool_schemas or self._load_default_tools()

    @staticmethod
    def _load_default_tools() -> list[dict]:
        """加载默认工具 schema"""
        return [
            {"name": "terminal", "description": "执行 shell 命令"},
            {"name": "read_file", "description": "读取文件内容"},
            {"name": "write_file", "description": "写入文件内容（覆盖）"},
            {"name": "patch", "description": "查找并替换文件中的文本"},
            {"name": "search_files", "description": "搜索文件内容或按名称查找文件"},
            {"name": "execute_code", "description": "运行 Python 代码并获取结果"},
            {"name": "web_search", "description": "搜索互联网信息"},
            {"name": "web_extract", "description": "从网页提取 Markdown 内容"},
            {"name": "memory", "description": "保存或检索持久化记忆"},
            {"name": "send_message", "description": "发送消息到飞书/微信等平台"},
            {"name": "browser_navigate", "description": "在浏览器中打开网页"},
            {"name": "delegate_task", "description": "派生子 Agent 执行任务"},
            {"name": "todo", "description": "管理任务列表"},
        ]

    def select_tools(self, task: str, top_k: int = 5) -> list[str]:
        """
        根据任务描述选择最相关的 top_k 工具。

        模拟逻辑: 对每个工具的 description 和 name 做关键词匹配评分。
        """
        scores = []
        task_lower = task.lower()

        for tool in self.tools:
            name = tool["name"].lower()
            desc = tool.get("description", "").lower()

            score = 0.0

            # 关键词直接匹配
            keyword_map = {
                "terminal": ["运行", "执行", "run", "execute", "deploy", "部署",
                            "安装", "install", "测试", "test", "构建", "build",
                            "命令", "command", "shell", "bash", "Docker", "docker",
                            "git", "pip", "npm"],
                "read_file": ["读取", "查看", "检查", "read", "view", "inspect",
                             "内容", "content", "文件", "file", "审查", "review",
                             "了解"],
                "write_file": ["创建", "写入", "生成", "create", "write", "generate",
                              "新建", "添加", "add", "文档", "doc"],
                "patch": ["修改", "修复", "fix", "改", "modify", "更新", "update",
                         "替换", "replace", "编辑", "edit", "bug"],
                "search_files": ["搜索", "查找", "search", "find", "定位", "locate",
                                "哪里", "where", "代码", "code"],
                "execute_code": ["分析", "处理", "计算", "analyze", "process", "compute",
                                "数据", "data", "脚本", "script", "python"],
                "web_search": ["搜索", "查找", "search", "find", "在线", "online",
                              "互联网", "internet", "了解", "learn", "信息",
                              "最佳实践", "best practice"],
                "web_extract": ["提取", "抓取", "extract", "scrape", "网页", "page",
                               "文章", "article", "内容", "content"],
                "memory": ["记住", "记忆", "保存", "remember", "save", "store",
                          "偏好", "preference", "设置", "配置"],
                "send_message": ["发送", "通知", "send", "notify", "消息", "message",
                                "飞书", "微信"],
                "browser_navigate": ["打开", "访问", "浏览器", "browser", "网页", "webpage"],
                "delegate_task": ["委托", "派发", "delegate", "子任务", "subtask",
                                 "并行", "parallel", "分发"],
                "todo": ["任务列表", "todo", "计划", "plan", "追踪", "track"],
            }

            keywords = keyword_map.get(name, [])
            for kw in keywords:
                if kw in task_lower:
                    score += 1.0

            # description 中的关键词匹配（加权较低）
            for kw in keywords[:5]:  # 只用前 5 个最重要的
                if kw in desc:
                    score += 0.3

            # 名称精确匹配
            if name in task_lower:
                score += 2.0

            # 单词匹配（对于英文）
            name_words = set(name.split("_"))
            task_words = set(re.findall(r"[a-z_]+", task_lower))
            common = name_words & task_words
            score += len(common) * 0.5

            scores.append((tool["name"], score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in scores[:top_k] if _ > 0]


# ─── 评估 ─────────────────────────────────────────────────


def evaluate_selection(
    selector: ToolSelector,
    test_cases: list[TestCase],
    top_k: int = 5,
) -> dict:
    """
    评估工具选择准确率。

    指标:
    - precision: 选中的工具中有多少是正确的
    - recall: 期望工具中有多少被选中
    - f1: 调和平均
    - forbidden_rate: 选错被禁工具的比例
    """
    results = []
    total_precision = 0
    total_recall = 0
    total_f1 = 0

    for tc in test_cases:
        selected = selector.select_tools(tc.task_description, top_k)

        # 精确率: 选中的工具中有多少在期望列表中
        correct = set(selected) & set(tc.expected_tools)
        precision = len(correct) / max(len(selected), 1)

        # 召回率: 期望工具中有多少被选中
        recall = len(correct) / max(len(tc.expected_tools), 1)

        # F1
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0
        )

        # 禁止工具检测
        forbidden_selected = set(selected) & set(tc.forbidden_tools)
        forbidden_rate = len(forbidden_selected) / max(len(tc.forbidden_tools), 1) if tc.forbidden_tools else 0

        total_precision += precision
        total_recall += recall
        total_f1 += f1

        results.append({
            "test_id": tc.id,
            "task": tc.task_description[:60],
            "selected": selected,
            "expected": tc.expected_tools,
            "correct": list(correct),
            "missed": list(set(tc.expected_tools) - set(selected)),
            "forbidden_hit": list(forbidden_selected),
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "forbidden_rate": round(forbidden_rate, 3),
        })

    n = len(test_cases)
    summary = {
        "total_tests": n,
        "avg_precision": round(total_precision / n, 3),
        "avg_recall": round(total_recall / n, 3),
        "avg_f1": round(total_f1 / n, 3),
        "by_category": {},
        "details": results,
    }

    # 按类别统计
    by_cat = defaultdict(list)
    for r in results:
        tc = next(t for t in test_cases if t.id == r["test_id"])
        by_cat[tc.category].append(r)

    for cat, cat_results in by_cat.items():
        summary["by_category"][cat] = {
            "count": len(cat_results),
            "avg_precision": round(sum(r["precision"] for r in cat_results) / len(cat_results), 3),
            "avg_recall": round(sum(r["recall"] for r in cat_results) / len(cat_results), 3),
            "avg_f1": round(sum(r["f1"] for r in cat_results) / len(cat_results), 3),
        }

    return summary


# ─── 对比测试 ─────────────────────────────────────────────


def compare_schemas(
    original_schemas: list[dict],
    optimized_schemas: list[dict],
    test_cases: list[TestCase] = None,
) -> dict:
    """
    对比原始 schema 和优化后 schema 的工具选择准确率。
    """
    if test_cases is None:
        test_cases = DEFAULT_TEST_CASES

    original_selector = ToolSelector(original_schemas)
    optimized_selector = ToolSelector(optimized_schemas)

    original_result = evaluate_selection(original_selector, test_cases)
    optimized_result = evaluate_selection(optimized_selector, test_cases)

    # 逐项对比
    per_test_comparison = []
    improved_count = 0
    degraded_count = 0
    same_count = 0

    for orig, opt in zip(original_result["details"], optimized_result["details"]):
        delta_f1 = opt["f1"] - orig["f1"]
        if delta_f1 > 0.01:
            improved_count += 1
        elif delta_f1 < -0.01:
            degraded_count += 1
        else:
            same_count += 1

        per_test_comparison.append({
            "test_id": orig["test_id"],
            "task": orig["task"],
            "original_f1": orig["f1"],
            "optimized_f1": opt["f1"],
            "delta_f1": round(delta_f1, 3),
            "original_selected": orig["selected"],
            "optimized_selected": opt["selected"],
        })

    return {
        "original": {
            "avg_f1": original_result["avg_f1"],
            "avg_precision": original_result["avg_precision"],
            "avg_recall": original_result["avg_recall"],
        },
        "optimized": {
            "avg_f1": optimized_result["avg_f1"],
            "avg_precision": optimized_result["avg_precision"],
            "avg_recall": optimized_result["avg_recall"],
        },
        "delta_f1": round(optimized_result["avg_f1"] - original_result["avg_f1"], 3),
        "improved_tests": improved_count,
        "degraded_tests": degraded_count,
        "unchanged_tests": same_count,
        "total_tests": len(test_cases),
        "per_test": per_test_comparison,
    }


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  tool_selector_test.py test [task]")
        print("  tool_selector_test.py evaluate")
        print("  tool_selector_test.py compare <original.json> <optimized.json>")
        print("  tool_selector_test.py report")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "test":
        task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "修复一个 Python bug"
        selector = ToolSelector()
        tools = selector.select_tools(task)
        print(f"Task: {task}")
        print(f"Selected tools: {tools}")

    elif cmd == "evaluate":
        selector = ToolSelector()
        result = evaluate_selection(selector, DEFAULT_TEST_CASES)
        print(f"=== Tool Selection Evaluation ===\n")
        print(f"Total tests: {result['total_tests']}")
        print(f"Avg Precision: {result['avg_precision']:.3f}")
        print(f"Avg Recall: {result['avg_recall']:.3f}")
        print(f"Avg F1: {result['avg_f1']:.3f}")
        print(f"\nBy category:")
        for cat, stats in sorted(result["by_category"].items()):
            print(f"  {cat}: F1={stats['avg_f1']:.3f} "
                  f"(P={stats['avg_precision']:.3f}, R={stats['avg_recall']:.3f})")
        print(f"\nFailures:")
        for r in result["details"]:
            if r["f1"] < 0.8:
                print(f"  [{r['test_id']}] {r['task']}")
                print(f"    Selected: {r['selected']}")
                print(f"    Expected: {r['expected']}")
                print(f"    Missed: {r['missed']}")

    elif cmd == "compare":
        if len(sys.argv) < 4:
            print("Usage: tool_selector_test.py compare <original.json> <optimized.json>")
            sys.exit(1)

        with open(sys.argv[2]) as f:
            original = json.load(f)
        with open(sys.argv[3]) as f:
            optimized = json.load(f)

        result = compare_schemas(original, optimized)
        print(f"=== Schema Comparison ===\n")
        print(f"Original F1: {result['original']['avg_f1']:.3f}")
        print(f"Optimized F1: {result['optimized']['avg_f1']:.3f}")
        print(f"Delta: {result['delta_f1']:+.3f}")
        print(f"Improved: {result['improved_tests']} | "
              f"Degraded: {result['degraded_tests']} | "
              f"Unchanged: {result['unchanged_tests']}")
        print(f"\nPer-test breakdown:")
        for t in result["per_test"]:
            icon = "↑" if t["delta_f1"] > 0 else ("↓" if t["delta_f1"] < 0 else "=")
            print(f"  {icon} [{t['test_id']}] {t['task'][:50]}: "
                  f"{t['original_f1']:.3f} → {t['optimized_f1']:.3f} "
                  f"({t['delta_f1']:+.3f})")

    elif cmd == "report":
        selector = ToolSelector()
        result = evaluate_selection(selector, DEFAULT_TEST_CASES)

        print("## 工具选择准确率报告\n")
        print(f"- 测试用例: {result['total_tests']}")
        print(f"- 平均精度: {result['avg_precision']:.1%}")
        print(f"- 平均召回: {result['avg_recall']:.1%}")
        print(f"- 平均 F1: {result['avg_f1']:.1%}\n")

        print("### 按类别\n")
        for cat, stats in sorted(result["by_category"].items()):
            print(f"- **{cat}**: F1={stats['avg_f1']:.1%} "
                  f"(P={stats['avg_precision']:.1%}, R={stats['avg_recall']:.1%})")

        print("\n### 待改进用例\n")
        for r in result["details"]:
            if r["f1"] < 1.0:
                print(f"- [{r['test_id']}] {r['task']}")
                if r["missed"]:
                    print(f"  - 遗漏: {r['missed']}")
                if r["forbidden_hit"]:
                    print(f"  - 误选禁具: {r['forbidden_hit']}")