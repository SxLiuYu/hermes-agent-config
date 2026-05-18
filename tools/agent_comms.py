#!/usr/bin/env python3
"""
Agent Communications Protocol — 多 Agent 协作通信协议
对标 CrewAI / AutoGen

功能：握手、消息传递、共识投票、心跳检测

CLI 用法：
  python3 agent_comms.py send --to "agent_x" --message "Hello"
  python3 agent_comms.py receive --from "agent_x"
  python3 agent_comms.py broadcast --message "Hello all"
  python3 agent_comms.py vote --proposal "deploy_v2" --vote yes
  python3 agent_comms.py tally --proposal-id "deploy_v2"
  python3 agent_comms.py status
  python3 agent_comms.py heartbeat                    # 发送心跳
  python3 agent_comms.py register --agent "agent_x"   # 注册新 agent
  python3 agent_comms.py deregister --agent "agent_x" # 注销 agent
  python3 agent_comms.py cleanup                      # 清理过期消息
"""

import argparse
import json
import os
import sys
import socket
import uuid
import time
from datetime import datetime, timezone, timedelta

# ── 路径配置 ──────────────────────────────────────────────
HERMES_DIR = os.path.expanduser("~/.hermes")
COMMS_DIR = os.path.join(HERMES_DIR, "agent_comms")
MESSAGES_FILE = os.path.join(COMMS_DIR, "messages.jsonl")
REGISTRY_FILE = os.path.join(COMMS_DIR, "registry.json")
VOTES_FILE = os.path.join(COMMS_DIR, "votes.jsonl")

MAX_MESSAGES = 200
HEARTBEAT_TIMEOUT_MINUTES = 5


# ── 工具函数 ──────────────────────────────────────────────

def ensure_dirs():
    """确保所有目录存在"""
    os.makedirs(COMMS_DIR, exist_ok=True)


def get_agent_id():
    """获取当前 agent 标识（默认使用主机名）"""
    return socket.gethostname()


def now_iso():
    """返回当前 ISO 时间戳"""
    return datetime.now(timezone.utc).isoformat()


def load_jsonl(filepath):
    """加载 JSONL 文件，返回 list[dict]"""
    if not os.path.exists(filepath):
        return []
    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def append_jsonl(filepath, record):
    """追加一条记录到 JSONL 文件"""
    ensure_dirs()
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_json(filepath, data):
    """保存 JSON 文件"""
    ensure_dirs()
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(filepath):
    """加载 JSON 文件，不存在则返回空字典"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def trim_messages():
    """裁剪消息到 MAX_MESSAGES 条（保留最新的）"""
    messages = load_jsonl(MESSAGES_FILE)
    if len(messages) > MAX_MESSAGES:
        messages = messages[-MAX_MESSAGES:]
        with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def update_heartbeat(agent_id):
    """更新 agent 心跳时间"""
    registry = load_json(REGISTRY_FILE)
    if agent_id not in registry:
        registry[agent_id] = {"status": "online", "last_heartbeat": None, "registered_at": now_iso()}
    registry[agent_id]["last_heartbeat"] = now_iso()
    registry[agent_id]["status"] = "online"
    save_json(REGISTRY_FILE, registry)


def check_heartbeats():
    """检查所有 agent 心跳，超时则标记 offline"""
    registry = load_json(REGISTRY_FILE)
    now = datetime.now(timezone.utc)
    timeout = timedelta(minutes=HEARTBEAT_TIMEOUT_MINUTES)
    changed = False

    for agent_id, info in registry.items():
        if info.get("last_heartbeat"):
            try:
                last = datetime.fromisoformat(info["last_heartbeat"])
                if now - last > timeout:
                    if info["status"] != "offline":
                        info["status"] = "offline"
                        changed = True
            except (ValueError, TypeError):
                pass

    if changed:
        save_json(REGISTRY_FILE, registry)
    return registry


def get_online_agents():
    """获取所有在线 agent 列表"""
    registry = check_heartbeats()
    return [name for name, info in registry.items() if info.get("status") == "online"]


# ── 发送消息 ──────────────────────────────────────────────

def cmd_send(args):
    """发送消息给指定 agent"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    msg = {
        "id": str(uuid.uuid4())[:8],
        "from": agent_id,
        "to": args.to,
        "message": args.message,
        "timestamp": now_iso(),
        "type": "message"
    }
    append_jsonl(MESSAGES_FILE, msg)
    trim_messages()
    print(json.dumps({"status": "sent", "message_id": msg["id"], "to": args.to}, ensure_ascii=False))


# ── 接收消息 ──────────────────────────────────────────────

