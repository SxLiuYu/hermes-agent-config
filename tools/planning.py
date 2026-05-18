#!/usr/bin/env python3
"""
Planning-First Architecture — 对标 SWE-Agent/Devin

核心功能:
  1. Task decomposition: 将复杂任务拆解为子步骤（带依赖关系）
  2. Plan generation: 生成结构化计划（JSON 格式）
  3. Plan tracking: 追踪子步骤执行进度
  4. Sub-agent context: 为子代理自动生成精简上下文（避免信息洪泛）

存储: ~/.hermes/plans/active.json

用法:
  python3 planning.py decompose --task "修复 data pipeline 的 OOM 问题"
  python3 planning.py track --step-id "s1" --status done
  python3 planning.py status
  python3 planning.py context --step-id "s2"
  python3 planning.py reset
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Constants ───────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
PLANS_DIR = HERMES_HOME / "plans"
ACTIVE_PLAN_PATH = PLANS_DIR / "active.json"
MAX_CONTEXT_LENGTH = 800  # 子代理上下文最大字符数，避免洪泛

# ─── Data Structures ─────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    """计划中的一个子步骤"""
    id: str                          # s1, s2, s3...
    description: str                 # 步骤描述
    status: str = "pending"          # pending | in_progress | done | failed | skipped
    depends_on: list = field(default_factory=list)  # 依赖的步骤 ID 列表
    tool_hints: list = field(default_factory=list)  # 建议使用的工具
    output_hint: str = ""            # 预期产出描述
    started_at: str = ""
    completed_at: str = ""
    agent_notes: str = ""            # 子代理执行后的备注

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "depends_on": self.depends_on,
            "tool_hints": self.tool_hints,
            "output_hint": self.output_hint,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "agent_notes": self.agent_notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanStep":
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            status=d.get("status", "pending"),
            depends_on=d.get("depends_on", []),
            tool_hints=d.get("tool_hints", []),
            output_hint=d.get("output_hint", ""),
            started_at=d.get("started_at", ""),
            completed_at=d.get("completed_at", ""),
            agent_notes=d.get("agent_notes", ""),
        )


@dataclass
class ActivePlan:
    """当前活跃的计划"""
    plan_id: str
    task: str                        # 原始任务描述
    steps: list = field(default_factory=list)  # list[PlanStep]
    created_at: str = ""
    updated_at: str = ""
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    metadata: dict = field(default_factory=dict)  # 额外元数据

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "task": self.task,
            "steps": [s.to_dict() if isinstance(s, PlanStep) else s for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActivePlan":
        steps = []
        for s in d.get("steps", []):
            if isinstance(s, PlanStep):
                steps.append(s)
            else:
                steps.append(PlanStep.from_dict(s))
        return cls(
            plan_id=d.get("plan_id", ""),
            task=d.get("task", ""),
            steps=steps,
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            total_steps=d.get("total_steps", 0),
            completed_steps=d.get("completed_steps", 0),
            failed_steps=d.get("failed_steps", 0),
            metadata=d.get("metadata", {}),
        )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_plan_id(task: str) -> str:
    raw = f"{task}|{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_active_plan() -> Optional[ActivePlan]:
    """从磁盘加载活跃计划"""
    if not ACTIVE_PLAN_PATH.exists():
        return None
    try:
        data = json.loads(ACTIVE_PLAN_PATH.read_text(encoding="utf-8"))
        return ActivePlan.from_dict(data)
    except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
        print(f"⚠️  计划文件损坏: {e}", file=sys.stderr)
        return None


def _save_active_plan(plan: ActivePlan):
    """保存活跃计划到磁盘"""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plan.updated_at = _now()
    plan.total_steps = len(plan.steps)
    plan.completed_steps = sum(1 for s in plan.steps
                               if (s.status if isinstance(s, PlanStep)
                                   else s.get("status", "")) == "done")
    plan.failed_steps = sum(1 for s in plan.steps
                            if (s.status if isinstance(s, PlanStep)
                                else s.get("status", "")) == "failed")
    ACTIVE_PLAN_PATH.write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _step_status_icon(status: str) -> str:
    return {
        "pending": "⬜",
        "in_progress": "🔄",
        "done": "✅",
        "failed": "❌",
        "skipped": "⏭️",
    }.get(status, "❓")


# ─── Task Decomposition ─────────────────────────────────────────────────────


# 通用任务分解模板（基于任务复杂度）
DECOMPOSE_TEMPLATES = {
    # 修复类
    "bug|fix|修复|debug|错误|crash|崩溃|异常": [
        {"id": "s1", "description": "复现问题：理解错误现象并定位出错范围",
         "depends_on": [], "tool_hints": ["search_files", "read_file"],
         "output_hint": "问题根因分析报告"},
        {"id": "s2", "description": "分析根因：阅读相关代码，确认问题源头",
         "depends_on": ["s1"], "tool_hints": ["read_file", "search_files"],
         "output_hint": "根因定位及修复方案"},
        {"id": "s3", "description": "实施修复：编写修复代码",
         "depends_on": ["s2"], "tool_hints": ["patch", "write_file"],
         "output_hint": "修复代码已提交"},
        {"id": "s4", "description": "验证修复：确保修复有效且无回归",
         "depends_on": ["s3"], "tool_hints": ["search_files", "read_file"],
         "output_hint": "验证通过确认"},
    ],
    # 功能开发类
    "feature|功能|开发|implement|add|新增|实现": [
        {"id": "s1", "description": "需求分析：明确功能边界和接口定义",
         "depends_on": [], "tool_hints": ["read_file", "search_files"],
         "output_hint": "需求规格说明"},
        {"id": "s2", "description": "设计方案：确定实现路径和技术选型",
         "depends_on": ["s1"], "tool_hints": [],
         "output_hint": "实现方案概述"},
        {"id": "s3", "description": "核心实现：编写功能代码",
         "depends_on": ["s2"], "tool_hints": ["write_file", "patch"],
         "output_hint": "功能代码完成"},
        {"id": "s4", "description": "测试验证：验证功能正确性",
         "depends_on": ["s3"], "tool_hints": ["search_files", "read_file"],
         "output_hint": "测试通过报告"},
    ],
    # 重构类
    "refactor|重构|优化|improve": [
        {"id": "s1", "description": "现状分析：梳理现有代码结构和依赖",
         "depends_on": [], "tool_hints": ["search_files", "read_file"],
         "output_hint": "代码结构分析"},
        {"id": "s2", "description": "重构实施：执行代码变更",
         "depends_on": ["s1"], "tool_hints": ["patch", "write_file"],
         "output_hint": "重构代码完成"},
        {"id": "s3", "description": "兼容验证：确保重构不破坏现有功能",
         "depends_on": ["s2"], "tool_hints": ["search_files", "read_file"],
         "output_hint": "兼容性验证通过"},
    ],
    # 调研分析类
    "research|调研|分析|评估|review|调查": [
        {"id": "s1", "description": "信息收集：检索相关代码/文档/数据",
         "depends_on": [], "tool_hints": ["search_files", "read_file"],
         "output_hint": "信息汇总"},
        {"id": "s2", "description": "深度分析：对比、归纳、得出结论",
         "depends_on": ["s1"], "tool_hints": [],
         "output_hint": "分析报告"},
        {"id": "s3", "description": "建议输出：提出可执行建议",
         "depends_on": ["s2"], "tool_hints": [],
         "output_hint": "建议方案列表"},
    ],
    # 多文件编辑类
    "multi.?file|多文件|跨文件|多个文件": [
        {"id": "s1", "description": "全局搜索：定位所有需要修改的文件",
         "depends_on": [], "tool_hints": ["search_files"],
         "output_hint": "文件清单"},
        {"id": "s2", "description": "逐文件修改：按依赖顺序编辑",
         "depends_on": ["s1"], "tool_hints": ["read_file", "patch", "write_file"],
         "output_hint": "所有文件修改完成"},
        {"id": "s3", "description": "全局验证：检查跨文件一致性",
         "depends_on": ["s2"], "tool_hints": ["search_files", "read_file"],
         "output_hint": "一致性验证通过"},
    ],
}

# 通用回退模板
FALLBACK_STEPS = [
    {"id": "s1", "description": "理解任务：明确目标、范围和约束",
     "depends_on": [], "tool_hints": [],
     "output_hint": "任务分析结果"},
    {"id": "s2", "description": "执行任务：完成核心工作",
     "depends_on": ["s1"], "tool_hints": ["read_file", "write_file", "patch", "search_files"],
     "output_hint": "核心产出"},
    {"id": "s3", "description": "验证总结：确认任务完成并输出结果",
     "depends_on": ["s2"], "tool_hints": [],
     "output_hint": "验证确认"},
]


def _match_template(task: str) -> list:
    """基于关键词匹配选择分解模板"""
    task_lower = task.lower()
    for pattern, template in DECOMPOSE_TEMPLATES.items():
        if re.search(pattern, task_lower):
            return template
    return FALLBACK_STEPS


def decompose_task(task: str) -> list:
    """
    将任务描述分解为带依赖关系的子步骤列表。

    Args:
        task: 自然语言任务描述

    Returns:
        list[dict]: 子步骤列表，每个包含 id, description, depends_on 等字段
    """
    if not task or not task.strip():
        return []

    template = _match_template(task)
    steps = []
    for s in template:
        step = {
            "id": s["id"],
            "description": s["description"],
            "depends_on": s.get("depends_on", []),
            "tool_hints": s.get("tool_hints", []),
            "output_hint": s.get("output_hint", ""),
        }
        steps.append(step)
    return steps


# ─── Plan Management ─────────────────────────────────────────────────────────


def cmd_decompose(task: str) -> dict:
    """分解任务并创建/更新活跃计划"""
    if not task or not task.strip():
        return {"error": "任务描述不能为空"}

    # 检查是否已有活跃计划
    existing = _load_active_plan()

    if existing:
        # 已有活跃计划 — 确认是否覆盖
        print(f"⚠️  已有活跃计划 (plan_id={existing.plan_id})，将被覆盖", file=sys.stderr)

    steps_raw = decompose_task(task)
    if not steps_raw:
        return {"error": "无法分解任务，请提供更具体的描述"}

    plan_id = _generate_plan_id(task)
    now = _now()

    steps = []
    for s in steps_raw:
        step = PlanStep(
            id=s["id"],
            description=s["description"],
            status="pending",
            depends_on=s.get("depends_on", []),
            tool_hints=s.get("tool_hints", []),
            output_hint=s.get("output_hint", ""),
        )
        steps.append(step)

    plan = ActivePlan(
        plan_id=plan_id,
        task=task,
        steps=steps,
        created_at=now,
        updated_at=now,
        total_steps=len(steps),
        completed_steps=0,
    )

    _save_active_plan(plan)

    result = {
        "plan_id": plan_id,
        "task": task,
        "total_steps": len(steps),
        "steps": [
            {
                "id": s.id,
                "description": s.description,
                "depends_on": s.depends_on,
                "status": s.status,
            }
            for s in steps
        ],
    }
    return result


def cmd_track(step_id: str, status: str, notes: str = "") -> dict:
    """更新指定步骤的状态"""
    plan = _load_active_plan()
    if not plan:
        return {"error": "没有活跃计划，请先执行 decompose"}

    valid_statuses = {"pending", "in_progress", "done", "failed", "skipped"}
    if status not in valid_statuses:
        return {"error": f"无效状态 '{status}'，有效值: {', '.join(sorted(valid_statuses))}"}

    # 找到对应步骤
    found = False
    now = _now()
    for s in plan.steps:
        step = s if isinstance(s, PlanStep) else PlanStep.from_dict(s)
        if step.id == step_id:
            # 检查依赖步骤是否完成
            if status == "in_progress":
                unmet = _check_dependencies(plan, step)
                if unmet:
                    return {
                        "error": f"依赖步骤未完成: {', '.join(unmet)}",
                        "unmet_dependencies": unmet,
                    }

            step.status = status
            if status == "in_progress" and not step.started_at:
                step.started_at = now
            if status == "done":
                step.completed_at = now
            if status == "failed":
                step.completed_at = now
            if notes:
                step.agent_notes = notes

            # 持久化回 plan.steps
            for i, ps in enumerate(plan.steps):
                if (ps.id if isinstance(ps, PlanStep) else ps.get("id", "")) == step_id:
                    plan.steps[i] = step
                    break

            found = True
            break

    if not found:
        return {"error": f"步骤 '{step_id}' 不存在"}

    _save_active_plan(plan)

    # 检查是否所有步骤完成
    all_done = all(
        (s.status if isinstance(s, PlanStep) else s.get("status", "")) == "done"
        for s in plan.steps
    )

    return {
        "step_id": step_id,
        "status": status,
        "plan_progress": f"{plan.completed_steps}/{plan.total_steps}",
        "all_done": all_done,
    }


def _check_dependencies(plan: ActivePlan, step: PlanStep) -> list:
    """检查步骤的依赖是否满足"""
    unmet = []
    for dep_id in step.depends_on:
        dep_step = None
        for s in plan.steps:
            sid = s.id if isinstance(s, PlanStep) else s.get("id", "")
            if sid == dep_id:
                dep_step = s
                break
        if dep_step is None:
            unmet.append(f"{dep_id}(不存在)")
        else:
            dep_status = dep_step.status if isinstance(dep_step, PlanStep) else dep_step.get("status", "")
            if dep_status != "done":
                unmet.append(f"{dep_id}({dep_status})")
    return unmet


def cmd_status() -> dict:
    """显示当前计划状态"""
    plan = _load_active_plan()
    if not plan:
        return {"status": "no_active_plan", "message": "当前没有活跃计划"}

    steps_detail = []
    for s in plan.steps:
        step = s if isinstance(s, PlanStep) else PlanStep.from_dict(s)
        steps_detail.append({
            "id": step.id,
            "description": step.description,
            "status": step.status,
            "depends_on": step.depends_on,
            "output_hint": step.output_hint,
            "started_at": step.started_at,
            "completed_at": step.completed_at,
            "agent_notes": step.agent_notes,
        })

    pending = sum(1 for s in steps_detail if s["status"] == "pending")
    in_progress = sum(1 for s in steps_detail if s["status"] == "in_progress")
    done = sum(1 for s in steps_detail if s["status"] == "done")
    failed = sum(1 for s in steps_detail if s["status"] == "failed")
    skipped = sum(1 for s in steps_detail if s["status"] == "skipped")

    # 找出下一个可执行的步骤（pending 且所有依赖已完成）
    next_steps = []
    for s in steps_detail:
        if s["status"] == "pending":
            deps_met = all(
                any(d2["id"] == dep and d2["status"] == "done" for d2 in steps_detail)
                for dep in s["depends_on"]
            )
            if deps_met:
                next_steps.append(s["id"])

    return {
        "plan_id": plan.plan_id,
        "task": plan.task,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "progress": {
            "total": plan.total_steps,
            "pending": pending,
            "in_progress": in_progress,
            "done": done,
            "failed": failed,
            "skipped": skipped,
            "percentage": round(done / max(plan.total_steps, 1) * 100, 1),
        },
        "next_available_steps": next_steps,
        "steps": steps_detail,
    }


def cmd_context(step_id: str) -> dict:
    """
    为子代理生成精简上下文。
    对标 SWE-Agent/Devin：只提供必要信息，避免信息洪泛。
    """
    plan = _load_active_plan()
    if not plan:
        return {"error": "没有活跃计划"}

    # 找到目标步骤
    target_step = None
    for s in plan.steps:
        step = s if isinstance(s, PlanStep) else PlanStep.from_dict(s)
        if step.id == step_id:
            target_step = step
            break

    if not target_step:
        return {"error": f"步骤 '{step_id}' 不存在"}

    # 收集已完成步骤的产出摘要
    completed_summaries = []
    for s in plan.steps:
        step = s if isinstance(s, PlanStep) else PlanStep.from_dict(s)
        if step.status == "done":
            summary = f"[{step.id}] {step.description}"
            if step.agent_notes:
                summary += f" — {step.agent_notes[:200]}"
            completed_summaries.append(summary)

    # 收集依赖步骤详情
    dep_details = []
    for dep_id in target_step.depends_on:
        for s in plan.steps:
            step = s if isinstance(s, PlanStep) else PlanStep.from_dict(s)
            if step.id == dep_id:
                dep_details.append({
                    "id": step.id,
                    "description": step.description,
                    "output": step.output_hint,
                    "notes": step.agent_notes,
                })

    # 构建精简上下文
    context_parts = [
        f"## 任务: {plan.task}",
        f"## 当前步骤: [{target_step.id}] {target_step.description}",
    ]

    if target_step.output_hint:
        context_parts.append(f"## 预期产出: {target_step.output_hint}")

    if target_step.tool_hints:
        context_parts.append(f"## 建议工具: {', '.join(target_step.tool_hints)}")

    if dep_details:
        context_parts.append("## 依赖步骤产出:")
        for dd in dep_details:
            context_parts.append(f"- [{dd['id']}] {dd['description']}")
            if dd["notes"]:
                context_parts.append(f"  备注: {dd['notes'][:150]}")

    if completed_summaries:
        context_parts.append("## 已完成步骤摘要:")
        for cs in completed_summaries[-5:]:  # 最多展示 5 个
            context_parts.append(f"- {cs}")

    # 整体计划进度条
    done_count = sum(1 for s in plan.steps
                     if (s.status if isinstance(s, PlanStep) else s.get("status", "")) == "done")
    total = len(plan.steps)
    bar = "█" * done_count + "░" * (total - done_count)
    context_parts.append(f"## 整体进度: [{bar}] {done_count}/{total}")

    full_context = "\n".join(context_parts)

    # 如果过长则截断，优先保留前面的关键信息
    if len(full_context) > MAX_CONTEXT_LENGTH:
        # 按行截断
        lines = full_context.split("\n")
        truncated = []
        length = 0
        for line in lines:
            if length + len(line) + 1 > MAX_CONTEXT_LENGTH:
                truncated.append(f"... (上下文已截断，总计 {len(full_context)} 字符)")
                break
            truncated.append(line)
            length += len(line) + 1
        full_context = "\n".join(truncated)

    return {
        "step_id": step_id,
        "context": full_context,
        "context_length": len(full_context),
        "truncated": len(full_context) < len("\n".join(context_parts)),
    }


def cmd_reset() -> dict:
    """重置活跃计划"""
    if ACTIVE_PLAN_PATH.exists():
        # 归档旧计划
        archive_dir = PLANS_DIR / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            plan = _load_active_plan()
            if plan:
                archive_path = archive_dir / f"{plan.plan_id}.json"
                archive_path.write_text(
                    ACTIVE_PLAN_PATH.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
        except Exception:
            pass
        ACTIVE_PLAN_PATH.unlink()

    return {"status": "reset", "message": "活跃计划已清除"}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Planning-First Architecture — 任务规划与追踪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s decompose --task "修复 data pipeline 的 OOM 问题"
  %(prog)s track --step-id "s1" --status done
  %(prog)s track --step-id "s2" --status in_progress
  %(prog)s status
  %(prog)s context --step-id "s2"
  %(prog)s reset
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # decompose
    p_decompose = subparsers.add_parser("decompose", help="分解任务为子步骤")
    p_decompose.add_argument("--task", required=True, help="任务描述")

    # track
    p_track = subparsers.add_parser("track", help="更新步骤状态")
    p_track.add_argument("--step-id", required=True, help="步骤 ID (如 s1, s2)")
    p_track.add_argument("--status", required=True,
                         choices=["pending", "in_progress", "done", "failed", "skipped"],
                         help="新状态")
    p_track.add_argument("--notes", default="", help="子代理执行备注")

    # status
    subparsers.add_parser("status", help="显示当前计划状态")

    # context
    p_context = subparsers.add_parser("context", help="为子代理生成精简上下文")
    p_context.add_argument("--step-id", required=True, help="目标步骤 ID")

    # reset
    subparsers.add_parser("reset", help="清除活跃计划")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "decompose":
            result = cmd_decompose(args.task)
        elif args.command == "track":
            result = cmd_track(args.step_id, args.status, args.notes)
        elif args.command == "status":
            result = cmd_status()
        elif args.command == "context":
            result = cmd_context(args.step_id)
        elif args.command == "reset":
            result = cmd_reset()
        else:
            result = {"error": f"未知命令: {args.command}"}

        print(json.dumps(result, indent=2, ensure_ascii=False))

        # 如果有 error 则返回非零退出码
        if isinstance(result, dict) and "error" in result:
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2, ensure_ascii=False),
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()