#!/usr/bin/env python3
"""
Human-in-Loop 安全门 (Human-in-Loop Safety Gate)
对标 Devin / Copilot — 高风险操作自动暂停，等待人类确认。

CLI 用法:
  python3 human_in_loop.py check --action "rm -rf /tmp/test"    检查是否需要安全门
  python3 human_in_loop.py gate   --action "..." --reason "..."  设置安全门
  python3 human_in_loop.py approve --gate-id "..."               确认通过
  python3 human_in_loop.py reject  --gate-id "..." --reason "..." 拒绝
  python3 human_in_loop.py inject                                 注入安全门提醒
  python3 human_in_loop.py list   [--status pending]              列出安全门
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# 安全门存储目录
# ──────────────────────────────────────────────
GATES_DIR = Path.home() / ".hermes" / "gates"
GATES_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────
# 高风险操作定义（自动触发安全门）
# ──────────────────────────────────────────────
HIGH_RISK_ACTIONS = [
    {
        "id": "file_deletion",
        "name": "文件/目录删除",
        "patterns": [
            r"\brm\b",
            r"\bdel\b",
            r"\bunlink\b",
            r"\brmdir\b",
            r"\bRemove-Item\b",
            r"\bdelete\b.*\bfile\b",
            r"\btrash\b",
        ],
        "risk_level": "critical",
        "description": "删除文件或目录操作",
    },
    {
        "id": "git_force_push",
        "name": "Git 强制推送",
        "patterns": [
            r"push\s+.*(--force|-f)",
            r"push\s+.*--force-with-lease",
            r"--force\b",
            r"git\s+push\s+-f\b",
        ],
        "risk_level": "critical",
        "description": "Git 强制推送可能覆盖远程历史",
    },
    {
        "id": "config_modification",
        "name": "修改配置文件",
        "patterns": [
            r"config\.(yaml|yml|json|toml|ini|conf)",
            r"\.env\b",
            r"\.gitconfig\b",
            r"\bdocker-compose\.(yml|yaml)\b",
            r"\bnginx\.conf\b",
            r"\bsystemd\b",
        ],
        "risk_level": "high",
        "description": "修改系统/应用配置文件",
    },
    {
        "id": "api_key_modification",
        "name": "修改密钥/Token",
        "patterns": [
            r"api[_-]?key",
            r"access[_-]?token",
            r"secret[_-]?key",
            r"private[_-]?key",
            r"credential",
            r"password",
            r"AWS_ACCESS",
            r"OPENAI_API_KEY",
        ],
        "risk_level": "critical",
        "description": "修改或写入 API Key / Token / 凭证",
    },
    {
        "id": "long_running_task",
        "name": "耗时任务（>5分钟）",
        "patterns": [
            r"npm\s+install",
            r"pip\s+install.*-r\s+requirements",
            r"docker\s+build",
            r"brew\s+install",
            r"apt-get\s+install",
            r"bundle\s+install",
            r"cargo\s+build",
            r"make\s+build",
            r"composer\s+install",
        ],
        "risk_level": "medium",
        "description": "可能耗时超过5分钟的任务",
    },
    {
        "id": "service_restart",
        "name": "服务重启",
        "patterns": [
            r"systemctl\s+restart",
            r"systemctl\s+reload",
            r"supervisorctl\s+restart",
            r"launchctl\s+unload",
            r"launchctl\s+load",
            r"brew\s+services\s+restart",
            r"docker\s+restart",
            r"\breboot\b",
        ],
        "risk_level": "high",
        "description": "重启系统服务可能导致服务中断",
    },
]

# ──────────────────────────────────────────────
# 安全门生命周期管理
# ──────────────────────────────────────────────

class GateStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


def _generate_gate_id() -> str:
    """生成唯一的安全门 ID。"""
    return f"gate_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _gate_path(gate_id: str) -> Path:
    """获取安全门文件路径。"""
    return GATES_DIR / f"{gate_id}.json"


def check_action(action: str) -> dict:
    """检查操作是否需要触发安全门。

    Returns:
        {"gated": bool, "risks": [...], "gate_id": str|None}
    """
    if not action:
        return {"gated": False, "risks": [], "gate_id": None}

    risks = []
    for risk_def in HIGH_RISK_ACTIONS:
        for pattern in risk_def["patterns"]:
            if re.search(pattern, action, re.IGNORECASE):
                risks.append({
                    "id": risk_def["id"],
                    "name": risk_def["name"],
                    "risk_level": risk_def["risk_level"],
                    "description": risk_def["description"],
                    "matched_pattern": pattern,
                })
                break  # 每个风险类别只记录一次

    return {
        "gated": len(risks) > 0,
        "risks": risks,
        "gate_id": None,  # check 只检测，不创建门
    }


def create_gate(action: str, reason: str = "", metadata: Optional[dict] = None) -> dict:
    """创建安全门，暂停操作等待人类确认。

    Returns:
        安全门信息 JSON
    """
    gate_id = _generate_gate_id()
    now = datetime.now(timezone.utc).isoformat()

    # 先检查风险
    check_result = check_action(action)
    if not check_result["gated"] and not reason:
        return {
            "gated": False,
            "gate_id": None,
            "message": "操作未匹配任何高风险规则，无需安全门",
            "risks": [],
        }

    gate_data = {
        "gate_id": gate_id,
        "status": GateStatus.PENDING,
        "created_at": now,
        "updated_at": now,
        "action": action,
        "reason": reason or "高风险操作自动触发安全门",
        "risks": check_result["risks"],
        "metadata": metadata or {},
        "approve_info": None,
        "reject_info": None,
    }

    _save_gate(gate_id, gate_data)

    return {
        "gated": True,
        "gate_id": gate_id,
        "message": "⛔ 安全门已激活，等待人工确认",
        "risks": check_result["risks"],
        "approval_required": True,
        "gate_data": gate_data,
    }


def _save_gate(gate_id: str, data: dict) -> None:
    """保存安全门状态到文件。"""
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    filepath = _gate_path(gate_id)
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _load_gate(gate_id: str) -> Optional[dict]:
    """加载安全门状态。"""
    filepath = _gate_path(gate_id)
    if not filepath.exists():
        return None
    try:
        return json.loads(filepath.read_text())
    except json.JSONDecodeError:
        return None


def approve_gate(gate_id: str, approver: str = "user", comment: str = "") -> dict:
    """批准安全门，允许操作继续。"""
    gate = _load_gate(gate_id)
    if gate is None:
        return {
            "success": False,
            "error": f"安全门不存在: {gate_id}",
            "gate_id": gate_id,
        }

    if gate["status"] != GateStatus.PENDING:
        return {
            "success": False,
            "error": f"安全门状态为 '{gate['status']}'，无法批准",
            "gate_id": gate_id,
            "current_status": gate["status"],
        }

    gate["status"] = GateStatus.APPROVED
    gate["approve_info"] = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approver": approver,
        "comment": comment,
    }
    _save_gate(gate_id, gate)

    return {
        "success": True,
        "message": "✅ 安全门已批准",
        "gate_id": gate_id,
        "status": GateStatus.APPROVED,
        "gate": gate,
    }


def reject_gate(gate_id: str, reason: str = "", approver: str = "user") -> dict:
    """拒绝安全门，阻止操作继续。"""
    gate = _load_gate(gate_id)
    if gate is None:
        return {
            "success": False,
            "error": f"安全门不存在: {gate_id}",
            "gate_id": gate_id,
        }

    if gate["status"] != GateStatus.PENDING:
        return {
            "success": False,
            "error": f"安全门状态为 '{gate['status']}'，无法拒绝",
            "gate_id": gate_id,
            "current_status": gate["status"],
        }

    gate["status"] = GateStatus.REJECTED
    gate["reject_info"] = {
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or "用户拒绝",
        "approver": approver,
    }
    _save_gate(gate_id, gate)

    return {
        "success": True,
        "message": "🚫 安全门已拒绝",
        "gate_id": gate_id,
        "status": GateStatus.REJECTED,
        "gate": gate,
    }


def list_gates(status: Optional[str] = None, limit: int = 50) -> dict:
    """列出安全门。"""
    gates = []
    if GATES_DIR.exists():
        for f in sorted(GATES_DIR.glob("gate_*.json"), reverse=True):
            if len(gates) >= limit:
                break
            try:
                gate = json.loads(f.read_text())
                if status and gate.get("status") != status:
                    continue
                gates.append({
                    "gate_id": gate["gate_id"],
                    "status": gate["status"],
                    "action": gate.get("action", "")[:100],
                    "created_at": gate.get("created_at", ""),
                    "risks": [r["name"] for r in gate.get("risks", [])],
                })
            except json.JSONDecodeError:
                continue

    return {
        "total": len(gates),
        "status_filter": status or "all",
        "gates": gates,
    }


# ──────────────────────────────────────────────
# Prompt 注入
# ──────────────────────────────────────────────

INJECTION_PROMPT = """## Human-in-Loop 安全门规则

