#!/usr/bin/env python3
"""
Hermes Architect Mode — Architect/Editor 双模式脚本。
受 Aider 的 Architect/Editor 模式启发：分离"思考规划"和"写代码编辑"两个阶段。

核心概念:
  Architect 阶段: 用强模型（DeepSeek V4 Pro / Kimi K2）分析问题，输出结构化计划，不写代码
  Editor 阶段:   用便宜模型（oMLX Qwen3.5-4B / DeepSeek Flash）严格按照计划执行编辑

命令行接口:
  # 生成计划（Architect 阶段）
  python architect_mode.py plan "修复 daily_replay_v9.7.py 的 bug" --model deepseek-v4-pro --output plan.json

  # 执行计划（Editor 阶段）
  python architect_mode.py execute plan.json --model omlx-qwen --workdir /path/to/project

  # 一步完成（plan + execute）
  python architect_mode.py auto "修复 xxx bug" --architect deepseek-v4-pro --editor omlx-qwen

特性:
  - 成本预估：计划生成时就估算 architect + editor 总成本
  - 计划验证：editor 执行前校验计划的可行性
  - 步骤追踪：执行过程中实时更新进度
  - 回滚支持：如果某步骤失败，支持回退到上一步快照
  - Hermes 集成：可通过 config.yaml 的 architect_mode 配置段启用
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 常量 ─────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

CONFIG_DIR = Path.home() / ".hermes"
SCRIPTS_DIR = CONFIG_DIR / "scripts"
ARCHITECT_CONFIG_PATH = CONFIG_DIR / "architect_config.json"

# 计划文件 Schema 版本
PLAN_SCHEMA_VERSION = "1.0"

# 成本模型 (每 1000 token 的美元价格)
COST_TABLE: Dict[str, Dict[str, float]] = {
    "deepseek-v4-pro": {
        "input": 0.01,
        "output": 0.02,
        "description": "DeepSeek V4 Pro — 最强推理",
        "avg_latency_ms": 2000,
        "context_window": 128000,
    },
    "kimi-k2": {
        "input": 0.008,
        "output": 0.016,
        "description": "Kimi K2 — 超强推理 + 超长上下文",
        "avg_latency_ms": 1500,
        "context_window": 256000,
    },
    "deepseek-v3": {
        "input": 0.003,
        "output": 0.006,
        "description": "DeepSeek V3.1 — 标准模型",
        "avg_latency_ms": 800,
        "context_window": 64000,
    },
    "deepseek-flash": {
        "input": 0.001,
        "output": 0.002,
        "description": "DeepSeek Flash — 极低价快速模型",
        "avg_latency_ms": 400,
        "context_window": 32000,
    },
    "glm-4.6": {
        "input": 0.003,
        "output": 0.006,
        "description": "GLM-4.6 — 多语言标准模型",
        "avg_latency_ms": 700,
        "context_window": 128000,
    },
    "omlx-qwen": {
        "input": 0.0,
        "output": 0.0,
        "description": "oMLX Qwen3.5-4B — 本地免费模型",
        "avg_latency_ms": 200,
        "context_window": 8192,
    },
    "omlx-qwen3.5": {
        "input": 0.0,
        "output": 0.0,
        "description": "oMLX Qwen3.5-4B（别名）",
        "avg_latency_ms": 200,
        "context_window": 8192,
    },
}

# 模型别名映射
MODEL_ALIASES = {
    "architect": "deepseek-v4-pro",     # 默认 Architect 模型
    "editor": "omlx-qwen",              # 默认 Editor 模型
    "cheap": "deepseek-flash",
    "strong": "deepseek-v4-pro",
    "v4": "deepseek-v4-pro",
    "kimi": "kimi-k2",
    "flash": "deepseek-flash",
    "omlx": "omlx-qwen",
    "local": "omlx-qwen",
    "v3": "deepseek-v3",
    "glm": "glm-4.6",
}

# 合法的计划步骤动作类型
VALID_ACTIONS = {
    "read_file",
    "edit",
    "create_file",
    "delete_file",
    "move_file",
    "shell",
    "test",
    "search",
    "analysis",
    "wait_approval",
    "rollback_marker",
}

# 需要目标文件路径的动作
FILE_ACTIONS = {
    "read_file", "edit", "create_file", "delete_file", "move_file", "test",
}


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 数据类型 ─────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class PlanStatus(Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class StepAction(Enum):
    READ_FILE = "read_file"
    EDIT = "edit"
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    MOVE_FILE = "move_file"
    SHELL = "shell"
    TEST = "test"
    SEARCH = "search"
    ANALYSIS = "analysis"
    WAIT_APPROVAL = "wait_approval"
    ROLLBACK_MARKER = "rollback_marker"


@dataclass
class CostEstimate:
    """成本估算。"""
    architect_input_tokens: int = 0
    architect_output_tokens: int = 0
    editor_input_tokens: int = 0
    editor_output_tokens: int = 0
    architect_cost: float = 0.0
    editor_cost: float = 0.0
    total_cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "architect_input_tokens": self.architect_input_tokens,
            "architect_output_tokens": self.architect_output_tokens,
            "editor_input_tokens": self.editor_input_tokens,
            "editor_output_tokens": self.editor_output_tokens,
            "architect_cost": round(self.architect_cost, 6),
            "editor_cost": round(self.editor_cost, 6),
            "total_cost": round(self.total_cost, 6),
        }


@dataclass
class PlanStep:
    """计划中的单个步骤。"""
    step: int
    action: str  # read_file, edit, create_file, delete_file, move_file, shell, test, search, analysis, wait_approval, rollback_marker
    target: str = ""  # 目标文件路径或命令
    intent: str = ""  # 步骤意图说明
    details: str = ""  # 详细操作描述（对 edit 而言是具体的修改说明）
    expected_output: str = ""  # 期望的输出/结果
    depends_on: Optional[int] = None  # 依赖的前置步骤序号
    retry_count: int = 0  # 已重试次数
    max_retries: int = 3  # 最大重试次数
    status: StepStatus = StepStatus.PENDING
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: float = 0.0

    # 执行结果
    output: Optional[str] = None
    diff: Optional[str] = None  # 对 edit 操作，存储 unified diff
    snapshot_path: Optional[str] = None  # 回滚用快照路径

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "step": self.step,
            "action": self.action,
            "target": self.target,
            "intent": self.intent,
            "details": self.details,
        }
        if self.expected_output:
            d["expected_output"] = self.expected_output
        if self.depends_on is not None:
            d["depends_on"] = self.depends_on
        if self.max_retries != 3:
            d["max_retries"] = self.max_retries
        return d

    def to_dict_full(self) -> Dict[str, Any]:
        return {
            **self.to_dict(),
            "status": self.status.value,
            "retry_count": self.retry_count,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "diff": self.diff,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlanStep":
        return cls(
            step=d["step"],
            action=d["action"],
            target=d.get("target", ""),
            intent=d.get("intent", ""),
            details=d.get("details", ""),
            expected_output=d.get("expected_output", ""),
            depends_on=d.get("depends_on"),
            max_retries=d.get("max_retries", 3),
            status=StepStatus(d.get("status", "pending")),
            retry_count=d.get("retry_count", 0),
            error=d.get("error"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            duration_ms=d.get("duration_ms", 0.0),
            output=d.get("output"),
            diff=d.get("diff"),
            snapshot_path=d.get("snapshot_path"),
        )


@dataclass
class ArchitectPlan:
    """完整的架构计划。"""
    schema_version: str = PLAN_SCHEMA_VERSION
    task: str = ""
    analysis: str = ""
    plan: List[PlanStep] = field(default_factory=list)
    files_to_touch: List[str] = field(default_factory=list)
    estimated_cost: CostEstimate = field(default_factory=CostEstimate)

    # 元数据
    id: str = ""
    created_at: str = ""
    architect_model: str = ""
    editor_model: str = ""
    workdir: str = "."
    status: PlanStatus = PlanStatus.DRAFT
    resume_step: int = 0  # 断点续传的起始步骤

    # 执行统计
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    execution_started_at: Optional[str] = None
    execution_completed_at: Optional[str] = None
    total_duration_ms: float = 0.0

    def generate_id(self) -> str:
        """基于内容和时间生成唯一 ID。"""
        raw = f"{self.task}|{self.created_at}|{len(self.plan)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def progress(self) -> float:
        """返回执行进度 0.0 ~ 1.0。"""
        if self.total_steps == 0:
            return 0.0
        return self.completed_steps / self.total_steps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "task": self.task,
            "analysis": self.analysis,
            "plan": [s.to_dict() for s in self.plan],
            "files_to_touch": self.files_to_touch,
            "estimated_cost": self.estimated_cost.to_dict(),
            "created_at": self.created_at,
            "architect_model": self.architect_model,
            "editor_model": self.editor_model,
            "workdir": self.workdir,
            "status": self.status.value,
            "resume_step": self.resume_step,
        }

    def to_dict_full(self) -> Dict[str, Any]:
        d = self.to_dict()
        d["plan"] = [s.to_dict_full() for s in self.plan]
        d["total_steps"] = self.total_steps
        d["completed_steps"] = self.completed_steps
        d["failed_steps"] = self.failed_steps
        d["execution_started_at"] = self.execution_started_at
        d["execution_completed_at"] = self.execution_completed_at
        d["total_duration_ms"] = self.total_duration_ms
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArchitectPlan":
        cost = CostEstimate()
        if "estimated_cost" in d:
            ec = d["estimated_cost"]
            cost = CostEstimate(
                architect_input_tokens=ec.get("architect_input_tokens", 0),
                architect_output_tokens=ec.get("architect_output_tokens", 0),
                editor_input_tokens=ec.get("editor_input_tokens", 0),
                editor_output_tokens=ec.get("editor_output_tokens", 0),
                architect_cost=ec.get("architect_cost", 0),
                editor_cost=ec.get("editor_cost", 0),
                total_cost=ec.get("total_cost", 0),
            )
        return cls(
            schema_version=d.get("schema_version", PLAN_SCHEMA_VERSION),
            task=d.get("task", ""),
            analysis=d.get("analysis", ""),
            plan=[PlanStep.from_dict(s) for s in d.get("plan", [])],
            files_to_touch=d.get("files_to_touch", []),
            estimated_cost=cost,
            id=d.get("id", ""),
            created_at=d.get("created_at", ""),
            architect_model=d.get("architect_model", ""),
            editor_model=d.get("editor_model", ""),
            workdir=d.get("workdir", "."),
            status=PlanStatus(d.get("status", "draft")),
            resume_step=d.get("resume_step", 0),
            total_steps=d.get("total_steps", 0),
            completed_steps=d.get("completed_steps", 0),
            failed_steps=d.get("failed_steps", 0),
            execution_started_at=d.get("execution_started_at"),
            execution_completed_at=d.get("execution_completed_at"),
            total_duration_ms=d.get("total_duration_ms", 0.0),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 成本估算器 ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class CostEstimator:
    """估算 architect + editor 的总成本。

    成本计算基于:
    1. 模型定价表 (COST_TABLE)
    2. 经验公式: 基于任务描述长度和计划复杂度估算 token 用量
    """

    # 每步编辑大约需要的 token 数（经验值）
    TOKENS_PER_EDIT_STEP_INPUT = 500   # 代码上下文 + 指令
    TOKENS_PER_EDIT_STEP_OUTPUT = 300  # 编辑输出

    # Architect 计划生成的 token 估算基准
    TOKENS_PER_TASK_CHAR_INPUT = 0.5   # 每个任务描述字符约 0.5 token
    TOKENS_PER_STEP_OUTPUT = 200       # 计划中每步约 200 token 输出
    ARCHITECT_BASE_INPUT = 800         # 系统提示 + 固定开销

    @staticmethod
    def resolve_model(model_name: str) -> str:
        """解析模型名称（支持别名）。"""
        model_name = model_name.lower().strip()
        return MODEL_ALIASES.get(model_name, model_name)

    @staticmethod
    def get_model_cost(model_name: str) -> Dict[str, float]:
        """获取模型成本信息。"""
        resolved = CostEstimator.resolve_model(model_name)
        if resolved in COST_TABLE:
            return COST_TABLE[resolved]
        # 未知模型的保守估计
        return {"input": 0.005, "output": 0.01, "description": f"Unknown: {model_name}"}

    @staticmethod
    def estimate_architect_tokens(task: str, num_steps: int, extra_context: str = "") -> Tuple[int, int]:
        """估算 Architect 阶段的 token 用量。

        Returns:
            (input_tokens, output_tokens)
        """
        task_chars = len(task) + len(extra_context)
        input_tokens = int(CostEstimator.ARCHITECT_BASE_INPUT +
                           task_chars * CostEstimator.TOKENS_PER_TASK_CHAR_INPUT)
        output_tokens = int(num_steps * CostEstimator.TOKENS_PER_STEP_OUTPUT + 300)
        return input_tokens, output_tokens

    @staticmethod
    def estimate_editor_tokens(steps: List[PlanStep]) -> Tuple[int, int]:
        """估算 Editor 阶段的 token 用量。

        Returns:
            (input_tokens, output_tokens)
        """
        input_tokens = 0
        output_tokens = 0
        for step in steps:
            if step.action in ("edit", "create_file"):
                input_tokens += CostEstimator.TOKENS_PER_EDIT_STEP_INPUT
                output_tokens += CostEstimator.TOKENS_PER_EDIT_STEP_OUTPUT
            elif step.action in ("read_file", "search", "analysis"):
                input_tokens += 200
                output_tokens += 150
            elif step.action == "shell":
                input_tokens += 150
                output_tokens += 100
            elif step.action == "test":
                input_tokens += 400
                output_tokens += 200
            else:
                input_tokens += 100
                output_tokens += 50
        return input_tokens, output_tokens

    @staticmethod
    def calculate(model_name: str, tokens: int, is_input: bool = True) -> float:
        """根据模型和 token 数计算成本。"""
        cost_info = CostEstimator.get_model_cost(model_name)
        key = "input" if is_input else "output"
        return (tokens / 1000) * cost_info.get(key, 0.005)

    @staticmethod
    def estimate_plan(plan: "ArchitectPlan", architect_model: str,
                      editor_model: str) -> CostEstimate:
        """估算整个 plan 的总成本。"""
        arch_input, arch_output = CostEstimator.estimate_architect_tokens(
            plan.task, len(plan.plan), plan.analysis
        )
        editor_input, editor_output = CostEstimator.estimate_editor_tokens(plan.plan)

        arch_cost_in = CostEstimator.calculate(architect_model, arch_input, True)
        arch_cost_out = CostEstimator.calculate(architect_model, arch_output, False)
        editor_cost_in = CostEstimator.calculate(editor_model, editor_input, True)
        editor_cost_out = CostEstimator.calculate(editor_model, editor_output, False)

        return CostEstimate(
            architect_input_tokens=arch_input,
            architect_output_tokens=arch_output,
            editor_input_tokens=editor_input,
            editor_output_tokens=editor_output,
            architect_cost=round(arch_cost_in + arch_cost_out, 6),
            editor_cost=round(editor_cost_in + editor_cost_out, 6),
            total_cost=round(arch_cost_in + arch_cost_out + editor_cost_in + editor_cost_out, 6),
        )

    @staticmethod
    def format_cost(estimate: CostEstimate) -> str:
        """格式化成本信息为人类可读字符串。"""
        return (
            f"💰 成本估算:\n"
            f"  Architect ({estimate.architect_input_tokens:,} in / {estimate.architect_output_tokens:,} out): "
            f"${estimate.architect_cost:.6f}\n"
            f"  Editor    ({estimate.editor_input_tokens:,} in / {estimate.editor_output_tokens:,} out): "
            f"${estimate.editor_cost:.6f}\n"
            f"  ─────────────────────────\n"
            f"  总计:     ${estimate.total_cost:.6f}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 计划验证器 ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class PlanValidator:
    """验证计划的可行性。

    检查项:
    1. Schema 版本兼容性
    2. 步骤 action 是否合法
    3. 步骤序号是否连续
    4. 依赖关系是否有效
    5. 目标文件是否存在（对现有文件操作）
    6. files_to_touch 与 plan 中的 target 是否一致
    """

    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir).resolve()
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def validate(self, plan: ArchitectPlan) -> Tuple[bool, List[str], List[str]]:
        """验证计划。返回 (is_valid, errors, warnings)。"""
        self.warnings = []
        self.errors = []

        self._check_schema(plan)
        self._check_steps(plan)
        self._check_files(plan)
        self._check_dependencies(plan)

        return len(self.errors) == 0, self.errors, self.warnings

    def _check_schema(self, plan: ArchitectPlan) -> None:
        """检查 Schema 版本。"""
        if not plan.schema_version:
            self.errors.append("缺少 schema_version")
        elif plan.schema_version != PLAN_SCHEMA_VERSION:
            self.warnings.append(
                f"Schema 版本不匹配: 文件版本 {plan.schema_version}, "
                f"当前版本 {PLAN_SCHEMA_VERSION}"
            )

    def _check_steps(self, plan: ArchitectPlan) -> None:
        """检查步骤合法性。"""
        seen_steps = set()
        for step in plan.plan:
            # 检查 action 合法性
            if step.action not in VALID_ACTIONS:
                self.errors.append(
                    f"步骤 {step.step}: 非法 action '{step.action}'，"
                    f"合法值: {', '.join(sorted(VALID_ACTIONS))}"
                )
                continue

            # 检查重复步骤号
            if step.step in seen_steps:
                self.errors.append(f"步骤 {step.step}: 重复的步骤序号")
            seen_steps.add(step.step)

            # 文件操作必须有 target
            if step.action in FILE_ACTIONS and not step.target:
                self.errors.append(
                    f"步骤 {step.step} ({step.action}): 缺少 target 字段"
                )

            # 检查 intent 是否存在
            if not step.intent:
                self.warnings.append(f"步骤 {step.step}: 缺少 intent 说明")

    def _check_files(self, plan: ArchitectPlan) -> None:
        """检查文件引用。"""
        all_targets: set = set()
        for step in plan.plan:
            if step.target and step.action in FILE_ACTIONS:
                all_targets.add(step.target)

        # 检查 files_to_touch 中声明的文件
        for f in plan.files_to_touch:
            full_path = self.workdir / f
            if step.action == "create_file":
                # 创建操作：目标不应已存在（除非明确要覆盖）
                if full_path.exists():
                    self.warnings.append(f"要创建的文件已存在: {f}")
            elif step.action == "delete_file":
                if not full_path.exists():
                    self.warnings.append(f"要删除的文件不存在: {f}")

        # 检查 plan 中引用但未在 files_to_touch 中声明的文件
        for t in all_targets:
            if t not in plan.files_to_touch:
                self.warnings.append(
                    f"文件 '{t}' 在步骤中被引用但未在 files_to_touch 中声明"
                )

    def _check_dependencies(self, plan: ArchitectPlan) -> None:
        """检查步骤间依赖关系。"""
        step_numbers = {s.step for s in plan.plan}
        for step in plan.plan:
            if step.depends_on is not None:
                if step.depends_on == step.step:
                    self.errors.append(f"步骤 {step.step}: 不能依赖自身")
                elif step.depends_on not in step_numbers:
                    self.errors.append(
                        f"步骤 {step.step}: 依赖的步骤 {step.depends_on} 不存在"
                    )
                elif step.depends_on >= step.step:
                    self.warnings.append(
                        f"步骤 {step.step}: 依赖步骤 {step.depends_on} 在后面，"
                        f"可能需要调整顺序"
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 计划模板生成器 ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class PlanTemplateGenerator:
    """在无法调用 LLM 时，根据任务描述生成一个基础计划模板。

    这是一个本地启发式生成器，用于:
    1. 离线模式 / 无 API 时
    2. 快速生成计划草稿
    3. 作为 LLM 生成的 fallback
    """

    # 常见 bug 修复模式
    BUG_FIX_PATTERNS = [
        (r"修复\s*(.+?)\s*(?:的|中的)\s*bug", "bug_fix"),
        (r"fix\s+(.+?)\s*(?:bug|issue|error)", "bug_fix"),
        (r"(.+?)\s*(?:报错|出错|异常|崩溃)", "bug_fix"),
    ]

    # 功能添加模式
    FEATURE_PATTERNS = [
        (r"添加\s*(.+?)(?:功能|模块|组件)", "feature_add"),
        (r"实现\s*(.+?)(?:功能|接口|方法)", "feature_add"),
        (r"add\s+(.+?)(?:\s+feature|\s+support)", "feature_add"),
    ]

    # 重构模式
    REFACTOR_PATTERNS = [
        (r"重构\s*(.+?)(?:代码|模块|文件)", "refactor"),
        (r"优化\s*(.+?)(?:性能|代码|结构)", "refactor"),
        (r"refactor\s+(.+)", "refactor"),
    ]

    @classmethod
    def classify_task(cls, task: str) -> str:
        """分类任务类型。"""
        task_lower = task.lower()
        for pattern, task_type in cls.BUG_FIX_PATTERNS:
            if re.search(pattern, task_lower):
                return task_type
        for pattern, task_type in cls.FEATURE_PATTERNS:
            if re.search(pattern, task_lower):
                return task_type
        for pattern, task_type in cls.REFACTOR_PATTERNS:
            if re.search(pattern, task_lower):
                return task_type
        return "general"

    @classmethod
    def extract_file_references(cls, task: str) -> List[str]:
        """从任务描述中提取文件引用。"""
        # 匹配 .py, .js, .ts, .yaml, .json, .md 等文件扩展名
        # 允许文件名中包含点号（如 daily_replay_v9.7.py）
        file_pattern = r'([\w\-_/.]+\.(?:py|js|ts|jsx|tsx|yaml|yml|json|md|txt|toml|cfg|ini|sh))'
        matches = re.findall(file_pattern, task)
        return list(dict.fromkeys(matches))  # 去重保序

    @classmethod
    def generate(cls, task: str, workdir: str = ".") -> ArchitectPlan:
        """生成基础计划模板。"""
        task_type = cls.classify_task(task)
        files = cls.extract_file_references(task)
        workdir_path = Path(workdir)

        plan = ArchitectPlan(
            task=task,
            analysis=f"任务类型: {task_type}\n涉及文件: {', '.join(files) if files else '未识别'}",
            workdir=str(workdir_path),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        if task_type == "bug_fix":
            plan.analysis += (
                "\n\n这是一个 bug 修复任务。建议先定位问题（read_file），"
                "然后进行针对性修复（edit），最后验证修复（test）。"
            )
            step_num = 1
            if files:
                for f in files:
                    full = workdir_path / f
                    if full.exists():
                        plan.plan.append(PlanStep(
                            step=step_num, action="read_file", target=f,
                            intent="理解现有逻辑，定位 bug 位置",
                            details=f"阅读 {f}，重点关注异常处理和边界条件"
                        ))
                        step_num += 1
            # 修复步骤
            target_file = files[0] if files else "待指定"
            plan.plan.append(PlanStep(
                step=step_num, action="edit", target=target_file,
                intent="修复 bug",
                details=f"根据分析结果修复 {target_file} 中的问题"
            ))
            step_num += 1
            # 验证步骤
            plan.plan.append(PlanStep(
                step=step_num, action="test", target=target_file,
                intent="验证修复结果",
                details="运行相关测试，确认 bug 已修复且无回归"
            ))
            plan.files_to_touch = files

        elif task_type == "feature_add":
            plan.analysis += (
                "\n\n这是一个功能添加任务。建议先了解现有代码结构，"
                "然后创建/修改相关文件。"
            )
            step_num = 1
            for f in files:
                full = workdir_path / f
                if full.exists():
                    plan.plan.append(PlanStep(
                        step=step_num, action="read_file", target=f,
                        intent="理解现有代码结构和接口"
                    ))
                    step_num += 1
            plan.plan.append(PlanStep(
                step=step_num, action="create_file", target=files[0] if files else "new_module.py",
                intent="创建新功能模块",
                details=f"根据 {task} 的需求实现新功能"
            ))
            plan.files_to_touch = [files[0]] if files else ["new_module.py"]

        elif task_type == "refactor":
            plan.analysis += (
                "\n\n这是一个重构任务。建议逐文件进行重构，"
                "每次重构后运行测试确认无回归。"
            )
            step_num = 1
            for f in files:
                plan.plan.append(PlanStep(
                    step=step_num, action="read_file", target=f,
                    intent="审查现有代码"
                ))
                step_num += 1
            if files:
                plan.plan.append(PlanStep(
                    step=step_num, action="edit", target=files[0],
                    intent="执行重构",
                    details="重构代码以提高可维护性和可读性"
                ))
                step_num += 1
                plan.plan.append(PlanStep(
                    step=step_num, action="test", target=files[0],
                    intent="验证重构后无回归"
                ))
            plan.files_to_touch = files

        else:
            # 通用任务
            plan.analysis += "\n\n此任务类型未明确识别，采用通用处理流程。"
            if files:
                plan.plan.append(PlanStep(
                    step=1, action="read_file", target=files[0],
                    intent="了解任务背景"
                ))
                plan.plan.append(PlanStep(
                    step=2, action="analysis",
                    intent="分析并制定详细方案",
                    details=f"深入分析 {task}，确定具体执行方案"
                ))
                plan.files_to_touch = files

        plan.id = plan.generate_id()
        plan.total_steps = len(plan.plan)
        return plan


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 编辑器引擎 ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class EditorEngine:
    """Editor 阶段执行引擎。

    严格按计划执行，以 action 为最小操作单元。
    支持:
    - 步骤追踪: 实时更新进度
    - 回滚: 失败时回退到上一个快照
    - 断点续传: 从 resume_step 恢复
    - 依赖检查: 跳过依赖未满足的步骤
    """

    def __init__(self, workdir: str = ".", model: str = "omlx-qwen",
                 verbose: bool = True, dry_run: bool = False):
        self.workdir = Path(workdir).resolve()
        self.model = model
        self.verbose = verbose
        self.dry_run = dry_run
        self.snapshots: Dict[int, str] = {}  # step_number -> snapshot_dir
        self._snapshot_base: Optional[Path] = None

    # ── 日志 ──────────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "INFO") -> None:
        if self.verbose:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [{level}] {msg}", file=sys.stderr)

    # ── 快照管理 ──────────────────────────────────────────────────────────

    def _ensure_snapshot_dir(self) -> Path:
        if self._snapshot_base is None:
            self._snapshot_base = Path(tempfile.mkdtemp(prefix="hermes_architect_snapshots_"))
        return self._snapshot_base

    def _create_snapshot(self, step_num: int, plan: ArchitectPlan) -> Optional[str]:
        """为涉及的文件创建快照以便回滚。"""
        if self.dry_run:
            self._log(f"步骤 {step_num}: [DRY RUN] 跳过创建快照")
            return None

        snapshot_dir = self._ensure_snapshot_dir() / f"step_{step_num}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        files_saved = 0
        for f in plan.files_to_touch:
            src = self.workdir / f
            if src.exists():
                dst = snapshot_dir / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                files_saved += 1

        if files_saved > 0:
            self.snapshots[step_num] = str(snapshot_dir)
            self._log(f"步骤 {step_num}: 创建快照 ({files_saved} 个文件) → {snapshot_dir}")
        return str(snapshot_dir) if files_saved > 0 else None

    def _restore_snapshot(self, step_num: int) -> bool:
        """恢复到指定步骤的快照。"""
        snapshot_dir = self.snapshots.get(step_num)
        if not snapshot_dir:
            self._log(f"步骤 {step_num}: 无可用快照，无法回滚", "ERROR")
            return False

        snapshot_path = Path(snapshot_dir)
        if not snapshot_path.exists():
            self._log(f"步骤 {step_num}: 快照目录 {snapshot_dir} 不存在", "ERROR")
            return False

        restored = 0
        for f in snapshot_path.rglob("*"):
            if f.is_file():
                rel = f.relative_to(snapshot_path)
                dst = self.workdir / rel
                shutil.copy2(f, dst)
                restored += 1

        self._log(f"步骤 {step_num}: 从快照恢复 {restored} 个文件")
        # 清理后续步骤的快照
        for sn in list(self.snapshots.keys()):
            if sn > step_num:
                shutil.rmtree(self.snapshots[sn], ignore_errors=True)
                del self.snapshots[sn]

        return True

    def _cleanup_snapshots(self) -> None:
        """清理所有快照。"""
        if self._snapshot_base and self._snapshot_base.exists():
            shutil.rmtree(self._snapshot_base, ignore_errors=True)
            self._snapshot_base = None
            self.snapshots.clear()

    # ── 步骤执行 ──────────────────────────────────────────────────────────

    def execute_read_file(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """执行读文件操作。"""
        target = self.workdir / step.target
        if not target.exists():
            return False, f"文件不存在: {step.target}"

        try:
            content = target.read_text(encoding="utf-8")
            self._log(f"步骤 {step.step}: 读取 {step.target} ({len(content)} 字符)")
            return True, content
        except UnicodeDecodeError:
            return False, f"无法以 UTF-8 解码: {step.target}"
        except Exception as e:
            return False, f"读取失败: {e}"

    def execute_shell(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """执行 shell 命令。"""
        if self.dry_run:
            self._log(f"步骤 {step.step}: [DRY RUN] 跳过命令: {step.target}")
            return True, "[DRY RUN] 命令未执行"

        try:
            result = subprocess.run(
                step.target,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self.workdir),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"
            success = result.returncode == 0
            if not success:
                return False, f"命令退出码 {result.returncode}:\n{output}"
            self._log(f"步骤 {step.step}: 命令执行成功")
            return True, output
        except subprocess.TimeoutExpired:
            return False, "命令执行超时 (120s)"
        except Exception as e:
            return False, f"命令执行失败: {e}"

    def execute_search(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """执行搜索操作（使用 grep/rg）。"""
        import subprocess as sp
        try:
            cmd = ["rg", "--line-number", "--max-count=20", step.target, str(self.workdir)]
            result = sp.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip() or result.stderr.strip()
            self._log(f"步骤 {step.step}: 搜索 '{step.target}' → {len(output)} 字符")
            return True, output
        except FileNotFoundError:
            # rg 不可用，fallback 到 grep
            try:
                cmd = ["grep", "-rn", "--max-count=20", step.target, str(self.workdir)]
                result = sp.run(cmd, capture_output=True, text=True, timeout=30)
                return True, result.stdout.strip() or result.stderr.strip()
            except Exception as e:
                return False, f"搜索失败: {e}"
        except Exception as e:
            return False, f"搜索失败: {e}"

    def execute_test(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """执行测试步骤 — 尝试运行 pytest 或 Python 脚本。"""
        target_path = self.workdir / step.target if step.target else None

        if not step.target:
            return self.execute_shell(PlanStep(
                step=step.step, action="shell",
                target="python -m pytest -x --tb=short 2>&1 | head -100",
                intent="运行项目测试"
            ))

        if not target_path or not target_path.exists():
            return False, f"测试目标不存在: {step.target}"

        # 如果是 Python 文件
        if step.target.endswith(".py"):
            cmd = f"python -m pytest {step.target} -x --tb=short 2>&1 | head -100"
            return self.execute_shell(PlanStep(
                step=step.step, action="shell", target=cmd,
                intent=step.intent
            ))

        return self.execute_shell(step)

    def execute_analysis(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """执行分析步骤（本地分析，不调用 LLM）。

        分析已有步骤的输出，生成总结性输出。
        """
        self._log(f"步骤 {step.step}: 执行分析 — {step.intent}")
        analysis_lines = [
            f"# 分析: {step.intent}",
            f"详情: {step.details}",
            "",
            "分析结果: (此步骤为本地分析标记点，实际分析由 AI 在上下文中完成)",
        ]
        return True, "\n".join(analysis_lines)

    def execute_wait_approval(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """等待人工审批。"""
        self._log(f"步骤 {step.step}: ⏸ 等待审批 — {step.intent}")
        if step.details:
            self._log(f"  详情: {step.details}")

        if self.dry_run:
            return True, "[DRY RUN] 自动通过审批"

        # 交互模式下等待用户输入
        try:
            response = input(f"\n  ⏸ 步骤 {step.step} 需要审批。继续？[Y/n] ").strip().lower()
            if response in ("", "y", "yes"):
                return True, "已批准"
            else:
                return False, "用户拒绝"
        except (EOFError, KeyboardInterrupt):
            return False, "审批被中断"

    def execute_rollback_marker(self, step: PlanStep) -> Tuple[bool, Optional[str]]:
        """回滚标记点 — 在此创建快照作为回滚目标。"""
        self._log(f"步骤 {step.step}: 📍 回滚标记点 — {step.intent}")
        return True, f"回滚标记: step_{step.step}"

    # ── 主执行循环 ────────────────────────────────────────────────────────

    def _execute_single_step(self, step: PlanStep, plan: ArchitectPlan) -> bool:
        """执行单个步骤。"""
        step.status = StepStatus.RUNNING
        step.started_at = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()

        # 文件操作的快照
        if step.action in ("edit", "create_file", "delete_file", "move_file"):
            step.snapshot_path = self._create_snapshot(step.step, plan)

        # 分发到具体执行方法
        handlers = {
            "read_file": self.execute_read_file,
            "shell": self.execute_shell,
            "search": self.execute_search,
            "test": self.execute_test,
            "analysis": self.execute_analysis,
            "wait_approval": self.execute_wait_approval,
            "rollback_marker": self.execute_rollback_marker,
            # edit / create_file / delete_file / move_file 需要 LLM 执行
        }

        handler = handlers.get(step.action)
        if handler:
            success, output = handler(step)
        elif step.action in ("edit", "create_file", "delete_file", "move_file"):
            # 这些操作需要外部工具执行（如 patch 工具或 LLM）
            success, output = self._execute_file_operation(step, plan)
        else:
            success, output = False, f"未知 action: {step.action}"

        elapsed = time.monotonic() - start
        step.duration_ms = elapsed * 1000
        step.completed_at = datetime.now(timezone.utc).isoformat()
        step.output = output

        if success:
            step.status = StepStatus.COMPLETED
            plan.completed_steps += 1
            self._log(f"步骤 {step.step}: ✅ 完成 ({elapsed:.1f}s)")
        else:
            step.status = StepStatus.FAILED
            step.error = output
            plan.failed_steps += 1
            self._log(f"步骤 {step.step}: ❌ 失败 — {output}")

        return success

    def _execute_file_operation(self, step: PlanStep, plan: ArchitectPlan) -> Tuple[bool, Optional[str]]:
        """执行文件操作（edit/create/delete/move）。

        在实际集成中，这些操作会通过 Hermes 的 patch 工具或 LLM 调用完成。
        这里提供一个框架，展示执行路径。

        对于独立运行模式，edit 操作在 dry_run 时只是标记，
        实际写操作需要外部工具注入。
        """
        target = self.workdir / step.target if step.target else None

        if step.action == "create_file":
            if self.dry_run:
                return True, f"[DRY RUN] 将创建: {step.target}"
            # 如果没有外部工具注入，只能创建空文件
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 如果有 details，写入 details 作为初始内容
                content = step.details or f"# Created by Architect Mode\n# Intent: {step.intent}\n"
                target.write_text(content, encoding="utf-8")
                return True, f"已创建文件: {step.target}"
            return False, "缺少 target"

        elif step.action == "delete_file":
            if self.dry_run:
                return True, f"[DRY RUN] 将删除: {step.target}"
            if target and target.exists():
                target.unlink()
                return True, f"已删除文件: {step.target}"
            return False, f"文件不存在: {step.target}"

        elif step.action == "move_file":
            if self.dry_run:
                return True, f"[DRY RUN] 将移动: {step.target} → {step.details}"
            if target and target.exists():
                dest = self.workdir / step.details if step.details else None
                if dest:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(dest))
                    return True, f"已移动: {step.target} → {step.details}"
            return False, f"移动失败: {step.target}"

        elif step.action == "edit":
            # edit 操作需要 LLM 或 patch 工具
            # 在独立模式下，仅记录意图
            if self.dry_run:
                return True, f"[DRY RUN] 将在 {step.target} 中: {step.details}"
            self._log(
                f"步骤 {step.step}: ⚠ edit 操作需要 LLM 执行 — {step.target}: {step.intent}",
                "WARN"
            )
            # 检查文件是否存在
            if target and target.exists():
                return True, (
                    f"文件 {step.target} 已就绪，等待 LLM 执行编辑。\n"
                    f"意图: {step.intent}\n"
                    f"详情: {step.details}"
                )
            return False, f"要编辑的文件不存在: {step.target}"

        return False, f"不支持的文件操作: {step.action}"

    def execute(self, plan: ArchitectPlan) -> Tuple[PlanStatus, ArchitectPlan]:
        """执行完整计划。"""
        self._log(f"═══ 开始执行计划: {plan.id} ═══")
        self._log(f"任务: {plan.task}")
        self._log(f"模型: {self.model}")
        self._log(f"步骤数: {len(plan.plan)}")
        self._log(f"工作目录: {self.workdir}")

        plan.status = PlanStatus.EXECUTING
        plan.execution_started_at = datetime.now(timezone.utc).isoformat()
        plan.total_steps = len(plan.plan)
        plan.completed_steps = 0
        plan.failed_steps = 0

        total_start = time.monotonic()

        for i, step in enumerate(plan.plan):
            # 跳过已完成的步骤（断点续传）
            if i < plan.resume_step:
                continue

            # 检查依赖
            if step.depends_on is not None:
                dep_step = next((s for s in plan.plan if s.step == step.depends_on), None)
                if dep_step and dep_step.status == StepStatus.FAILED:
                    self._log(
                        f"步骤 {step.step}: ⏭ 跳过（依赖步骤 {step.depends_on} 失败）"
                    )
                    step.status = StepStatus.SKIPPED
                    step.error = f"依赖步骤 {step.depends_on} 已失败"
                    continue

            # 执行步骤
            success = self._execute_single_step(step, plan)

            # 回滚标记: 成功后创建快照作为回退点
            if step.action == "rollback_marker" and success:
                self._create_snapshot(step.step, plan)

            # 失败处理
            if not success and step.retry_count < step.max_retries - 1:
                self._log(
                    f"步骤 {step.step}: 重试 {step.retry_count + 1}/{step.max_retries}"
                )
                step.retry_count += 1
                success = self._execute_single_step(step, plan)

            if not success:
                # 尝试回滚
                self._log(f"步骤 {step.step}: 执行失败，尝试回滚...")
                plan.status = PlanStatus.ROLLING_BACK
                rollback_target = step.step - 1
                if rollback_target > 0:
                    if self._restore_snapshot(rollback_target):
                        plan.status = PlanStatus.ROLLED_BACK
                        self._log(f"步骤 {step.step}: 已回滚到步骤 {rollback_target}")
                else:
                    self._log("无可回滚的快照", "WARN")

                plan.execution_completed_at = datetime.now(timezone.utc).isoformat()
                plan.total_duration_ms = (time.monotonic() - total_start) * 1000
                return PlanStatus.FAILED, plan

            # 进度报告
            if self.verbose and (i + 1) % 5 == 0:
                pct = plan.progress() * 100
                self._log(f"进度: {plan.completed_steps}/{plan.total_steps} ({pct:.0f}%)")

        # 全部完成
        plan.status = PlanStatus.COMPLETED
        plan.execution_completed_at = datetime.now(timezone.utc).isoformat()
        plan.total_duration_ms = (time.monotonic() - total_start) * 1000

        self._cleanup_snapshots()

        self._log("═══ 计划执行完成 ═══")
        self._log(f"状态: {plan.status.value}")
        self._log(f"完成: {plan.completed_steps}/{plan.total_steps}")
        self._log(f"耗时: {plan.total_duration_ms / 1000:.1f}s")

        return PlanStatus.COMPLETED, plan


# ═══════════════════════════════════════════════════════════════════════════════
# ─── Architect 模式管理器 ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class ArchitectModeManager:
    """Architect/Editor 双模式的总管理器。

    负责:
    1. 计划生成（Architect 阶段）
    2. 计划验证
    3. 计划执行（Editor 阶段）
    4. 成本追踪
    5. 配置管理
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or self._load_default_config()

    @staticmethod
    def _load_default_config() -> Dict[str, Any]:
        """加载默认配置。"""
        default = {
            "architect_model": "deepseek-v4-pro",
            "editor_model": "omlx-qwen",
            "fallback_architect": "kimi-k2",
            "fallback_editor": "deepseek-flash",
            "max_retries": 3,
            "dry_run": False,
            "verbose": True,
            "auto_approve": False,
            "save_plan": True,
            "plan_dir": str(CONFIG_DIR / "plans"),
        }

        if ARCHITECT_CONFIG_PATH.exists():
            try:
                with open(ARCHITECT_CONFIG_PATH) as f:
                    user_config = json.load(f)
                default.update(user_config)
            except Exception:
                pass

        return default

    def save_config(self) -> None:
        """保存当前配置。"""
        ARCHITECT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ARCHITECT_CONFIG_PATH, "w") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    # ── 计划生成 ──────────────────────────────────────────────────────────

    def generate_plan(
        self,
        task: str,
        architect_model: Optional[str] = None,
        extra_context: str = "",
        workdir: str = ".",
    ) -> ArchitectPlan:
        """生成架构计划（Architect 阶段）。

        使用强模型分析任务，生成结构化计划。
        在 LLM 不可用时，使用 PlanTemplateGenerator 作为 fallback。
        """
        model = architect_model or self.config["architect_model"]
        resolved_model = CostEstimator.resolve_model(model)

        # 尝试通过 LLM 生成计划（如果 Hermes LLM 接口可用）
        plan = self._try_llm_plan(task, resolved_model, extra_context, workdir)

        # Fallback: 使用模板生成器
        if plan is None:
            print("⚠ LLM 计划生成不可用，使用启发式模板生成器", file=sys.stderr)
            plan = PlanTemplateGenerator.generate(task, workdir)

        # 填充元数据
        plan.architect_model = resolved_model
        plan.editor_model = CostEstimator.resolve_model(
            self.config.get("editor_model", "omlx-qwen")
        )
        plan.workdir = workdir
        plan.created_at = datetime.now(timezone.utc).isoformat()
        if not plan.id:
            plan.id = plan.generate_id()
        plan.total_steps = len(plan.plan)

        # 成本估算
        plan.estimated_cost = CostEstimator.estimate_plan(
            plan, resolved_model, plan.editor_model
        )

        return plan

    def _try_llm_plan(
        self, task: str, model: str, extra_context: str, workdir: str
    ) -> Optional[ArchitectPlan]:
        """尝试通过 LLM 生成计划。如果不可用返回 None。"""
        # 检查是否有可用的 LLM 工具
        # 在实际 Hermes 集成中，这里会调用 Hermes 的 LLM 接口
        # 当前独立模式返回 None 让 fallback 处理
        #
        # 提示词示例:
        prompt = self._build_architect_prompt(task, extra_context, workdir)

        # 尝试通过环境变量设置的 LLM 接口调用
        api_key = os.environ.get("HERMES_API_KEY") or os.environ.get("OPENAI_API_KEY")
        api_base = os.environ.get("HERMES_API_BASE") or os.environ.get("OPENAI_API_BASE")

        if not api_key:
            return None

        return self._call_llm_for_plan(prompt, model, api_key, api_base, task, workdir)

    def _build_architect_prompt(self, task: str, extra_context: str, workdir: str) -> str:
        """构建 Architect 阶段的提示词。"""
        return f"""你是一个资深软件架构师。你的任务是分析问题并制定详细的修复/实现计划。

## 任务
{task}

## 额外上下文
{extra_context if extra_context else "无"}

## 工作目录
{workdir}

## 输出要求
请输出一个严格的 JSON 计划，格式如下:
```json
{{
  "task": "原始任务描述",
  "analysis": "详细的问题分析和解决方案思路",
  "plan": [
    {{
      "step": 1,
      "action": "read_file|edit|create_file|delete_file|move_file|shell|test|search|analysis|wait_approval|rollback_marker",
      "target": "文件路径或命令",
      "intent": "此步骤的目的",
      "details": "具体的执行细节",
      "expected_output": "期望的输出结果（可选）",
      "depends_on": null
    }}
  ],
  "files_to_touch": ["会被修改的文件列表"]
}}
```

## 重要规则
1. 第一个步骤通常是 read_file 了解现有代码
2. 每个 edit 步骤只修改一个文件
3. 修改后必须包含 test 验证步骤
4. 关键操作前添加 rollback_marker 以便失败时回滚
5. 只输出 JSON，不要输出其他内容

请生成计划:"""

    def _call_llm_for_plan(
        self, prompt: str, model: str, api_key: str, api_base: Optional[str],
        task: str, workdir: str,
    ) -> Optional[ArchitectPlan]:
        """调用 LLM API 生成计划。"""
        import urllib.request
        import urllib.error

        base_url = (api_base or "https://api.deepseek.com").rstrip("/")
        url = f"{base_url}/chat/completions"

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个资深软件架构师。只输出 JSON，不输出其他内容。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]

            # 提取 JSON
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                plan_dict = json.loads(json_match.group())
                return self._parse_llm_plan(plan_dict, task, workdir, model)

        except Exception as e:
            print(f"⚠ LLM 计划生成失败: {e}", file=sys.stderr)

        return None

    def _parse_llm_plan(
        self, plan_dict: Dict[str, Any], task: str, workdir: str, model: str
    ) -> ArchitectPlan:
        """解析 LLM 返回的计划 JSON。"""
        plan = ArchitectPlan(
            task=plan_dict.get("task", task),
            analysis=plan_dict.get("analysis", ""),
            workdir=workdir,
            architect_model=model,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        for s in plan_dict.get("plan", []):
            step = PlanStep(
                step=s.get("step", 0),
                action=s.get("action", "analysis"),
                target=s.get("target", ""),
                intent=s.get("intent", ""),
                details=s.get("details", ""),
                expected_output=s.get("expected_output", ""),
                depends_on=s.get("depends_on"),
            )
            plan.plan.append(step)

        plan.files_to_touch = plan_dict.get("files_to_touch", [])
        plan.id = plan.generate_id()
        plan.total_steps = len(plan.plan)
        return plan

    # ── 计划执行 ──────────────────────────────────────────────────────────

    def execute_plan(
        self,
        plan: ArchitectPlan,
        editor_model: Optional[str] = None,
        workdir: Optional[str] = None,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> Tuple[PlanStatus, ArchitectPlan]:
        """执行计划（Editor 阶段）。"""
        model = editor_model or self.config["editor_model"]
        wd = workdir or plan.workdir or "."

        # 验证计划
        validator = PlanValidator(wd)
        is_valid, errors, warnings = validator.validate(plan)

        if warnings:
            for w in warnings:
                print(f"⚠ {w}", file=sys.stderr)

        if not is_valid:
            plan.status = PlanStatus.VALIDATION_FAILED
            for e in errors:
                print(f"❌ {e}", file=sys.stderr)
            return PlanStatus.VALIDATION_FAILED, plan

        plan.status = PlanStatus.READY
        if verbose:
            print(f"✅ 计划验证通过 ({len(plan.plan)} 个步骤)", file=sys.stderr)
            print(CostEstimator.format_cost(plan.estimated_cost), file=sys.stderr)

        # 执行
        engine = EditorEngine(
            workdir=wd,
            model=model,
            verbose=verbose,
            dry_run=dry_run,
        )

        status, updated_plan = engine.execute(plan)
        return status, updated_plan

    # ── 快捷方法 ──────────────────────────────────────────────────────────

    def auto(
        self,
        task: str,
        architect_model: Optional[str] = None,
        editor_model: Optional[str] = None,
        workdir: str = ".",
        dry_run: bool = False,
        verbose: bool = True,
    ) -> Tuple[PlanStatus, ArchitectPlan]:
        """一步完成：生成计划 → 验证 → 执行。"""
        print("🏗  Architect 阶段: 分析任务并生成计划...", file=sys.stderr)
        plan = self.generate_plan(
            task=task,
            architect_model=architect_model,
            workdir=workdir,
        )

        print(f"\n📋 计划已生成 ({len(plan.plan)} 个步骤)", file=sys.stderr)
        print(f"   ID: {plan.id}", file=sys.stderr)
        print(f"   分析: {plan.analysis[:200]}{'...' if len(plan.analysis) > 200 else ''}", file=sys.stderr)
        print(CostEstimator.format_cost(plan.estimated_cost), file=sys.stderr)

        if not self.config.get("auto_approve", False):
            print("\n📋 计划步骤预览:", file=sys.stderr)
            for s in plan.plan:
                print(f"   {s.step:2d}. [{s.action:15s}] {s.intent}", file=sys.stderr)

            try:
                response = input("\n▶ 执行此计划？[Y/n] ").strip().lower()
                if response not in ("", "y", "yes"):
                    print("已取消", file=sys.stderr)
                    return PlanStatus.DRAFT, plan
            except (EOFError, KeyboardInterrupt):
                print("已取消", file=sys.stderr)
                return PlanStatus.DRAFT, plan

        print("\n🔧 Editor 阶段: 执行计划...", file=sys.stderr)
        status, plan = self.execute_plan(
            plan=plan,
            editor_model=editor_model,
            workdir=workdir,
            dry_run=dry_run,
            verbose=verbose,
        )

        # 保存执行结果
        if self.config.get("save_plan", True):
            self._save_plan_report(plan)

        return status, plan

    def _save_plan_report(self, plan: ArchitectPlan) -> None:
        """保存计划执行报告。"""
        plan_dir = Path(self.config.get("plan_dir", str(CONFIG_DIR / "plans")))
        plan_dir.mkdir(parents=True, exist_ok=True)

        report_path = plan_dir / f"{plan.id}_{plan.status.value}.json"
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict_full(), f, indent=2, ensure_ascii=False)
            if self.config.get("verbose", True):
                print(f"📄 报告已保存: {report_path}", file=sys.stderr)
        except Exception as e:
            print(f"⚠ 报告保存失败: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════════
# ─── CLI ──────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_plan(args: argparse.Namespace) -> int:
    """plan 子命令: 生成架构计划。"""
    manager = ArchitectModeManager()

    plan = manager.generate_plan(
        task=args.task,
        architect_model=args.model,
        extra_context=args.context or "",
        workdir=args.workdir,
    )

    # 输出
    plan_dict = plan.to_dict()
    plan_dict["estimated_cost"] = plan.estimated_cost.to_dict()

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(plan_dict, f, indent=2, ensure_ascii=False)
        print(f"✅ 计划已保存到: {output_path}")
    else:
        print(json.dumps(plan_dict, indent=2, ensure_ascii=False))

    print(f"\n{CostEstimator.format_cost(plan.estimated_cost)}")

    # 预览步骤
    if args.verbose:
        print(f"\n📋 计划步骤 ({len(plan.plan)} 步):")
        for s in plan.plan:
            dep = f" (依赖步骤 {s.depends_on})" if s.depends_on else ""
            print(f"  {s.step:2d}. [{s.action:15s}] {s.target:30s} — {s.intent}{dep}")

    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """execute 子命令: 执行计划。"""
    manager = ArchitectModeManager()

    # 加载计划
    plan_path = Path(args.plan_file)
    if not plan_path.exists():
        print(f"❌ 计划文件不存在: {args.plan_file}", file=sys.stderr)
        return 1

    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_dict = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ 计划文件 JSON 解析失败: {e}", file=sys.stderr)
        return 1

    plan = ArchitectPlan.from_dict(plan_dict)

    # 执行
    status, plan = manager.execute_plan(
        plan=plan,
        editor_model=args.model,
        workdir=args.workdir or plan.workdir,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    # 输出结果
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict_full(), f, indent=2, ensure_ascii=False)
        print(f"📄 执行报告已保存到: {output_path}")

    if status == PlanStatus.COMPLETED:
        print(f"\n✅ 计划执行成功 ({plan.completed_steps}/{plan.total_steps} 步骤)")
        return 0
    elif status == PlanStatus.VALIDATION_FAILED:
        print("\n❌ 计划验证失败", file=sys.stderr)
        return 1
    else:
        print(f"\n❌ 计划执行失败 (状态: {status.value})", file=sys.stderr)
        return 1


def cmd_auto(args: argparse.Namespace) -> int:
    """auto 子命令: 一步完成 plan + execute。"""
    manager = ArchitectModeManager()

    status, plan = manager.auto(
        task=args.task,
        architect_model=args.architect,
        editor_model=args.editor,
        workdir=args.workdir,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict_full(), f, indent=2, ensure_ascii=False)
        print(f"📄 报告已保存到: {output_path}")

    if status == PlanStatus.COMPLETED:
        print(f"\n✅ 任务完成 ({plan.completed_steps}/{plan.total_steps} 步骤)")
        print(f"   耗时: {plan.total_duration_ms / 1000:.1f}s")
        print(CostEstimator.format_cost(plan.estimated_cost))
        return 0
    else:
        print(f"\n❌ 任务失败 (状态: {status.value})", file=sys.stderr)
        return 1


def cmd_validate(args: argparse.Namespace) -> int:
    """validate 子命令: 验证计划文件。"""
    plan_path = Path(args.plan_file)
    if not plan_path.exists():
        print(f"❌ 计划文件不存在: {args.plan_file}", file=sys.stderr)
        return 1

    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_dict = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
        return 1

    plan = ArchitectPlan.from_dict(plan_dict)
    validator = PlanValidator(args.workdir)
    is_valid, errors, warnings = validator.validate(plan)

    if errors:
        for e in errors:
            print(f"❌ {e}")
    if warnings:
        for w in warnings:
            print(f"⚠ {w}")

    if is_valid:
        print(f"✅ 计划有效 ({len(plan.plan)} 个步骤)")
        return 0
    else:
        print(f"❌ 计划无效 — {len(errors)} 个错误")
        return 1


def cmd_config(args: argparse.Namespace) -> int:
    """config 子命令: 管理配置。"""
    manager = ArchitectModeManager()

    if args.show:
        print(json.dumps(manager.config, indent=2, ensure_ascii=False))
        return 0

    if args.set_key and args.set_value is not None:
        # 解析值类型
        try:
            value = json.loads(args.set_value)
        except json.JSONDecodeError:
            value = args.set_value
        manager.config[args.set_key] = value
        manager.save_config()
        print(f"✅ 已设置: {args.set_key} = {value}")
        return 0

    if args.reset:
        manager.config = ArchitectModeManager._load_default_config.__func__(None)  # type: ignore
        manager.save_config()
        print("✅ 配置已重置为默认值")
        return 0

    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    """costs 子命令: 显示模型成本和成本估算。"""
    if args.model:
        model = CostEstimator.resolve_model(args.model)
        cost = CostEstimator.get_model_cost(model)
        print(f"模型: {model}")
        print(f"描述: {cost.get('description', 'N/A')}")
        print(f"输入: ${cost['input']:.4f}/1K tokens")
        print(f"输出: ${cost['output']:.4f}/1K tokens")
        print(f"延迟: ~{cost.get('avg_latency_ms', 'N/A')}ms")
        print(f"上下文: {cost.get('context_window', 'N/A'):,} tokens")
        return 0

    if args.task:
        # 估算任务的成本
        plan = PlanTemplateGenerator.generate(args.task, args.workdir or ".")
        estimate = CostEstimator.estimate_plan(
            plan, args.architect or "deepseek-v4-pro", args.editor or "omlx-qwen"
        )
        print(CostEstimator.format_cost(estimate))
        return 0

    # 显示所有模型成本
    print("💰 模型成本表:\n")
    print(f"{'模型':<20} {'输入$/1K':>10} {'输出$/1K':>10} {'延迟':>8} {'上下文':>10}")
    print("-" * 62)
    for name, info in COST_TABLE.items():
        if name.startswith("omlx"):
            cost_display = "免费"
        else:
            cost_display = f"${info['input']:.4f}"
        print(
            f"{name:<20} {cost_display:>10} ${info['output']:.4f}   "
            f"{info['avg_latency_ms']:>4}ms {info['context_window']:>8,}"
        )
    return 0


def build_cli() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="architect_mode",
        description="Hermes Architect Mode — Architect/Editor 双模式系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成计划
  python architect_mode.py plan "修复 daily_replay_v9.7.py 的 bug" --model deepseek-v4-pro --output plan.json

  # 执行计划
  python architect_mode.py execute plan.json --model omlx-qwen --workdir /path/to/project

  # 一步完成
  python architect_mode.py auto "添加日志功能" --architect deepseek-v4-pro --editor omlx-qwen

  # 验证计划
  python architect_mode.py validate plan.json

  # 成本估算
  python architect_mode.py costs --task "修复计算bug" --architect deepseek-v4-pro --editor deepseek-flash

  # 管理配置
  python architect_mode.py config --show
  python architect_mode.py config --set editor_model deepseek-flash
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # plan 子命令
    plan_parser = subparsers.add_parser("plan", help="生成架构计划 (Architect 阶段)")
    plan_parser.add_argument("task", help="任务描述")
    plan_parser.add_argument("--model", "-m", default="deepseek-v4-pro",
                             help="Architect 模型 (默认: deepseek-v4-pro)")
    plan_parser.add_argument("--context", "-c", default="",
                             help="额外上下文信息")
    plan_parser.add_argument("--output", "-o", default="",
                             help="输出计划文件路径 (JSON)")
    plan_parser.add_argument("--workdir", "-w", default=".",
                             help="工作目录 (默认: 当前目录)")
    plan_parser.add_argument("--verbose", "-v", action="store_true", default=True,
                             help="详细输出 (默认开启)")

    # execute 子命令
    exec_parser = subparsers.add_parser("execute", help="执行计划 (Editor 阶段)")
    exec_parser.add_argument("plan_file", help="计划文件路径 (JSON)")
    exec_parser.add_argument("--model", "-m", default="omlx-qwen",
                             help="Editor 模型 (默认: omlx-qwen)")
    exec_parser.add_argument("--workdir", "-w", default="",
                             help="工作目录 (默认: 计划中的 workdir)")
    exec_parser.add_argument("--output", "-o", default="",
                             help="执行报告输出路径")
    exec_parser.add_argument("--dry-run", action="store_true",
                             help="模拟执行，不实际修改文件")
    exec_parser.add_argument("--verbose", "-v", action="store_true", default=True,
                             help="详细输出")

    # auto 子命令
    auto_parser = subparsers.add_parser("auto", help="一步完成: 生成计划 + 执行")
    auto_parser.add_argument("task", help="任务描述")
    auto_parser.add_argument("--architect", "-a", default="deepseek-v4-pro",
                             help="Architect 模型 (默认: deepseek-v4-pro)")
    auto_parser.add_argument("--editor", "-e", default="omlx-qwen",
                             help="Editor 模型 (默认: omlx-qwen)")
    auto_parser.add_argument("--workdir", "-w", default=".",
                             help="工作目录")
    auto_parser.add_argument("--output", "-o", default="",
                             help="执行报告输出路径")
    auto_parser.add_argument("--dry-run", action="store_true",
                             help="模拟执行")
    auto_parser.add_argument("--verbose", "-v", action="store_true", default=True,
                             help="详细输出")

    # validate 子命令
    val_parser = subparsers.add_parser("validate", help="验证计划文件")
    val_parser.add_argument("plan_file", help="计划文件路径 (JSON)")
    val_parser.add_argument("--workdir", "-w", default=".",
                            help="工作目录 (用于检查文件存在性)")

    # costs 子命令
    costs_parser = subparsers.add_parser("costs", help="查看模型成本和成本估算")
    costs_parser.add_argument("--model", "-m", default="",
                              help="显示指定模型的成本")
    costs_parser.add_argument("--task", "-t", default="",
                              help="估算任务的成本")
    costs_parser.add_argument("--architect", "-a", default="deepseek-v4-pro",
                              help="Architect 模型")
    costs_parser.add_argument("--editor", "-e", default="omlx-qwen",
                              help="Editor 模型")
    costs_parser.add_argument("--workdir", "-w", default=".",
                              help="工作目录")

    # config 子命令
    config_parser = subparsers.add_parser("config", help="管理 Architect Mode 配置")
    config_parser.add_argument("--show", action="store_true",
                               help="显示当前配置")
    config_parser.add_argument("--set", dest="set_key", default="",
                               help="设置配置项的键名")
    config_parser.add_argument("--value", dest="set_value", default=None,
                               help="设置配置项的值 (JSON 格式)")
    config_parser.add_argument("--reset", action="store_true",
                               help="重置为默认配置")

    return parser


def main() -> int:
    """入口函数。"""
    parser = build_cli()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "plan": cmd_plan,
        "execute": cmd_execute,
        "auto": cmd_auto,
        "validate": cmd_validate,
        "config": cmd_config,
        "costs": cmd_costs,
    }

    handler = handlers.get(args.command)
    if handler:
        return handler(args)

    print(f"未知命令: {args.command}", file=sys.stderr)
    return 1


# ═══════════════════════════════════════════════════════════════════════════════
# ─── Hermes 集成接口 ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def get_slash_commands() -> Dict[str, str]:
    """返回可在 Hermes 中使用的 /architect 斜杠命令列表。"""
    return {
        "/architect plan <任务>": "生成架构计划",
        "/architect execute <计划文件>": "执行已有计划",
        "/architect auto <任务>": "一步完成分析和执行",
        "/architect validate <计划文件>": "验证计划文件",
        "/architect costs": "显示成本估算",
        "/architect config": "管理配置",
        "/architect resume <计划文件>": "从断点恢复执行",
    }


def run_slash_command(command: str, args_str: str, workdir: str = ".") -> Dict[str, Any]:
    """供 Hermes 内部调用的斜杠命令处理接口。

    Args:
        command: 斜杠命令子动作 (plan/execute/auto/validate/costs/config/resume)
        args_str: 参数字符串
        workdir: 工作目录

    Returns:
        {"ok": bool, "message": str, "data": dict}
    """
    manager = ArchitectModeManager()

    try:
        if command == "plan":
            plan = manager.generate_plan(task=args_str, workdir=workdir)
            return {"ok": True, "message": f"计划已生成 ({len(plan.plan)} 步)", "data": plan.to_dict()}

        elif command == "execute":
            plan_path = Path(args_str.strip())
            if not plan_path.exists():
                return {"ok": False, "message": f"计划文件不存在: {args_str}"}
            with open(plan_path) as f:
                plan = ArchitectPlan.from_dict(json.load(f))
            status, plan = manager.execute_plan(plan=plan, workdir=workdir)
            return {"ok": status == PlanStatus.COMPLETED,
                    "message": f"执行完成: {status.value}",
                    "data": plan.to_dict_full()}

        elif command == "auto":
            status, plan = manager.auto(task=args_str, workdir=workdir)
            return {"ok": status == PlanStatus.COMPLETED,
                    "message": f"任务完成: {status.value}",
                    "data": plan.to_dict_full()}

        elif command == "validate":
            plan_path = Path(args_str.strip())
            with open(plan_path) as f:
                plan = ArchitectPlan.from_dict(json.load(f))
            validator = PlanValidator(workdir)
            is_valid, errors, warnings = validator.validate(plan)
            return {"ok": is_valid, "message": f"验证{'通过' if is_valid else '失败'}",
                    "data": {"errors": errors, "warnings": warnings}}

        elif command == "costs":
            plan = PlanTemplateGenerator.generate(args_str or "general task", workdir)
            estimate = CostEstimator.estimate_plan(plan, "deepseek-v4-pro", "omlx-qwen")
            return {"ok": True, "message": CostEstimator.format_cost(estimate),
                    "data": estimate.to_dict()}

        elif command == "config":
            if args_str.strip() == "show":
                return {"ok": True, "message": "当前配置", "data": manager.config}
            return {"ok": False, "message": "使用 config --set key value 修改配置"}

        else:
            return {"ok": False, "message": f"未知命令: {command}"}

    except Exception as e:
        return {"ok": False, "message": f"执行失败: {e}"}


# ═══════════════════════════════════════════════════════════════════════════════
# ─── 入口 ─────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(main())