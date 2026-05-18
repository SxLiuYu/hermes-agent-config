#!/usr/bin/env python3
"""
里程碑任务规划器 (Milestone Planner)

对标 Avenir-Web 论文（UCL/Princeton/Edinburgh）：
  - 自动分解复杂任务为 2-6 个可验证里程碑
  - 状态追踪：Pending → In Progress → Completed → Failed
  - 失败反思缓冲区：记录每步失败的教训（最多 10 条）
  - 每个里程碑附带明确的 Done 判定条件

存储：~/.hermes/milestones/{task_id}.json

用法:
  python3 milestone_planner.py plan --goal "修复 data pipeline 的 OOM 问题" --session-id xxx
  python3 milestone_planner.py status --task-id xxx
  python3 milestone_planner.py update --task-id xxx --milestone-id m1 --status completed
  python3 milestone_planner.py reflect --task-id xxx --milestone-id m2 --lesson "需要先检查内存限制"
  python3 milestone_planner.py show-buffer
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Constants ───────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
MILESTONES_DIR = HERMES_HOME / "milestones"
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
MAX_REFLECTION_BUFFER = 10

# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class DoneCriterion:
    """一个 Done 判定条件"""
    description: str
    met: bool = False


@dataclass
class Milestone:
    """单个里程碑"""
    id: str                          # m1, m2, m3...
    description: str
    status: str = "pending"          # pending | in_progress | completed | failed
    done_criteria: list = field(default_factory=list)   # list[str] | list[dict]
    lesson: str = ""                 # 失败/完成后的经验教训
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    failed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "done_criteria": self.done_criteria,
            "lesson": self.lesson,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "failed_at": self.failed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Milestone":
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            status=d.get("status", "pending"),
            done_criteria=d.get("done_criteria", []),
            lesson=d.get("lesson", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            completed_at=d.get("completed_at", ""),
            failed_at=d.get("failed_at", ""),
        )


@dataclass
class ReflectionItem:
    """失败反思缓冲区中的一条记录"""
    milestone_id: str
    lesson: str
    timestamp: str
    task_id: str

    def to_dict(self) -> dict:
        return {
            "milestone_id": self.milestone_id,
            "lesson": self.lesson,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
        }


@dataclass
class Task:
    """一个规划任务"""
    task_id: str
    goal: str
    session_id: str = ""
    milestones: list = field(default_factory=list)       # list[Milestone]
    reflection_buffer: list = field(default_factory=list) # list[dict] (ReflectionItem)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "session_id": self.session_id,
            "milestones": [m.to_dict() if isinstance(m, Milestone) else m for m in self.milestones],
            "reflection_buffer": self.reflection_buffer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        milestones = []
        for m in d.get("milestones", []):
            if isinstance(m, Milestone):
                milestones.append(m)
            else:
                milestones.append(Milestone.from_dict(m))
        return cls(
            task_id=d.get("task_id", ""),
            goal=d.get("goal", ""),
            session_id=d.get("session_id", ""),
            milestones=milestones,
            reflection_buffer=d.get("reflection_buffer", []),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_task_id(goal: str, session_id: str) -> str:
    """基于 goal + session_id + 时间戳 生成唯一 task_id"""
    raw = f"{goal}|{session_id}|{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _task_path(task_id: str) -> Path:
    return MILESTONES_DIR / f"{task_id}.json"


def _load_task(task_id: str) -> Optional[Task]:
    """从磁盘加载 task"""
    path = _task_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task.from_dict(data)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"⚠️  任务文件损坏: {e}", file=sys.stderr)
        return None


def _save_task(task: Task):
    """保存 task 到磁盘"""
    MILESTONES_DIR.mkdir(parents=True, exist_ok=True)
    task.updated_at = _now()
    path = _task_path(task.task_id)
    path.write_text(
        json.dumps(task.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_global_buffer() -> list:
    """加载全局反思缓冲区（跨任务汇总）"""
    buffer_path = MILESTONES_DIR / "_reflection_buffer.json"
    if not buffer_path.exists():
        return []
    try:
        return json.loads(buffer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def _save_global_buffer(items: list):
    """保存全局反思缓冲区"""
    MILESTONES_DIR.mkdir(parents=True, exist_ok=True)
    buffer_path = MILESTONES_DIR / "_reflection_buffer.json"
    # 只保留最近 10 条
    trimmed = items[-MAX_REFLECTION_BUFFER:]
    buffer_path.write_text(
        json.dumps(trimmed, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ─── FinnA API Call ───────────────────────────────────────────────────────────

def _get_api_key() -> str:
    """获取 FinnA API Key"""
    for env_var in ["FINNA_KEY", "FINNA_API_KEY", "FINNA_QWEN_KEY"]:
        key = os.environ.get(env_var, "")
        if key and key.startswith("app-"):
            return key
    return "app-6OzRGg93TfuDOny9NUnKMvQU"


def call_finna_decompose(goal: str, model: str = "deepseek-v4-pro") -> Optional[str]:
    """调用 FinnA API 将目标分解为里程碑"""
    api_key = _get_api_key()
    if not api_key:
        print("⚠️  没有可用的 FinnA API Key", file=sys.stderr)
        return None

    prompt = f"""你是一个任务规划专家。请将以下复杂任务分解为 2-6 个可验证的里程碑。

