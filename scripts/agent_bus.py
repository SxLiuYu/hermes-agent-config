#!/usr/bin/env python3
"""
Agent Bus — 共享消息总线 + 任务表

对标 Claude Code Agent Teams 的三个核心能力：
  1. 共享任务表 — 带状态机和依赖追踪
  2. Agent 间直接通信 — 队友之间发消息，不只是上报给 lead
  3. 文件锁 — 防止多个 agent 同时编辑同一文件

SQLite 后端，零配置。用于升级 delegate_task 子 agent 的协作能力。

用法:
  # 创建团队任务
  python3 scripts/agent_bus.py task-create --team "refactor-auth" \\
    --title "重构 JWT 认证" --desc "从 Flask-JWT 迁移到 PyJWT"

  # 子 agent 认领任务
  python3 scripts/agent_bus.py task-claim --task "task-001" --agent "agent-3"

  # 子 agent 之间发消息
  python3 scripts/agent_bus.py msg-send --from "agent-3" --to "agent-5" \\
    --content "我需要 auth 模块的接口签名"

  # 检查未读消息
  python3 scripts/agent_bus.py msg-check --agent "agent-5"

  # 查看团队状态
  python3 scripts/agent_bus.py team-status --team "refactor-auth"
"""

import json
import sqlite3
import argparse
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
DB_PATH = HERMES_HOME / "state" / "agent_bus.sqlite"


def get_db() -> sqlite3.Connection:
    """获取数据库连接，自动初始化"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    """初始化表结构"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            team TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'pending'
                CHECK(status IN ('pending','in_progress','completed','blocked','cancelled')),
            assigned_to TEXT,
            depends_on TEXT,       -- 逗号分隔的 task ID
            created_by TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_team ON tasks(team, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(assigned_to, status);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'message'
                CHECK(msg_type IN ('message','question','answer','alert','result')),
            timestamp TEXT NOT NULL,
            read INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(to_agent, read);

        CREATE TABLE IF NOT EXISTS file_locks (
            path TEXT NOT NULL,
            agent TEXT NOT NULL,
            team TEXT,
            locked_at TEXT NOT NULL,
            PRIMARY KEY (path)
        );

        CREATE TABLE IF NOT EXISTS teams (
            name TEXT PRIMARY KEY,
            lead_agent TEXT,
            members TEXT,  -- JSON array of agent IDs
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()


# ========== Task Management ==========

def task_create(team: str, title: str, description: str = "",
                depends_on: str = "", created_by: str = "lead") -> str:
    """创建任务"""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO tasks (id, team, title, description, depends_on, created_by, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (task_id, team, title, description, depends_on, created_by, now),
    )
    conn.commit()
    conn.close()
    return task_id


def task_claim(task_id: str, agent: str) -> bool:
    """认领任务（带竞争保护）"""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "SELECT status, depends_on FROM tasks WHERE id=?", (task_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            print(f"❌ 任务 {task_id} 不存在")
            return False
        if row["status"] != "pending":
            conn.rollback()
            conn.close()
            print(f"❌ 任务 {task_id} 状态为 {row['status']}，无法认领")
            return False
        # 检查依赖
        if row["depends_on"]:
            deps = [d.strip() for d in row["depends_on"].split(",") if d.strip()]
            for dep in deps:
                dep_row = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (dep,)
                ).fetchone()
                if not dep_row or dep_row["status"] != "completed":
                    conn.rollback()
                    conn.close()
                    print(f"❌ 任务 {task_id} 依赖 {dep} 尚未完成")
                    return False
        # 认领
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE tasks SET status='in_progress', assigned_to=?, started_at=? WHERE id=?",
            (agent, now, task_id),
        )
        conn.execute("COMMIT")
        print(f"✅ Agent '{agent}' 认领了任务 '{task_id}'")
        return True
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"❌ 认领失败: {e}")
        return False
    finally:
        conn.close()


def task_complete(task_id: str, result: str = "") -> bool:
    """完成任务，自动解锁依赖此任务的其他任务"""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status='completed', completed_at=?, result=? WHERE id=?",
        (now, result, task_id),
    )
    # 解除阻塞依赖
    conn.execute(
        "UPDATE tasks SET status='pending' WHERE status='blocked' AND depends_on LIKE ?",
        (f"%{task_id}%",),
    )
    conn.commit()
    conn.close()
    print(f"✅ 任务 '{task_id}' 已标记完成")
    return True