在执行以下高风险操作之前，**必须暂停**并等待人类确认：
1. **文件删除** (rm, del, unlink, rmdir) — 不可逆操作
2. **Git 强制推送** (push --force / -f) — 可能覆盖远程历史
3. **修改配置文件** (config.yaml, .env, .gitconfig) — 影响系统行为
4. **修改密钥/Token** — 影响安全认证
5. **耗时任务** (>5分钟) — 通知用户等待
6. **服务重启** (systemctl restart, supervisorctl restart) — 可能导致服务中断

安全门流程：
- 检测到高风险操作 → 自动创建安全门
- 通知用户确认 → 等待 approve/reject
- approve → 继续执行 | reject → 安全拒绝并记录原因"""


def inject_prompt() -> str:
    """返回注入到 session_start 的安全门提醒 prompt。"""
    return INJECTION_PROMPT


def inject_json() -> str:
    """以 JSON 格式返回注入的安全门规则。"""
    return json.dumps({
        "type": "human_in_loop_injection",
        "high_risk_categories": [
            {"id": r["id"], "name": r["name"], "risk_level": r["risk_level"]}
            for r in HIGH_RISK_ACTIONS
        ],
        "prompt": INJECTION_PROMPT,
    }, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 集成辅助：便捷的 gate 命令（check + create）
# ──────────────────────────────────────────────

def gate_action(action: str, reason: str = "", auto_create: bool = True) -> dict:
    """检查并（如需要）创建安全门。

    这是 'gate' 子命令的逻辑：先 check，如果 gated 则自动创建。
    """
    check_result = check_action(action)

    if not check_result["gated"]:
        return {
            "gated": False,
            "gate_id": None,
            "message": "操作安全，无需安全门",
            "risks": [],
        }

    if auto_create:
        return create_gate(action, reason)
    else:
        check_result["message"] = "检测到高风险操作，需要创建安全门"
        return check_result


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Human-in-Loop 安全门 — 高风险操作自动暂停，等待人类确认",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 human_in_loop.py check  --action "rm -rf /tmp/test"
  python3 human_in_loop.py gate   --action "rm -rf /tmp/test"
  python3 human_in_loop.py approve --gate-id "gate_20240518_120000_abc12345"
  python3 human_in_loop.py reject  --gate-id "gate_20240518_120000_abc12345" --reason "危险操作"
  python3 human_in_loop.py inject
  python3 human_in_loop.py list
  python3 human_in_loop.py list --status pending
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # check
    check_p = sub.add_parser("check", help="检查操作是否需要安全门")
    check_p.add_argument("--action", required=True, help="要检查的操作/命令")

    # gate
    gate_p = sub.add_parser("gate", help="设置安全门（check + create）")
    gate_p.add_argument("--action", required=True, help="要门控的操作/命令")
    gate_p.add_argument("--reason", default="", help="触发安全门的原因")
    gate_p.add_argument("--no-auto", action="store_true", help="仅检查，不自动创建门")

    # approve
    approve_p = sub.add_parser("approve", help="确认通过安全门")
    approve_p.add_argument("--gate-id", required=True, help="安全门 ID")
    approve_p.add_argument("--comment", default="", help="批准备注")

    # reject
    reject_p = sub.add_parser("reject", help="拒绝安全门")
    reject_p.add_argument("--gate-id", required=True, help="安全门 ID")
    reject_p.add_argument("--reason", default="", help="拒绝原因")

    # inject
    sub.add_parser("inject", help="注入安全门提醒到 prompt")

    # list
    list_p = sub.add_parser("list", help="列出安全门")
    list_p.add_argument("--status", choices=["pending", "approved", "rejected", "expired"],
                        default=None, help="按状态过滤")
    list_p.add_argument("--limit", type=int, default=50, help="最大数量（默认50）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        result = check_action(args.action)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "gate":
        result = gate_action(args.action, reason=args.reason, auto_create=not args.no_auto)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "approve":
        result = approve_gate(args.gate_id, comment=args.comment)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "reject":
        result = reject_gate(args.gate_id, reason=args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "inject":
        print(inject_json())

    elif args.command == "list":
        result = list_gates(status=args.status, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()