每个里程碑需要包含：
1. id: 序号标识（m1, m2, ...）
2. description: 清晰简洁的描述（一句话）
3. done_criteria: 明确的 Done 判定条件列表（2-4 条可验证的标准）

任务目标：
{goal}

请严格用以下 JSON 格式返回（不要其他内容）：
{{
  "milestones": [
    {{
      "id": "m1",
      "description": "里程碑描述",
      "done_criteria": ["判定条件1", "判定条件2", "判定条件3"]
    }}
  ]
}}"""

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except urllib.error.URLError as e:
        print(f"⚠️  FinnA API 网络错误: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"⚠️  FinnA API 错误: {e}", file=sys.stderr)
        return None


def _parse_milestones_from_llm(text: str) -> list:
    """从 LLM 返回中解析里程碑列表"""
    try:
        data = json.loads(text)
        return data.get("milestones", [])
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 块
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group())
            return data.get("milestones", [])
        except json.JSONDecodeError:
            pass

    # 尝试逐行解析
    milestones = []
    pattern = re.compile(
        r'(?:milestone|步骤|里程碑)\s*[#:]?\s*(\d+|[mM]\d+)[:：\s-]+(.+?)(?=milestone|步骤|里程碑|$)',
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    for idx, (num, desc) in enumerate(matches):
        m_id = f"m{num}" if num.isdigit() else num.lower()
        if len(desc.strip()) > 5:
            milestones.append({
                "id": m_id if m_id.startswith("m") else f"m{idx+1}",
                "description": desc.strip()[:200],
                "done_criteria": ["任务产出通过验证", "无阻塞错误"],
            })
    return milestones


# ─── Heuristic Decomposition ──────────────────────────────────────────────────

# 预定义的关键词模板（对标 Avenir-Web 论文的静态分解策略）
HEURISTIC_TEMPLATES = {
    "oom|内存|memory|out of memory": [
        {"id": "m1", "description": "定位 OOM 根因：分析内存分布与峰值", "done_criteria": ["确认 OOM 发生的具体位置/操作", "输出内存快照分析报告", "识别 top 3 内存消耗点"]},
        {"id": "m2", "description": "实施内存优化方案", "done_criteria": ["代码改动已提交", "峰值内存降低 50%+", "无回归测试失败"]},
        {"id": "m3", "description": "压力测试验证", "done_criteria": ["原触发 OOM 的输入量不再 OOM", "2x 数据量下稳定运行", "内存监控无异常峰值"]},
    ],
    "bug|修复|fix|debug": [
        {"id": "m1", "description": "复现 bug 并定位根因", "done_criteria": ["可稳定复现 bug", "定位到具体代码行/逻辑", "理解根因"]},
        {"id": "m2", "description": "编写修复代码并通过单元测试", "done_criteria": ["修复代码已提交", "新增/修改的测试通过", "回归测试全部通过"]},
        {"id": "m3", "description": "Code Review 与部署验证", "done_criteria": ["CR 通过", "在 staging 环境验证通过", "相关文档已更新"]},
    ],
    "api|接口|endpoint|rest": [
        {"id": "m1", "description": "定义 API 契约与数据结构", "done_criteria": ["OpenAPI/Swagger spec 完成", "请求/响应 schema 定义完成", "边界条件已明确"]},
        {"id": "m2", "description": "实现 API 核心逻辑", "done_criteria": ["所有 endpoint 实现完毕", "单元测试覆盖率 >80%", "错误处理完整"]},
        {"id": "m3", "description": "集成测试与文档", "done_criteria": ["集成测试通过", "API 文档上线", "性能基准测试完成"]},
    ],
    "pipeline|流水线|etl|data": [
        {"id": "m1", "description": "审计现有 pipeline 架构与瓶颈", "done_criteria": ["完成数据流图", "识别性能瓶颈", "量化当前吞吐量"]},
        {"id": "m2", "description": "重构/优化 pipeline 关键环节", "done_criteria": ["核心优化代码完成", "pipeline 可端到端运行", "数据正确性校验通过"]},
        {"id": "m3", "description": "基准测试与稳定性验证", "done_criteria": ["吞吐量提升 >2x", "7 天无失败运行", "监控告警已配置"]},
    ],
    "deploy|部署|release|上线": [
        {"id": "m1", "description": "准备部署物料与配置", "done_criteria": ["Docker image 构建通过", "配置文件审查完成", "回滚方案就绪"]},
        {"id": "m2", "description": "Staging 环境部署与冒烟测试", "done_criteria": ["Staging 部署成功", "冒烟测试全部通过", "性能指标正常"]},
        {"id": "m3", "description": "生产环境灰度发布", "done_criteria": ["灰度 10% 流量正常", "关键指标无劣化", "全量发布完成"]},
        {"id": "m4", "description": "发布后监控与总结", "done_criteria": ["24h 运行无异常", "发布复盘文档完成", "监控面板正常"]},
    ],
    "refactor|重构": [
        {"id": "m1", "description": "分析现有代码结构与依赖", "done_criteria": ["依赖图/模块关系图完成", "识别重构边界", "制定重构计划"]},
        {"id": "m2", "description": "实施核心重构", "done_criteria": ["目标模块重构完成", "接口兼容性保持", "测试全部通过"]},
        {"id": "m3", "description": "清理与文档更新", "done_criteria": ["废弃代码已移除", "文档已更新", "团队已完成知识传递"]},
    ],
    "test|测试|unittest|coverage": [
        {"id": "m1", "description": "评估当前测试覆盖与缺失", "done_criteria": ["测试覆盖率报告完成", "识别未覆盖的关键路径", "优先级排序完成"]},
        {"id": "m2", "description": "编写高优先级测试用例", "done_criteria": ["新增测试 >10 个", "覆盖率提升 >15%", "边界条件已覆盖"]},
        {"id": "m3", "description": "CI 集成与测试稳定性", "done_criteria": ["CI pipeline 包含新测试", "测试在 CI 中 5 次连续通过", "测试执行时间 < 限值"]},
    ],
    "docs|文档|readme|documentation": [
        {"id": "m1", "description": "审计现有文档完整性", "done_criteria": ["文档覆盖率报告完成", "识别缺失/过时部分", "整理文档清单"]},
        {"id": "m2", "description": "编写/更新核心文档", "done_criteria": ["README 更新完毕", "API 文档完成", "架构概述完成"]},
        {"id": "m3", "description": "审查与发布", "done_criteria": ["文档审查通过", "已部署到文档站点", "团队成员已确认可用"]},
    ],
}

# 通用回退模板
FALLBACK_TEMPLATE = [
    {"id": "m1", "description": "分析问题：调研现状与约束", "done_criteria": ["问题范围已明确", "现有方案/工具已评估", "约束条件已枚举"]},
    {"id": "m2", "description": "制定方案：设计解决策略", "done_criteria": ["方案设计文档完成", "关键决策点已论证", "风险点已识别"]},
    {"id": "m3", "description": "实施执行：按方案推进", "done_criteria": ["核心改动已完成", "无阻塞错误", "初步验证通过"]},
    {"id": "m4", "description": "验证总结：确认目标达成", "done_criteria": ["验收测试通过", "相关方确认完成", "经验教训已记录"]},
]


def heuristic_decompose(goal: str) -> list:
    """基于关键词匹配选择预定义模板"""
    goal_lower = goal.lower()
    for pattern, template in HEURISTIC_TEMPLATES.items():
        if re.search(pattern, goal_lower):
            return template
    return FALLBACK_TEMPLATE


# ─── LLM + Heuristic Hybrid ──────────────────────────────────────────────────

def decompose_goal(goal: str, use_llm: bool = True) -> list:
    """
    分解目标为里程碑列表。
    优先使用 LLM，fallback 到启发式。
    """
    if use_llm:
        print("🤖 正在调用 LLM 分解任务...")
        llm_response = call_finna_decompose(goal)
        if llm_response:
            milestones = _parse_milestones_from_llm(llm_response)
            if milestones and 2 <= len(milestones) <= 10:
                print(f"✅ LLM 分解成功: {len(milestones)} 个里程碑")
                return milestones
            else:
                print(f"⚠️  LLM 返回不符合预期（{len(milestones) if milestones else 0} 个里程碑），fallback 到启发式")

    print("🧠 使用启发式模板分解...")
    return heuristic_decompose(goal)


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_plan(goal: str, session_id: str, no_llm: bool = False) -> str:
    """
    创建任务并分解里程碑。
    """
    task_id = _generate_task_id(goal, session_id)
    now = _now()

    # 分解
    raw_milestones = decompose_goal(goal, use_llm=not no_llm)

    # 构建 Milestone 对象
    milestones = []
    for m in raw_milestones:
        ms = Milestone(
            id=m.get("id", f"m{len(milestones)+1}"),
            description=m.get("description", ""),
            status="pending",
            done_criteria=m.get("done_criteria", []),
            created_at=now,
            updated_at=now,
        )
        milestones.append(ms)

    # 加载全局反思缓冲区中的相关教训，注入到 milestone context
    global_buffer = _load_global_buffer()
    relevant_lessons = [
        item for item in global_buffer
        if any(kw in goal.lower() for kw in item.get("lesson", "").lower().split()[:3])
    ][:3]

    # 构建 task
    task = Task(
        task_id=task_id,
        goal=goal,
        session_id=session_id,
        milestones=milestones,
        reflection_buffer=[],
        created_at=now,
        updated_at=now,
    )

    _save_task(task)

    # 格式化输出
    lines = [
        f"🎯 任务已规划: {task_id}",
        f"📌 目标: {goal}",
        f"📋 里程碑 ({len(milestones)} 个):",
    ]
    for m in milestones:
        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "failed": "❌"}[m.status]
        lines.append(f"  {status_icon} [{m.id}] {m.description}")
        for i, dc in enumerate(m.done_criteria, 1):
            lines.append(f"       ✓{i} {dc}")

    if relevant_lessons:
        lines.append(f"\n💡 相关历史教训 (来自全局反思缓冲区):")
        for item in relevant_lessons:
            lines.append(f"  - [{item.get('task_id','?')[:8]}] {item.get('lesson','')}")

    return "\n".join(lines)


def cmd_status(task_id: str) -> str:
    """显示任务状态"""
    task = _load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"

    status_counts = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for m in task.milestones:
        ms = m if isinstance(m, Milestone) else Milestone.from_dict(m)
        status_counts[ms.status] = status_counts.get(ms.status, 0) + 1

    total = len(task.milestones)
    progress = status_counts["completed"] / total * 100 if total > 0 else 0

    lines = [
        f"🎯 任务: {task.task_id}",
        f"📌 目标: {task.goal}",
        f"📅 创建: {task.created_at}  更新: {task.updated_at}",
        f"📊 进度: {status_counts['completed']}/{total} ({progress:.0f}%)",
        f"   ⬜ Pending: {status_counts['pending']}  "
        f"🔄 In Progress: {status_counts['in_progress']}  "
        f"✅ Completed: {status_counts['completed']}  "
        f"❌ Failed: {status_counts['failed']}",
        "",
        "━" * 50,
    ]

    for m in task.milestones:
        ms = m if isinstance(m, Milestone) else Milestone.from_dict(m)
        icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "failed": "❌"}[ms.status]
        lines.append(f"  {icon} [{ms.id}] {ms.description} ({ms.status})")
        for i, dc in enumerate(ms.done_criteria, 1):
            lines.append(f"       ✓{i} {dc}")
        if ms.lesson:
            lines.append(f"       💬 教训: {ms.lesson}")
        if ms.completed_at:
            lines.append(f"       ⏱️  完成: {ms.completed_at}")
        if ms.failed_at:
            lines.append(f"       ⏱️  失败: {ms.failed_at}")

    # 显示任务级反思缓冲区
    if task.reflection_buffer:
        lines.append("")
        lines.append("━" * 50)
        lines.append(f"📝 反思缓冲区 ({len(task.reflection_buffer)} 条):")
        for item in task.reflection_buffer:
            lines.append(f"  [{item.get('milestone_id','?')}] {item.get('lesson','')} — {item.get('timestamp','')[:10]}")

    return "\n".join(lines)


def cmd_update(task_id: str, milestone_id: str, status: str) -> str:
    """更新里程碑状态"""
    valid_statuses = {"pending", "in_progress", "completed", "failed"}
    if status not in valid_statuses:
        return f"❌ 无效状态: {status}。有效值: {', '.join(valid_statuses)}"

    task = _load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"

    now = _now()
    found = False
    for i, m in enumerate(task.milestones):
        ms = m if isinstance(m, Milestone) else Milestone.from_dict(m)
        if ms.id == milestone_id:
            old_status = ms.status
            ms.status = status
            ms.updated_at = now

            if status == "completed":
                ms.completed_at = now
            elif status == "failed":
                ms.failed_at = now

            task.milestones[i] = ms
            found = True
            break

    if not found:
        return f"❌ 里程碑不存在: {milestone_id}（可用: {', '.join((m if isinstance(m, Milestone) else Milestone.from_dict(m)).id for m in task.milestones)}）"

    _save_task(task)

    icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "failed": "❌"}
    return f"{icon[status]} [{milestone_id}] 状态更新: {old_status} → {status}"


def cmd_reflect(task_id: str, milestone_id: str, lesson: str) -> str:
    """记录失败反思"""
    task = _load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"

    now = _now()

    # 更新对应 milestone 的 lesson
    ms_found = False
    for i, m in enumerate(task.milestones):
        ms = m if isinstance(m, Milestone) else Milestone.from_dict(m)
        if ms.id == milestone_id:
            ms.lesson = lesson
            ms.updated_at = now
            task.milestones[i] = ms
            ms_found = True
            break

    if not ms_found:
        return f"❌ 里程碑不存在: {milestone_id}"

    # 加到任务级反思缓冲区
    reflection_item = {
        "milestone_id": milestone_id,
        "lesson": lesson,
        "timestamp": now,
        "task_id": task_id,
    }
    task.reflection_buffer.append(reflection_item)

    # 限制任务级缓冲区大小
    if len(task.reflection_buffer) > MAX_REFLECTION_BUFFER:
        task.reflection_buffer = task.reflection_buffer[-MAX_REFLECTION_BUFFER:]

    _save_task(task)

    # 同时添加到全局反思缓冲区
    global_buffer = _load_global_buffer()
    global_buffer.append(reflection_item)
    _save_global_buffer(global_buffer)

    return f"💬 反思已记录: [{milestone_id}] {lesson}\n📋 任务级缓冲区: {len(task.reflection_buffer)}/{MAX_REFLECTION_BUFFER} 条\n🌐 全局缓冲区: {len(global_buffer)}/{MAX_REFLECTION_BUFFER} 条"


def cmd_show_buffer(task_id: Optional[str] = None) -> str:
    """显示反思缓冲区"""
    if task_id:
        task = _load_task(task_id)
        if not task:
            return f"❌ 任务不存在: {task_id}"
        return _format_buffer(task.reflection_buffer, f"任务 {task_id} 反思缓冲区")
    else:
        global_buffer = _load_global_buffer()
        return _format_buffer(global_buffer, "全局反思缓冲区")


def _format_buffer(buffer: list, title: str) -> str:
    if not buffer:
        return f"📝 {title}: （空）"

    lines = [f"📝 {title} ({len(buffer)} 条):", ""]
    for item in buffer:
        mid = item.get("milestone_id", "?")
        tid = item.get("task_id", "?")[:8]
        lesson = item.get("lesson", "")
        ts = item.get("timestamp", "")[:10]
        lines.append(f"  [{mid}] [{tid}] {lesson} — {ts}")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="里程碑任务规划器 — 对标 Avenir-Web 论文",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 milestone_planner.py plan --goal "修复 data pipeline 的 OOM 问题" --session-id dev1
  python3 milestone_planner.py plan --goal "新增用户认证 API" --no-llm
  python3 milestone_planner.py status --task-id abc123def456
  python3 milestone_planner.py update --task-id abc123 --milestone-id m1 --status completed
  python3 milestone_planner.py reflect --task-id abc123 --milestone-id m2 --lesson "需要先检查内存限制"
  python3 milestone_planner.py show-buffer
  python3 milestone_planner.py show-buffer --task-id abc123
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # plan
    plan_p = sub.add_parser("plan", help="规划任务并分解里程碑")
    plan_p.add_argument("--goal", required=True, help="任务目标描述")
    plan_p.add_argument("--session-id", default="default", help="会话 ID")
    plan_p.add_argument("--no-llm", action="store_true", help="仅使用启发式分解，不调用 LLM")

    # status
    status_p = sub.add_parser("status", help="查看任务状态")
    status_p.add_argument("--task-id", required=True, help="任务 ID")

    # update
    update_p = sub.add_parser("update", help="更新里程碑状态")
    update_p.add_argument("--task-id", required=True, help="任务 ID")
    update_p.add_argument("--milestone-id", required=True, help="里程碑 ID (如 m1)")
    update_p.add_argument("--status", required=True,
                          choices=["pending", "in_progress", "completed", "failed"],
                          help="新状态")

    # reflect
    reflect_p = sub.add_parser("reflect", help="记录失败反思")
    reflect_p.add_argument("--task-id", required=True, help="任务 ID")
    reflect_p.add_argument("--milestone-id", required=True, help="里程碑 ID")
    reflect_p.add_argument("--lesson", required=True, help="经验教训")

    # show-buffer
    buffer_p = sub.add_parser("show-buffer", help="显示反思缓冲区")
    buffer_p.add_argument("--task-id", default=None, help="任务 ID（不指定则显示全局缓冲区）")

    args = parser.parse_args()

    if args.command == "plan":
        print(cmd_plan(args.goal, args.session_id, no_llm=args.no_llm))
    elif args.command == "status":
        print(cmd_status(args.task_id))
    elif args.command == "update":
        print(cmd_update(args.task_id, args.milestone_id, args.status))
    elif args.command == "reflect":
        print(cmd_reflect(args.task_id, args.milestone_id, args.lesson))
    elif args.command == "show-buffer":
        print(cmd_show_buffer(args.task_id))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()