def task_list(team: str, status: str = None) -> list:
    """列出团队任务"""
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE team=? AND status=? ORDER BY created_at",
            (team, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE team=? ORDER BY status, created_at",
            (team,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ========== Agent Messaging ==========

def msg_send(team: str, from_agent: str, to_agent: str,
             content: str, msg_type: str = "message") -> int:
    """发送消息"""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO messages (team, from_agent, to_agent, content, msg_type, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (team, from_agent, to_agent, content, msg_type, now),
    )
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def msg_check(agent: str, mark_read: bool = True) -> list:
    """检查未读消息"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE to_agent=? AND read=0 ORDER BY timestamp",
        (agent,),
    ).fetchall()
    if mark_read and rows:
        conn.execute(
            "UPDATE messages SET read=1 WHERE to_agent=? AND read=0",
            (agent,),
        )
        conn.commit()
    conn.close()
    return [dict(r) for r in rows]


# ========== File Locks ==========

def file_lock(path: str, agent: str, team: str = "") -> bool:
    """获取文件锁（原子操作）"""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("INSERT INTO file_locks (path, agent, team, locked_at) VALUES (?,?,?,?)",
                     (path, agent, team, now))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # 已被锁定
        row = conn.execute("SELECT agent, locked_at FROM file_locks WHERE path=?", (path,)).fetchone()
        print(f"🔒 文件 '{path}' 已被 Agent '{row['agent']}' 锁定 (since {row['locked_at']})")
        return False
    finally:
        conn.close()


def file_unlock(path: str, agent: str = None) -> bool:
    """释放文件锁"""
    conn = get_db()
    if agent:
        conn.execute("DELETE FROM file_locks WHERE path=? AND agent=?", (path, agent))
    else:
        conn.execute("DELETE FROM file_locks WHERE path=?", (path,))
    conn.commit()
    conn.close()
    return True


def file_is_locked(path: str) -> dict:
    """检查文件是否被锁定"""
    conn = get_db()
    row = conn.execute("SELECT * FROM file_locks WHERE path=?", (path,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ========== Team Management ==========

def team_create(name: str, lead_agent: str) -> bool:
    """创建团队"""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO teams (name, lead_agent, members, created_at) VALUES (?,?,?,?)",
        (name, lead_agent, json.dumps([lead_agent]), now),
    )
    conn.commit()
    conn.close()
    print(f"✅ 团队 '{name}' 已创建，Lead: {lead_agent}")
    return True


def team_status(name: str) -> dict:
    """查看团队状态"""
    conn = get_db()
    team = conn.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()
    if not team:
        conn.close()
        return None
    tasks = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks WHERE team=? GROUP BY status",
        (name,),
    ).fetchall()
    conn.close()

    task_counts = {r["status"]: r["cnt"] for r in tasks}
    total = sum(task_counts.values())

    return {
        "team": dict(team),
        "tasks": {
            "total": total,
            "pending": task_counts.get("pending", 0),
            "in_progress": task_counts.get("in_progress", 0),
            "completed": task_counts.get("completed", 0),
            "blocked": task_counts.get("blocked", 0),
        },
    }


def team_add_member(name: str, agent: str) -> bool:
    """添加团队成员"""
    conn = get_db()
    team = conn.execute("SELECT members FROM teams WHERE name=?", (name,)).fetchone()
    if not team:
        conn.close()
        print(f"❌ 团队 '{name}' 不存在")
        return False
    members = json.loads(team["members"])
    if agent not in members:
        members.append(agent)
        conn.execute("UPDATE teams SET members=? WHERE name=?", (json.dumps(members), name))
        conn.commit()
    conn.close()
    print(f"✅ Agent '{agent}' 加入团队 '{name}'")
    return True


# ========== CLI ==========

def main():
    parser = argparse.ArgumentParser(description="Agent Bus — 共享消息总线 + 任务表")
    sub = parser.add_subparsers(dest="command")

    # 团队管理
    tc = sub.add_parser("team-create", help="创建团队")
    tc.add_argument("--name", required=True)
    tc.add_argument("--lead", required=True)

    ts = sub.add_parser("team-status", help="团队状态")
    ts.add_argument("--name", required=True)

    ta = sub.add_parser("team-add", help="添加成员")
    ta.add_argument("--name", required=True)
    ta.add_argument("--agent", required=True)

    # 任务
    tcr = sub.add_parser("task-create", help="创建任务")
    tcr.add_argument("--team", required=True)
    tcr.add_argument("--title", required=True)
    tcr.add_argument("--desc", default="")
    tcr.add_argument("--depends-on", default="")
    tcr.add_argument("--by", default="lead")

    tcl = sub.add_parser("task-claim", help="认领任务")
    tcl.add_argument("--task", required=True)
    tcl.add_argument("--agent", required=True)

    tco = sub.add_parser("task-complete", help="完成任务")
    tco.add_argument("--task", required=True)
    tco.add_argument("--result", default="")

    tl = sub.add_parser("task-list", help="列出任务")
    tl.add_argument("--team", required=True)
    tl.add_argument("--status", default=None)

    # 消息
    ms = sub.add_parser("msg-send", help="发送消息")
    ms.add_argument("--team", required=True)
    ms.add_argument("--from", dest="from_agent", required=True)
    ms.add_argument("--to", dest="to_agent", required=True)
    ms.add_argument("--content", required=True)
    ms.add_argument("--type", dest="msg_type", default="message")

    mc = sub.add_parser("msg-check", help="检查未读消息")
    mc.add_argument("--agent", required=True)
    mc.add_argument("--no-mark-read", action="store_true")

    # 文件锁
    fl = sub.add_parser("file-lock", help="锁定文件")
    fl.add_argument("--path", required=True)
    fl.add_argument("--agent", required=True)
    fl.add_argument("--team", default="")

    fu = sub.add_parser("file-unlock", help="解锁文件")
    fu.add_argument("--path", required=True)
    fu.add_argument("--agent", default=None)

    fic = sub.add_parser("file-check", help="检查文件锁")
    fic.add_argument("--path", required=True)

    args = parser.parse_args()

    if args.command == "team-create":
        team_create(args.name, args.lead)
    elif args.command == "team-status":
        status = team_status(args.name)
        if status:
            t = status["tasks"]
            members = json.loads(status["team"]["members"])
            print(f"\n👥 团队: {args.name}  Lead: {status['team']['lead_agent']}")
            print(f"   成员: {', '.join(members)}")
            print(f"\n📋 任务: 总计 {t['total']}")
            print(f"   ⏳ pending: {t['pending']}  🔄 in_progress: {t['in_progress']}")
            print(f"   ✅ completed: {t['completed']}  🚫 blocked: {t['blocked']}")
        else:
            print(f"❌ 团队 '{args.name}' 不存在")
    elif args.command == "team-add":
        team_add_member(args.name, args.agent)

    elif args.command == "task-create":
        task_create(args.team, args.title, args.desc, args.depends_on, args.by)
    elif args.command == "task-claim":
        task_claim(args.task, args.agent)
    elif args.command == "task-complete":
        task_complete(args.task, args.result)
    elif args.command == "task-list":
        tasks = task_list(args.team, args.status)
        if not tasks:
            print("(无任务)")
        for t in tasks:
            icon = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "blocked": "🚫"}.get(t["status"], "❓")
            assignee = t.get("assigned_to", "-")
            print(f"  {icon} [{t['status']}] {t['id']}: {t['title']} (→ {assignee})")

    elif args.command == "msg-send":
        msg_id = msg_send(args.team, args.from_agent, args.to_agent, args.content, args.msg_type)
        print(f"✉️  消息 #{msg_id} 已发送: {args.from_agent} → {args.to_agent}")
    elif args.command == "msg-check":
        messages = msg_check(args.agent, mark_read=not args.no_mark_read)
        if not messages:
            print("📭 无未读消息")
        for m in messages:
            print(f"  ✉️  [{m['msg_type']}] {m['from_agent']} → {m['to_agent']}")
            print(f"     {m['content'][:100]}")

    elif args.command == "file-lock":
        file_lock(args.path, args.agent, args.team)
    elif args.command == "file-unlock":
        file_unlock(args.path, args.agent)
    elif args.command == "file-check":
        lock = file_is_locked(args.path)
        if lock:
            print(f"🔒 {args.path}: locked by {lock['agent']} at {lock['locked_at']}")
        else:
            print(f"✅ {args.path}: 未锁定")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()