def cmd_receive(args):
    """接收来自指定 agent 的消息"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    messages = load_jsonl(MESSAGES_FILE)

    # 过滤：发给当前 agent 或广播（to="*"）、且来自指定 agent
    filtered = []
    for msg in messages:
        if msg.get("from") == args.from_agent:
            to_field = msg.get("to", "")
            if to_field == agent_id or to_field == "*" or to_field == "all":
                filtered.append(msg)

    # 限制返回数量
    limit = args.limit or 50
    filtered = filtered[-limit:]

    print(json.dumps({"status": "ok", "from": args.from_agent, "count": len(filtered), "messages": filtered}, ensure_ascii=False, indent=2))


# ── 广播消息 ──────────────────────────────────────────────

def cmd_broadcast(args):
    """广播消息给所有已注册 agent"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    msg = {
        "id": str(uuid.uuid4())[:8],
        "from": agent_id,
        "to": "*",
        "message": args.message,
        "timestamp": now_iso(),
        "type": "broadcast"
    }
    append_jsonl(MESSAGES_FILE, msg)
    trim_messages()

    online = get_online_agents()
    print(json.dumps({"status": "broadcast", "message_id": msg["id"], "online_agents": online, "count": len(online)}, ensure_ascii=False))


# ── 投票 ──────────────────────────────────────────────────

