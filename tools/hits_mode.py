#!/usr/bin/env python3
"""
HITS 模式 — 对标 JiuwenSwarm HITS (Human in the Swarm)
人不只是指挥官，也可以作为团队成员参与多 Agent 协作。

CLI 用法：
  python3 hits_mode.py join --role "reviewer"      人加入团队担任某角色
  python3 hits_mode.py leave                       人退出团队
  python3 hits_mode.py contribute --message "..."  以成员身份提交贡献
  python3 hits_mode.py status                      当前人在团队中的状态
  python3 hits_mode.py inject                      注入 HITS 模式提醒到 prompt
  python3 hits_mode.py switch --mode "hits"        HOTS ↔ HITS 模式切换

状态文件：~/.hermes/hits/state.json
角色：planner / reviewer / decider / observer
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 配置 ─────────────────────────────────────────────────
HITS_DIR = Path.home() / ".hermes" / "hits"
STATE_FILE = HITS_DIR / "state.json"
CONTRIBUTIONS_DIR = HITS_DIR / "contributions"

# 支持的角色及其描述
AVAILABLE_ROLES = {
    "planner": "规划者 — 制定执行计划、分解任务、分配资源",
    "reviewer": "审查者 — 审查代码/文档/输出、提供反馈、把控质量",
    "decider": "决策者 — 在关键节点做决策、仲裁分歧、确定方向",
    "observer": "观察者 — 旁观团队协作、提供外部视角、不直接干预",
}

# 支持的模式
MODES = ["hots", "hits"]  # Human-in-the-Swarm / Human-Outside-the-Swarm


def ensure_dirs():
    """确保所有目录存在"""
    HITS_DIR.mkdir(parents=True, exist_ok=True)
    CONTRIBUTIONS_DIR.mkdir(parents=True, exist_ok=True)


def get_default_state() -> dict:
    """返回默认状态"""
    return {
        "mode": "hots",  # 默认指挥官模式
        "role": None,
        "joined_at": None,
        "active_session": None,
        "stats": {
            "contributions_count": 0,
            "sessions_joined": 0,
            "total_contributions": 0,
        },
        "history": [],
    }


def load_state() -> dict:
    """加载当前 HITS 状态。文件不存在或损坏时返回默认状态。"""
    ensure_dirs()
    if not STATE_FILE.exists():
        default = get_default_state()
        save_state(default)
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # 兼容旧格式：补充缺失字段
        default = get_default_state()
        for key in default:
            if key not in state:
                state[key] = default[key]
        return state
    except (json.JSONDecodeError, OSError):
        default = get_default_state()
        save_state(default)
        return default


def save_state(state: dict):
    """保存 HITS 状态到文件"""
    ensure_dirs()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def add_history(state: dict, event: str, details: dict | None = None):
    """记录历史事件"""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event,
        "details": details or {},
    }
    if "history" not in state:
        state["history"] = []
    state["history"].append(entry)
    # 保留最近 100 条
    if len(state["history"]) > 100:
        state["history"] = state["history"][-100:]


def save_contribution(role: str, message: str) -> Path:
    """保存贡献到文件"""
    ensure_dirs()
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    filename = f"{ts}_{role}.json"
    path = CONTRIBUTIONS_DIR / filename
    contribution = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "role": role,
        "message": message,
        "id": ts,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(contribution, f, indent=2, ensure_ascii=False)
    return path


# ─── CLI: join ────────────────────────────────────────────
def cmd_join(args):
    """人加入团队担任某角色"""
    state = load_state()

    if args.role not in AVAILABLE_ROLES:
        print(json.dumps({
            "error": f"无效角色 '{args.role}'",
            "available_roles": list(AVAILABLE_ROLES.keys()),
            "hint": "可用角色: " + ", ".join(AVAILABLE_ROLES.keys()),
        }, ensure_ascii=False))
        sys.exit(1)

    if state["role"] is not None:
        print(json.dumps({
            "error": f"你已经以 '{state['role']}' 角色在团队中",
            "action": "请先执行 leave 退出当前角色，再 join 新角色",
            "joined_at": state.get("joined_at"),
        }, ensure_ascii=False))
        sys.exit(1)

    now = datetime.utcnow().isoformat() + "Z"
    state["mode"] = "hits"
    state["role"] = args.role
    state["joined_at"] = now
    state["active_session"] = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    state["stats"]["sessions_joined"] = state["stats"].get("sessions_joined", 0) + 1

    add_history(state, "joined", {"role": args.role})

    save_state(state)

    result = {
        "status": "joined",
        "mode": "hits",
        "role": args.role,
        "role_description": AVAILABLE_ROLES[args.role],
        "joined_at": now,
        "message": f"你已以「{args.role}」角色加入团队。你现在是团队中的 {AVAILABLE_ROLES[args.role]}。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: leave ───────────────────────────────────────────
def cmd_leave(args):
    """人退出团队"""
    state = load_state()

    if state["role"] is None:
        print(json.dumps({
            "status": "not_in_team",
            "message": "你当前不在团队中。使用 join --role <role> 加入。",
        }, ensure_ascii=False))
        return

    previous_role = state["role"]
    joined_at = state.get("joined_at")

    add_history(state, "left", {"role": previous_role, "joined_at": joined_at})

    state["mode"] = "hots"
    state["role"] = None
    state["joined_at"] = None
    state["active_session"] = None

    save_state(state)

    result = {
        "status": "left",
        "mode": "hots",
        "previous_role": previous_role,
        "message": f"你已退出团队（原角色: {previous_role}）。现在回到指挥官模式 (HOTS)。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: contribute ──────────────────────────────────────
def cmd_contribute(args):
    """以团队成员身份提交贡献"""
    state = load_state()

    if state["role"] is None:
        print(json.dumps({
            "error": "你当前不在团队中，无法提交贡献",
            "action": "请先 join --role <role> 加入团队",
        }, ensure_ascii=False))
        sys.exit(1)

    message = args.message
    if not message:
        # 尝试从 stdin 读取
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print(json.dumps({"error": "请提供 --message 或通过管道输入内容"}, ensure_ascii=False))
            sys.exit(1)

    contrib_path = save_contribution(state["role"], message)

    state["stats"]["contributions_count"] = state["stats"].get("contributions_count", 0) + 1
    state["stats"]["total_contributions"] = state["stats"].get("total_contributions", 0) + 1

    add_history(state, "contributed", {
        "role": state["role"],
        "message_preview": message[:200] + ("..." if len(message) > 200 else ""),
        "file": str(contrib_path),
    })

    save_state(state)

    result = {
        "status": "contributed",
        "role": state["role"],
        "session": state.get("active_session"),
        "contribution_file": str(contrib_path),
        "message_length": len(message),
        "message": "你的贡献已提交到团队共享内存。Agent 队友可以看到它。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: status ──────────────────────────────────────────
def cmd_status(args):
    """查看当前人在团队中的状态"""
    state = load_state()

    if state["role"] is None:
        result = {
            "mode": state.get("mode", "hots"),
            "in_team": False,
            "role": None,
            "message": "你当前不在团队中。处于指挥官 (HOTS) 模式。",
            "stats": state.get("stats", {}),
            "available_roles": list(AVAILABLE_ROLES.keys()),
            "session_count": state.get("stats", {}).get("sessions_joined", 0),
        }
    else:
        result = {
            "mode": state.get("mode", "hits"),
            "in_team": True,
            "role": state["role"],
            "role_description": AVAILABLE_ROLES.get(state["role"], ""),
            "joined_at": state.get("joined_at"),
            "active_session": state.get("active_session"),
            "message": f"你正在以「{state['role']}」角色参与团队协作。",
            "stats": state.get("stats", {}),
            "contributions_this_session": state.get("stats", {}).get("contributions_count", 0),
            "available_roles": list(AVAILABLE_ROLES.keys()),
            "session_count": state.get("stats", {}).get("sessions_joined", 0),
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: switch ──────────────────────────────────────────
def cmd_switch(args):
    """HOTS ↔ HITS 模式切换"""
    state = load_state()

    target_mode = args.mode.lower()
    if target_mode not in MODES:
        print(json.dumps({
            "error": f"无效模式 '{args.mode}'",
            "available_modes": MODES,
        }, ensure_ascii=False))
        sys.exit(1)

    current_mode = state.get("mode", "hots")

    if target_mode == current_mode:
        print(json.dumps({
            "status": "unchanged",
            "mode": current_mode,
            "message": f"已经在 {current_mode.upper()} 模式下",
        }, ensure_ascii=False))
        return

    if target_mode == "hits":
        # 切换到 HITS 需要先 join
        if state["role"] is None:
            print(json.dumps({
                "error": "切换到 HITS 模式需要先加入团队",
                "action": "请先执行 join --role <role>",
            }, ensure_ascii=False))
            sys.exit(1)
        state["mode"] = "hits"
        add_history(state, "switched_to_hits")
    else:  # hots
        # 切换到 HOTS：保留角色信息但退出活跃模式
        state["mode"] = "hots"
        add_history(state, "switched_to_hots", {"was_role": state.get("role")})

    save_state(state)

    result = {
        "status": "switched",
        "from": current_mode.upper(),
        "to": target_mode.upper(),
        "role": state.get("role"),
        "message": f"模式已从 {current_mode.upper()} 切换到 {target_mode.upper()}",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ─── CLI: inject ──────────────────────────────────────────
def cmd_inject(args):
    """注入 HITS 模式提醒到 prompt"""
    state = load_state()

    if state["role"] is None or state.get("mode", "hots") == "hots":
        injection_lines = [
            "## 当前协作模式: HOTS (Human Outside the Swarm)",
            "",
            "用户是**总指挥官**，不在团队中。你作为 Agent 执行任务。",
            "如需用户加入团队参与协作，用户可执行: `python3 hits_mode.py join --role <role>`",
            "",
            "可选角色:",
        ]
        for role, desc in AVAILABLE_ROLES.items():
            injection_lines.append(f"  - **{role}**: {desc}")
    else:
        injection_lines = [
            f"## 当前协作模式: HITS (Human in the Swarm)",
            "",
            f"⚠️ **重要**: 用户当前是团队中的一员，担任 **{state['role']}** 角色。",
            f"角色说明: {AVAILABLE_ROLES.get(state['role'], '')}",
            "",
            "用户的消息不是指挥命令，而是**团队成员贡献**。",
            f"你应该把用户当作同行队友（角色: {state['role']}）来协作，而不是上级指挥官。",
            "",
            f"加入时间: {state.get('joined_at', 'unknown')}",
            f"本次贡献数: {state.get('stats', {}).get('contributions_count', 0)}",
            "",
            "协作提示:",
            "  - 与团队成员平等交流，不要把所有决策都推给用户",
            "  - 尊重用户的角色职责，用户会履行其角色任务",
            "  - 当需要角色专属判断时，可以请求对应角色成员（包括用户）的输入",
        ]

    injection = "\n".join(injection_lines)

    if args.prompt:
        print(injection)
    else:
        print(json.dumps({
            "injection": injection,
            "mode": state.get("mode", "hots"),
            "role": state.get("role"),
            "in_team": state.get("role") is not None,
        }, ensure_ascii=False))


# ─── CLI: activity ────────────────────────────────────────
def cmd_activity(args):
    """查看最近的团队活动历史"""
    state = load_state()
    history = state.get("history", [])

    if args.limit:
        history = history[-args.limit:]

    print(json.dumps({
        "mode": state.get("mode", "hots"),
        "role": state.get("role"),
        "total_events": len(state.get("history", [])),
        "events_shown": len(history),
        "history": history,
    }, ensure_ascii=False, indent=2))


# ─── CLI: reset ───────────────────────────────────────────
def cmd_reset(args):
    """重置 HITS 状态"""
    if not args.force:
        confirm = input("确定要重置所有 HITS 状态吗? (yes/no): ").strip()
        if confirm.lower() != "yes":
            print(json.dumps({"status": "cancelled", "message": "操作已取消"}, ensure_ascii=False))
            return

    default = get_default_state()
    save_state(default)

    # 清理贡献文件
    if CONTRIBUTIONS_DIR.exists():
        for f in CONTRIBUTIONS_DIR.glob("*.json"):
            f.unlink()

    print(json.dumps({"status": "reset", "message": "HITS 状态已重置为默认值"}, ensure_ascii=False))


# ─── 主入口 ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="HITS 模式 — Human in the Swarm 多人协作管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 hits_mode.py join --role reviewer
  python3 hits_mode.py status
  python3 hits_mode.py contribute --message "这个代码有 SQL 注入风险"
  python3 hits_mode.py inject
  python3 hits_mode.py switch --mode hots
  python3 hits_mode.py leave
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # join
    join_parser = subparsers.add_parser("join", help="加入团队担任角色")
    join_parser.add_argument(
        "--role", type=str, required=True,
        choices=list(AVAILABLE_ROLES.keys()),
        help=f"角色: {', '.join(AVAILABLE_ROLES.keys())}",
    )

    # leave
    subparsers.add_parser("leave", help="退出团队")

    # contribute
    contrib_parser = subparsers.add_parser("contribute", help="以成员身份提交贡献")
    contrib_parser.add_argument("--message", type=str, help="贡献内容")

    # status
    subparsers.add_parser("status", help="查看当前状态")

    # switch
    switch_parser = subparsers.add_parser("switch", help="HOTS ↔ HITS 模式切换")
    switch_parser.add_argument(
        "--mode", type=str, required=True,
        choices=MODES,
        help="目标模式: hots (指挥官) 或 hits (队员)",
    )

    # inject
    inject_parser = subparsers.add_parser("inject", help="注入 HITS 模式提醒到 prompt")
    inject_parser.add_argument("--prompt", action="store_true", help="输出纯文本 prompt 片段")

    # activity
    activity_parser = subparsers.add_parser("activity", help="查看团队活动历史")
    activity_parser.add_argument("--limit", type=int, help="限制显示条数")

    # reset
    reset_parser = subparsers.add_parser("reset", help="重置 HITS 状态")
    reset_parser.add_argument("--force", action="store_true", help="跳过确认直接重置")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "join": cmd_join,
        "leave": cmd_leave,
        "contribute": cmd_contribute,
        "status": cmd_status,
        "switch": cmd_switch,
        "inject": cmd_inject,
        "activity": cmd_activity,
        "reset": cmd_reset,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        print(json.dumps({"error": f"未知命令: {args.command}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()