#!/usr/bin/env python3
"""
Swarm Skills — 对标 JiuwenSwarm (华为 openJiuwen)
多 Agent 协作模式封装为可复用的"团队 SOP"

CLI 用法：
  python3 swarm_skills.py create --name "..."   创建新 Swarm Skill
  python3 swarm_skills.py list                   列出所有 Swarm Skills
  python3 swarm_skills.py show --name "..."      查看详情
  python3 swarm_skills.py execute --name "..." --task "..."  执行
  python3 swarm_skills.py inject                 注入当前可用的 Swarm Skills 列表

存储：~/.hermes/swarm_skills/*.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 配置 ─────────────────────────────────────────────────
SWARM_SKILLS_DIR = Path.home() / ".hermes" / "swarm_skills"


def ensure_dir() -> Path:
    """确保目录存在"""
    SWARM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SWARM_SKILLS_DIR


# ─── 预置 Swarm Skills ────────────────────────────────────
PRESET_SKILLS = {
    "code-review-team": {
        "name": "code-review-team",
        "description": "代码审查团队：规划→实现→审查→测试",
        "agents": [
            {"role": "planner", "model": "local", "tools": ["planning", "search"]},
            {"role": "coder", "model": "finna-flash", "tools": ["terminal", "file"]},
            {"role": "reviewer", "model": "finna-pro", "tools": ["terminal", "search"]},
            {"role": "tester", "model": "local", "tools": ["terminal"]},
        ],
        "workflow": [
            {"step": 1, "agent": "planner", "action": "生成实现计划", "depends_on": []},
            {"step": 2, "agent": "coder", "action": "按计划实现", "depends_on": [1]},
            {"step": 3, "agent": "reviewer", "action": "审查代码", "depends_on": [2]},
            {"step": 4, "agent": "tester", "action": "运行测试", "depends_on": [2]},
        ],
        "communication": "shared_memory",
        "timeout_per_step": 300,
        "version": 1,
        "created_at": "2026-05-18T00:00:00Z",
        "preset": True,
    },
    "research-synthesis": {
        "name": "research-synthesis",
        "description": "研究综合团队：搜索→提取→总结→优化",
        "agents": [
            {"role": "searcher", "model": "local", "tools": ["search", "browser"]},
            {"role": "extractor", "model": "finna-flash", "tools": ["search", "file"]},
            {"role": "summarizer", "model": "finna-pro", "tools": ["file"]},
            {"role": "refiner", "model": "finna-pro", "tools": ["file", "search"]},
        ],
        "workflow": [
            {"step": 1, "agent": "searcher", "action": "搜索相关资料", "depends_on": []},
            {"step": 2, "agent": "extractor", "action": "提取关键信息", "depends_on": [1]},
            {"step": 3, "agent": "summarizer", "action": "生成综合报告", "depends_on": [2]},
            {"step": 4, "agent": "refiner", "action": "优化和润色报告", "depends_on": [3]},
        ],
        "communication": "shared_memory",
        "timeout_per_step": 600,
        "version": 1,
        "created_at": "2026-05-18T00:00:00Z",
        "preset": True,
    },
    "debug-triangle": {
        "name": "debug-triangle",
        "description": "调试铁三角：复现→分析→修复→验证",
        "agents": [
            {"role": "reproducer", "model": "local", "tools": ["terminal", "file"]},
            {"role": "analyzer", "model": "finna-pro", "tools": ["terminal", "search"]},
            {"role": "fixer", "model": "finna-flash", "tools": ["terminal", "file"]},
            {"role": "verifier", "model": "local", "tools": ["terminal"]},
        ],
        "workflow": [
            {"step": 1, "agent": "reproducer", "action": "复现问题", "depends_on": []},
            {"step": 2, "agent": "analyzer", "action": "分析根因", "depends_on": [1]},
            {"step": 3, "agent": "fixer", "action": "实施修复", "depends_on": [2]},
            {"step": 4, "agent": "verifier", "action": "验证修复", "depends_on": [3]},
        ],
        "communication": "shared_memory",
        "timeout_per_step": 300,
        "version": 1,
        "created_at": "2026-05-18T00:00:00Z",
        "preset": True,
    },
    "deploy-pipeline": {
        "name": "deploy-pipeline",
        "description": "部署流水线：检查→构建→测试→部署",
        "agents": [
            {"role": "checker", "model": "local", "tools": ["terminal", "file"]},
            {"role": "builder", "model": "finna-flash", "tools": ["terminal"]},
            {"role": "tester", "model": "finna-pro", "tools": ["terminal", "file"]},
            {"role": "deployer", "model": "local", "tools": ["terminal", "ssh"]},
        ],
        "workflow": [
            {"step": 1, "agent": "checker", "action": "代码检查与审计", "depends_on": []},
            {"step": 2, "agent": "builder", "action": "构建项目", "depends_on": [1]},
            {"step": 3, "agent": "tester", "action": "运行测试套件", "depends_on": [2]},
            {"step": 4, "agent": "deployer", "action": "部署到目标环境", "depends_on": [3]},
        ],
        "communication": "shared_memory",
        "timeout_per_step": 600,
        "version": 1,
        "created_at": "2026-05-18T00:00:00Z",
        "preset": True,
    },
}


def init_presets():
    """如果预设 skill 文件不存在，写入它们"""
    ensure_dir()
    for name, skill in PRESET_SKILLS.items():
        path = SWARM_SKILLS_DIR / f"{name}.json"
        if not path.exists():
            with open(path, "w", encoding="utf-8") as f:
                json.dump(skill, f, indent=2, ensure_ascii=False)


# ─── 加载与保存 ───────────────────────────────────────────
def load_skill(name: str) -> dict | None:
    """按名称加载 Swarm Skill"""
    init_presets()
    path = SWARM_SKILLS_DIR / f"{name}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_all_skills() -> list[dict]:
    """加载全部 Swarm Skills"""
    init_presets()
    skills = []
    if SWARM_SKILLS_DIR.exists():
        for fpath in sorted(SWARM_SKILLS_DIR.glob("*.json")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    skills.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    return skills


def save_skill(skill: dict):
    """保存 Swarm Skill 到文件"""
    ensure_dir()
    path = SWARM_SKILLS_DIR / f"{skill['name']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(skill, f, indent=2, ensure_ascii=False)


# ─── 工作流拓扑排序与校验 ─────────────────────────────────
def validate_workflow(workflow: list[dict], agent_roles: set[str]) -> list[str]:
    """校验工作流：依赖存在、无环、role 存在。返回错误列表。"""
    errors = []
    step_map = {s["step"]: s for s in workflow}

    for s in workflow:
        if s["agent"] not in agent_roles:
            errors.append(f"步骤 {s['step']}: agent '{s['agent']}' 不在 agents 中定义的角色里")
        for dep in s.get("depends_on", []):
            if dep not in step_map:
                errors.append(f"步骤 {s['step']}: 依赖步骤 {dep} 不存在")

    # 简单环检测 (DFS)
    visited = set()
    in_stack = set()

    def dfs(step_id):
        if step_id in in_stack:
            return True  # 有环
        if step_id in visited:
            return False
        visited.add(step_id)
        in_stack.add(step_id)
        if step_id in step_map:
            for dep in step_map[step_id].get("depends_on", []):
                if dfs(dep):
                    return True
        in_stack.discard(step_id)
        return False

    for s in workflow:
        if dfs(s["step"]):
            errors.append("工作流存在循环依赖")
            break

    return errors


# ─── CLI: create ──────────────────────────────────────────
def cmd_create(args):
    """交互式创建新的 Swarm Skill"""
    init_presets()

    name = args.name
    if not name:
        name = input("Skill 名称 (如 my-team): ").strip()

    if load_skill(name):
        print(json.dumps({"error": f"Skill '{name}' 已存在", "action": "use --name with a different name"}, ensure_ascii=False))
        sys.exit(1)

    description = args.description or input("描述: ").strip()

    # 收集 agents
    agents = []
    print("\n--- 定义团队成员 (空行结束) ---")
    while True:
        role = input("  角色名 (如 planner): ").strip()
        if not role:
            break
        model = input("  模型 (如 local / finna-flash / finna-pro): ").strip() or "local"
        tools_str = input("  工具 (逗号分隔, 如 search,terminal): ").strip()
        tools = [t.strip() for t in tools_str.split(",") if t.strip()] if tools_str else []
        agents.append({"role": role, "model": model, "tools": tools})
        print(f"  ✓ 已添加: {role} (model={model}, tools={tools})")

    if not agents:
        print(json.dumps({"error": "至少需要定义一个 agent"}, ensure_ascii=False))
        sys.exit(1)

    agent_roles = {a["role"] for a in agents}

    # 收集 workflow
    workflow = []
    print("\n--- 定义工作流步骤 (空行结束) ---")
    step_counter = 1
    while True:
        agent = input(f"  步骤 {step_counter} - Agent 角色 (可选: {', '.join(sorted(agent_roles))}): ").strip()
        if not agent:
            break
        if agent not in agent_roles:
            print(f"  ⚠ 角色 '{agent}' 未在 agents 中定义, 可用: {', '.join(sorted(agent_roles))}")
            continue
        action = input(f"  动作描述: ").strip()
        deps_str = input(f"  依赖步骤 (逗号分隔数字, 无依赖直接回车): ").strip()
        depends_on = [int(d.strip()) for d in deps_str.split(",") if d.strip().isdigit()] if deps_str else []
        workflow.append({"step": step_counter, "agent": agent, "action": action, "depends_on": depends_on})
        step_counter += 1

    if not workflow:
        print(json.dumps({"error": "至少需要定义一个工作流步骤"}, ensure_ascii=False))
        sys.exit(1)

    # 校验
    errors = validate_workflow(workflow, agent_roles)
    if errors:
        print(json.dumps({"error": "工作流校验失败", "details": errors}, ensure_ascii=False))
        sys.exit(1)

    communication = args.communication or input("\n通信模式 (shared_memory / message_queue): ").strip() or "shared_memory"
    timeout = args.timeout or input("每步超时秒数 (默认 300): ").strip() or "300"

    skill = {
        "name": name,
        "description": description,
        "agents": agents,
        "workflow": workflow,
        "communication": communication,
        "timeout_per_step": int(timeout),
        "version": 1,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "preset": False,
    }

    save_skill(skill)
    print(json.dumps({"status": "created", "name": name, "path": str(SWARM_SKILLS_DIR / f"{name}.json")}, ensure_ascii=False))


# ─── CLI: list ────────────────────────────────────────────
def cmd_list(args):
    """列出所有 Swarm Skills"""
    init_presets()
    skills = load_all_skills()

    if not skills:
        print(json.dumps({"skills": [], "count": 0, "message": "暂无 Swarm Skills，使用 create 创建"}, ensure_ascii=False))
        return

    summary = []
    for s in skills:
        summary.append({
            "name": s["name"],
            "description": s["description"],
            "agents_count": len(s.get("agents", [])),
            "workflow_steps": len(s.get("workflow", [])),
            "preset": s.get("preset", False),
            "version": s.get("version", 1),
            "created_at": s.get("created_at", "unknown"),
        })

    result = {"skills": summary, "count": len(summary)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: show ────────────────────────────────────────────
def cmd_show(args):
    """查看指定 Swarm Skill 的详情"""
    init_presets()
    skill = load_skill(args.name)

    if not skill:
        print(json.dumps({"error": f"Skill '{args.name}' 不存在，使用 list 查看可用 skills"}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(skill, ensure_ascii=False, indent=2))


# ─── CLI: execute ─────────────────────────────────────────
def cmd_execute(args):
    """模拟执行 Swarm Skill"""
    init_presets()
    skill = load_skill(args.name)

    if not skill:
        print(json.dumps({"error": f"Skill '{args.name}' 不存在"}, ensure_ascii=False))
        sys.exit(1)

    if not args.task:
        print(json.dumps({"error": "请提供 --task 参数"}, ensure_ascii=False))
        sys.exit(1)

    # 拓扑排序执行步骤
    workflow = skill["workflow"]
    # 按依赖关系排序
    step_map = {s["step"]: s for s in workflow}

    def topo_sort(steps):
        """拓扑排序工作流步骤"""
        indegree = {s["step"]: 0 for s in steps}
        for s in steps:
            for dep in s.get("depends_on", []):
                indegree[s["step"]] += 1
        queue = [s["step"] for s in steps if indegree[s["step"]] == 0]
        result = []
        while queue:
            queue.sort()
            sid = queue.pop(0)
            result.append(step_map[sid])
            # 找到依赖 sid 的步骤
            for s in steps:
                if sid in s.get("depends_on", []):
                    indegree[s["step"]] -= 1
                    if indegree[s["step"]] == 0:
                        queue.append(s["step"])
        return result

    ordered_steps = topo_sort(workflow)
    execution_log = []
    start_time = time.time()

    print(json.dumps({
        "status": "executing",
        "skill": skill["name"],
        "task": args.task,
        "communication": skill.get("communication", "shared_memory"),
        "timeout_per_step": skill.get("timeout_per_step", 300),
    }, ensure_ascii=False))

    for step in ordered_steps:
        step_start = time.time()
        agent_info = next((a for a in skill["agents"] if a["role"] == step["agent"]), None)

        # 模拟执行
        time.sleep(0.3)  # 模拟执行延迟

        elapsed = round(time.time() - step_start, 2)
        log_entry = {
            "step": step["step"],
            "agent": step["agent"],
            "model": agent_info["model"] if agent_info else "unknown",
            "action": step["action"],
            "depends_on": step.get("depends_on", []),
            "status": "completed",
            "elapsed_seconds": elapsed,
        }
        execution_log.append(log_entry)
        print(json.dumps(log_entry, ensure_ascii=False))

    total_elapsed = round(time.time() - start_time, 2)
    summary = {
        "status": "completed",
        "skill": skill["name"],
        "task": args.task,
        "total_steps": len(execution_log),
        "total_elapsed_seconds": total_elapsed,
        "steps": execution_log,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


# ─── CLI: inject ──────────────────────────────────────────
def cmd_inject(args):
    """注入当前可用的 Swarm Skills 列表到 prompt"""
    init_presets()
    skills = load_all_skills()

    if not skills:
        injection = "当前没有可用的 Swarm Skills。使用 `python3 swarm_skills.py create --name \"...\"` 创建。\n"
    else:
        lines = ["## 可用的 Swarm Skills（多 Agent 团队 SOP）", ""]
        for s in skills:
            agents_str = ", ".join(a["role"] for a in s.get("agents", []))
            workflow_str = " → ".join(
                f"{st['agent']}({st['action']})" for st in s.get("workflow", [])
            )
            lines.append(f"- **{s['name']}**: {s['description']}")
            lines.append(f"    团队: {agents_str}")
            lines.append(f"    工作流: {workflow_str}")
            lines.append(f"    通信: {s.get('communication', 'shared_memory')} | 超时: {s.get('timeout_per_step', 300)}s/步")
            lines.append("")

        lines.append("使用方式:")
        lines.append("- 通过 `swarm_skills.py execute --name <skill> --task \"...\"` 启动团队执行")
        lines.append("- 根据任务需求选择合适的 Swarm Skill")

        injection = "\n".join(lines)

    if args.prompt:
        # 输出为 prompt 片段
        print(injection)
    else:
        print(json.dumps({"injection": injection, "skills_count": len(skills)}, ensure_ascii=False))


# ─── CLI: delete ──────────────────────────────────────────
def cmd_delete(args):
    """删除 Swarm Skill（仅非预设）"""
    init_presets()
    skill = load_skill(args.name)

    if not skill:
        print(json.dumps({"error": f"Skill '{args.name}' 不存在"}, ensure_ascii=False))
        sys.exit(1)

    if skill.get("preset"):
        print(json.dumps({"error": f"预设 Skill '{args.name}' 不可删除", "hint": "预设 skills 内置于代码中"}, ensure_ascii=False))
        sys.exit(1)

    path = SWARM_SKILLS_DIR / f"{args.name}.json"
    path.unlink()
    print(json.dumps({"status": "deleted", "name": args.name}, ensure_ascii=False))


# ─── 主入口 ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Swarm Skills — 多 Agent 协作团队 SOP 管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 swarm_skills.py list
  python3 swarm_skills.py show --name code-review-team
  python3 swarm_skills.py execute --name code-review-team --task "审查 api.py"
  python3 swarm_skills.py inject
  python3 swarm_skills.py create --name my-team
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # create
    create_parser = subparsers.add_parser("create", help="创建新 Swarm Skill")
    create_parser.add_argument("--name", type=str, help="Skill 名称")
    create_parser.add_argument("--description", type=str, help="描述")
    create_parser.add_argument("--communication", type=str, help="通信模式")
    create_parser.add_argument("--timeout", type=int, help="每步超时(秒)")

    # list
    subparsers.add_parser("list", help="列出所有 Swarm Skills")

    # show
    show_parser = subparsers.add_parser("show", help="查看指定 Swarm Skill 详情")
    show_parser.add_argument("--name", type=str, required=True, help="Skill 名称")

    # execute
    exec_parser = subparsers.add_parser("execute", help="执行 Swarm Skill")
    exec_parser.add_argument("--name", type=str, required=True, help="Skill 名称")
    exec_parser.add_argument("--task", type=str, required=True, help="执行任务描述")

    # inject
    inject_parser = subparsers.add_parser("inject", help="注入 Swarm Skills 列表到 prompt")
    inject_parser.add_argument("--prompt", action="store_true", help="输出纯文本 prompt 片段（默认输出 JSON）")

    # delete
    delete_parser = subparsers.add_parser("delete", help="删除 Swarm Skill")
    delete_parser.add_argument("--name", type=str, required=True, help="Skill 名称")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "show": cmd_show,
        "execute": cmd_execute,
        "inject": cmd_inject,
        "delete": cmd_delete,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        print(json.dumps({"error": f"未知命令: {args.command}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()