def cmd_vote(args):
    """对提案进行投票"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    vote = {
        "proposal_id": args.proposal,
        "voter": agent_id,
        "vote": args.vote,
        "timestamp": now_iso(),
        "type": "vote"
    }
    append_jsonl(VOTES_FILE, vote)
    print(json.dumps({"status": "voted", "proposal_id": args.proposal, "vote": args.vote, "voter": agent_id}, ensure_ascii=False))


# ── 计票 ──────────────────────────────────────────────────

def cmd_tally(args):
    """统计提案投票结果"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    votes = load_jsonl(VOTES_FILE)
    proposal_votes = [v for v in votes if v.get("proposal_id") == args.proposal_id]

    yes_count = sum(1 for v in proposal_votes if v.get("vote") == "yes")
    no_count = sum(1 for v in proposal_votes if v.get("vote") == "no")
    total = yes_count + no_count

    # 需要 >50% 同意
    if total == 0:
        passed = False
        result = "no_votes"
    elif yes_count > total / 2:
        passed = True
        result = "passed"
    else:
        passed = False
        result = "rejected"

    output = {
        "status": "ok",
        "proposal_id": args.proposal_id,
        "yes": yes_count,
        "no": no_count,
        "total": total,
        "threshold": ">50%",
        "passed": passed,
        "result": result,
        "voters": [v["voter"] for v in proposal_votes]
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 状态 ──────────────────────────────────────────────────

def cmd_status(args):
    """查看共享消息池和 agent 注册状态"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)

    registry = check_heartbeats()
    messages = load_jsonl(MESSAGES_FILE)
    votes = load_jsonl(VOTES_FILE)

    # 统计 agent 状态
    agents = {}
    for name, info in registry.items():
        agents[name] = {
            "status": info.get("status", "unknown"),
            "last_heartbeat": info.get("last_heartbeat"),
            "registered_at": info.get("registered_at")
        }

    # 统计消息
    message_count = len(messages)
    recent_messages = messages[-10:] if messages else []

    # 提案统计
    proposal_ids = set(v.get("proposal_id") for v in votes if v.get("proposal_id"))
    proposals = {}
    for pid in proposal_ids:
        pv = [v for v in votes if v.get("proposal_id") == pid]
        yes = sum(1 for v in pv if v.get("vote") == "yes")
        no = sum(1 for v in pv if v.get("vote") == "no")
        proposals[pid] = {"yes": yes, "no": no, "total": len(pv)}

    output = {
        "status": "ok",
        "current_agent": agent_id,
        "agents": agents,
        "online_count": sum(1 for a in agents.values() if a["status"] == "online"),
        "total_agents": len(agents),
        "message_count": message_count,
        "max_messages": MAX_MESSAGES,
        "recent_messages": recent_messages,
        "proposals": proposals
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 注册 ──────────────────────────────────────────────────

def cmd_register(args):
    """注册新 agent"""
    registry = load_json(REGISTRY_FILE)
    agent_name = args.register_agent

    if agent_name in registry:
        print(json.dumps({"status": "exists", "agent": agent_name, "message": "Agent 已存在，更新心跳"}, ensure_ascii=False))
    else:
        registry[agent_name] = {
            "status": "online",
            "last_heartbeat": now_iso(),
            "registered_at": now_iso()
        }
        print(json.dumps({"status": "registered", "agent": agent_name}, ensure_ascii=False))

    save_json(REGISTRY_FILE, registry)


# ── 注销 ──────────────────────────────────────────────────

def cmd_deregister(args):
    """注销 agent"""
    registry = load_json(REGISTRY_FILE)
    agent_name = args.deregister_agent

    if agent_name in registry:
        del registry[agent_name]
        save_json(REGISTRY_FILE, registry)
        print(json.dumps({"status": "deregistered", "agent": agent_name}, ensure_ascii=False))
    else:
        print(json.dumps({"status": "not_found", "agent": agent_name, "message": "Agent 不存在"}, ensure_ascii=False))


# ── 心跳 ──────────────────────────────────────────────────

def cmd_heartbeat(args):
    """发送心跳"""
    agent_id = args.agent or get_agent_id()
    update_heartbeat(agent_id)
    print(json.dumps({"status": "heartbeat", "agent": agent_id, "timestamp": now_iso()}, ensure_ascii=False))


# ── 清理 ──────────────────────────────────────────────────

def cmd_cleanup(args):
    """清理过期消息（保留最新 MAX_MESSAGES 条）"""
    trim_messages()

    # 清理过期离线 agent（超过 1 小时未心跳）
    registry = load_json(REGISTRY_FILE)
    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=1)
    to_remove = []

    for name, info in registry.items():
        if info.get("last_heartbeat"):
            try:
                last = datetime.fromisoformat(info["last_heartbeat"])
                if now - last > threshold:
                    to_remove.append(name)
            except (ValueError, TypeError):
                pass

    for name in to_remove:
        del registry[name]

    save_json(REGISTRY_FILE, registry)

    output = {
        "status": "cleaned",
        "messages_trimmed": True,
        "max_messages": MAX_MESSAGES,
        "agents_removed": to_remove,
        "agents_removed_count": len(to_remove)
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Agent 通信协议 — 多 Agent 协作消息传递与投票",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s send --to agent_x --message "Hello"
  %(prog)s receive --from agent_x
  %(prog)s broadcast --message "All hands meeting"
  %(prog)s vote --proposal deploy --vote yes
  %(prog)s tally --proposal-id deploy
  %(prog)s status
  %(prog)s heartbeat
        """
    )
    parser.add_argument("--agent", "-a", help="当前 agent 名称（默认：主机名）")

    sub = parser.add_subparsers(dest="command", help="子命令")

    # send
    p_send = sub.add_parser("send", help="发送消息给指定 agent")
    p_send.add_argument("--to", required=True, help="目标 agent 名称")
    p_send.add_argument("--message", "-m", required=True, help="消息内容")

    # receive
    p_recv = sub.add_parser("receive", help="接收来自指定 agent 的消息")
    p_recv.add_argument("--from", dest="from_agent", required=True, help="发送方 agent 名称")
    p_recv.add_argument("--limit", "-n", type=int, default=50, help="最大返回条数（默认 50）")

    # broadcast
    p_bcast = sub.add_parser("broadcast", help="广播消息给所有 agent")
    p_bcast.add_argument("--message", "-m", required=True, help="广播内容")

    # vote
    p_vote = sub.add_parser("vote", help="对提案投票")
    p_vote.add_argument("--proposal", required=True, help="提案 ID")
    p_vote.add_argument("--vote", required=True, choices=["yes", "no"], help="投票：yes/no")

    # tally
    p_tally = sub.add_parser("tally", help="统计提案投票结果")
    p_tally.add_argument("--proposal-id", required=True, help="提案 ID")

    # status
    sub.add_parser("status", help="查看共享消息池和 agent 状态")

    # register
    p_reg = sub.add_parser("register", help="注册新 agent")
    p_reg.add_argument("--agent", dest="register_agent", required=True, help="要注册的 agent 名称")

    # deregister
    p_dereg = sub.add_parser("deregister", help="注销 agent")
    p_dereg.add_argument("--agent", dest="deregister_agent", required=True, help="要注销的 agent 名称")

    # heartbeat
    sub.add_parser("heartbeat", help="发送心跳信号")

    # cleanup
    sub.add_parser("cleanup", help="清理过期消息和离线 agent")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # 确保目录存在
    ensure_dirs()

    # 路由到对应处理函数
    commands = {
        "send": cmd_send,
        "receive": cmd_receive,
        "broadcast": cmd_broadcast,
        "vote": cmd_vote,
        "tally": cmd_tally,
        "status": cmd_status,
        "register": cmd_register,
        "deregister": cmd_deregister,
        "heartbeat": cmd_heartbeat,
        "cleanup": cmd_cleanup,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()