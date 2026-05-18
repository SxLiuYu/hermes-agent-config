#!/usr/bin/env python3
"""
Session Memory Extractor — 从 session transcript 中提取关键信息
对标 Claude Code 的 session_memory.md 格式。

功能:
  1. 扫描 ~/.hermes/sessions/ 中的最近 session JSON 文件
  2. 解析 conversation messages，提取关键信息
  3. 生成/更新 session_memory.md 文件
  4. 支持增量更新（只处理新 session）

输出格式（Claude Code session_memory 风格）:
  # Session Title
  # Current State
  # Task specification
  # Files and Functions
  # Workflow
  # Errors & Corrections
  # Learnings
  # Key results
  # Worklog

命令行:
  python session_memory_extract.py run              # 运行一次提取
  python session_memory_extract.py run --session <id>  # 处理指定 session
  python session_memory_extract.py run --since 24h   # 处理最近 N 小时的 session
  python session_memory_extract.py status            # 查看状态
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

# ── 路径配置 ──────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SESSIONS_DIR = HERMES_HOME / "sessions"
SCRIPTS_DIR = HERMES_HOME / "scripts"
LOGS_DIR = HERMES_HOME / "logs"
MEMORY_FILE = HERMES_HOME / "session_memory.md"
STATE_FILE = HERMES_HOME / "session_memory_state.json"
EXTRACT_LOG = LOGS_DIR / "session_memory_extract.log"

# ── 文件操作相关工具名 ────────────────────────────────────────
FILE_TOOLS = {
    "write_file", "read_file", "patch", "search_files",
    "terminal", "write", "edit", "create", "delete",
}

# ── 错误模式 ──────────────────────────────────────────────────
ERROR_PATTERNS = [
    r"Traceback\s*\(most recent call last\)",
    r"\w+Error:",
    r"\w+Exception:",
    r"error[s]?\s*:\s*",
    r"failed",
    r"❌",
    r"FAILED",
]


def log(msg: str):
    """写日志"""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(EXTRACT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    """加载状态文件，跟踪已处理的 session"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_sessions": {}, "last_run": None}


def save_state(state: dict):
    """保存状态"""
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    # 只保留最近 100 个已处理的 session
    processed = state.get("processed_sessions", {})
    if len(processed) > 100:
        # 按时间排序，保留最新的 100 个
        sorted_keys = sorted(processed.keys(),
                             key=lambda k: processed[k].get("processed_at", ""),
                             reverse=True)[:100]
        state["processed_sessions"] = {k: processed[k] for k in sorted_keys}
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _is_user_session(filepath: Path) -> bool:
    """判断是否为用户发起的 session（非 cron 任务）"""
    name = filepath.name
    # cron session 文件名格式: session_cron_<hash>_<date>_<time>.json
    if name.startswith("session_cron_"):
        return False
    return True


def find_session_files(since: Optional[datetime] = None,
                       specific_session: Optional[str] = None,
                       max_sessions: int = 30) -> List[Path]:
    """查找需要处理的 session 文件
    
    优先级：用户 session > cron session
    限制：最多 max_sessions 个
    """
    if not SESSIONS_DIR.exists():
        return []

    all_files = sorted(SESSIONS_DIR.glob("session_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)

    if specific_session:
        return [f for f in all_files if specific_session in f.name]

    if since:
        cutoff = since.timestamp()
        all_files = [f for f in all_files if f.stat().st_mtime >= cutoff]
    else:
        # 默认：最近 24 小时
        cutoff = (datetime.now() - timedelta(hours=24)).timestamp()
        all_files = [f for f in all_files if f.stat().st_mtime >= cutoff]

    # 优先用户 session，然后补充 cron session
    user_sessions = [f for f in all_files if _is_user_session(f)]
    cron_sessions = [f for f in all_files if not _is_user_session(f)]

    # 用户 session 全取，cron session 采样（最多 5 个最新的）
    selected = user_sessions[:max_sessions]
    if len(selected) < max_sessions:
        selected.extend(cron_sessions[:max_sessions - len(selected)])

    return selected


def parse_session_file(filepath: Path) -> Optional[dict]:
    """解析 session JSON 文件"""
    try:
        data = json.loads(filepath.read_text())
        return data
    except Exception as e:
        log(f"⚠️  解析失败 {filepath.name}: {e}")
        return None


def extract_session_info(data: dict) -> dict:
    """从 session JSON 中提取关键信息"""
    info = {
        "session_id": data.get("session_id", "unknown"),
        "platform": data.get("platform", "unknown"),
        "session_start": data.get("session_start", ""),
        "last_updated": data.get("last_updated", ""),
        "model": data.get("model", ""),
        "user_messages": [],
        "task_description": "",
        "files_modified": [],
        "files_read": [],
        "errors": [],
        "learnings": [],
        "results": [],
        "tool_calls": [],
        "worklog_entries": [],
        "title": "",
    }

    messages = data.get("messages", [])
    if not messages:
        return info

    # ── 提取用户消息 ──
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            content = str(msg.get("content", ""))
            # 跳过系统/前缀消息
            if content.startswith("[IMPORTANT:") or content.startswith("[SYSTEM"):
                continue
            info["user_messages"].append(content[:500])

    # ── Session 标题 ──
    if info["user_messages"]:
        first_msg = info["user_messages"][0]
        # 取第一行作为标题，截断到 120 字符
        title = first_msg.split("\n")[0].strip()
        if len(title) > 120:
            title = title[:117] + "..."
        info["title"] = title

    # ── 分析每条消息 ──
    for msg in messages:
        role = msg.get("role", "")
        content = str(msg.get("content", ""))

        if role == "user" and content:
            # 提取任务描述
            if not info["task_description"] and len(content) > 20:
                info["task_description"] = content[:1000]

        elif role == "assistant":
            # 检查是否包含错误信息
            has_error = False
            for pattern in ERROR_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    has_error = True
                    # 提取错误附近的行
                    for line in content.split("\n"):
                        if re.search(pattern, line, re.IGNORECASE):
                            err_line = line.strip()[:200]
                            if err_line not in info["errors"]:
                                info["errors"].append(err_line)
                    break

            # 提取学习/发现
            if any(kw in content.lower() for kw in
                   ["learned", "discovered", "note:", "important:",
                    "关键", "发现", "注意", "学到"]):
                for line in content.split("\n"):
                    for kw in ["learned", "discovered", "note:", "important:",
                                "关键", "发现", "注意", "学到"]:
                        if kw.lower() in line.lower():
                            clean = line.strip().lstrip("-#*> ")[:300]
                            if clean and clean not in info["learnings"]:
                                info["learnings"].append(clean)
                            break

            # 提取最终结果（最后一条 assistant 消息的前几行）
            finish_reason = msg.get("finish_reason", "")
            if finish_reason == "stop" or msg == [m for m in messages if m.get("role") == "assistant"][-1:]:
                result_lines = [l.strip() for l in content.split("\n") if l.strip()]
                if result_lines:
                    summary = "\n".join(result_lines[:5])
                    if len(summary) > 500:
                        summary = summary[:497] + "..."
                    info["results"].append(summary)

        elif role == "tool":
            tool_name = ""
            tool_call_id = msg.get("tool_call_id", "")

            # 从消息中推断工具名
            # 查找之前的 assistant 消息中的 tool_call
            pass

    # ── 提取文件操作 ──
    file_patterns = [
        (r'(?:^|\s)(/~?[\w./-]+\.(?:py|js|ts|json|yaml|yml|md|sh|toml|cfg|ini|html|css|sql))',
         "files_modified"),
        (r'["\']((?:/~?[\w./-]+\.(?:py|js|ts|json|yaml|yml|md|sh|toml|cfg|ini)))\s*["\']',
         "files_modified"),
    ]

    all_content = " ".join(
        str(m.get("content", "")) for m in messages
        if m.get("role") in ("assistant", "tool")
    )

    seen_files = set()
    for pattern, category in file_patterns:
        for match in re.finditer(pattern, all_content):
            filepath = match.group(1)
            if filepath not in seen_files and len(filepath) > 3:
                seen_files.add(filepath)
                if category == "files_modified":
                    info["files_modified"].append(filepath)

    # ── 工具调用 ──
    for msg in messages:
        if msg.get("role") == "assistant":
            tool_calls_list = msg.get("tool_calls", [])
            for tc in tool_calls_list:
                func = tc.get("function", {})
                tool_name = func.get("name", "unknown")
                if tool_name not in info["tool_calls"]:
                    info["tool_calls"].append(tool_name)

    # ── Worklog ──
    if info["session_start"]:
        info["worklog_entries"].append({
            "time": info["session_start"],
            "action": f"Session started on {info['platform']}",
        })

    # 每个用户消息作为一个 worklog entry
    for i, um in enumerate(info["user_messages"]):
        short = um[:100].replace("\n", " ")
        info["worklog_entries"].append({
            "time": "",
            "action": f"User request #{i+1}: {short}",
        })

    # 文件操作日志
    for f in info["files_modified"][:10]:
        info["worklog_entries"].append({
            "time": "",
            "action": f"Modified: {f}",
        })

    return info


def deduplicate_entries(entries: List[str]) -> List[str]:
    """去重并清理条目"""
    seen = set()
    result = []
    for entry in entries:
        clean = entry.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(entry.strip())
    return result


def format_session_memory(all_infos: List[dict],
                          existing_memory: str = "") -> str:
    """格式化为 session_memory.md"""
    lines = []

    # ── 头部 ──
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines.append("# Session Memory")
    lines.append(f"> Last updated: {now}")
    lines.append(f"> Sessions analyzed: {len(all_infos)}")
    lines.append("")

    # ── 解析已有 memory 中的持久化部分 ──
    existing_sections = {}
    if existing_memory:
        current_section = None
        current_content = []
        for line in existing_memory.split("\n"):
            if line.startswith("## ") and not line.startswith("## Session: "):
                if current_section:
                    existing_sections[current_section] = "\n".join(current_content).strip()
                current_section = line[3:].strip()
                current_content = []
            elif current_section:
                current_content.append(line)
        if current_section:
            existing_sections[current_section] = "\n".join(current_content).strip()

    # ── 聚合所有 session 的信息 ──
    all_tasks = []
    all_files = []
    all_errors = []
    all_learnings = []
    all_results = []
    all_worklog = []

    for info in all_infos:
        if info.get("task_description"):
            all_tasks.append(info["task_description"])
        all_files.extend(info.get("files_modified", []))
        all_errors.extend(info.get("errors", []))
        all_learnings.extend(info.get("learnings", []))
        all_results.extend(info.get("results", []))
        all_worklog.extend(info.get("worklog_entries", []))

    # ── 去重 ──
    all_files = list(dict.fromkeys(all_files))
    all_errors = deduplicate_entries(all_errors)
    all_learnings = deduplicate_entries(all_learnings)
    all_results = deduplicate_entries(all_results)

    # ── Current State ──
    lines.append("## Current State")
    if all_infos:
        # 优先显示用户 session，回退到 cron session
        latest = all_infos[0]
        for info in all_infos:
            if info.get("platform") != "cron":
                latest = info
                break
        lines.append(f"- Last session: `{latest['session_id']}`")
        lines.append(f"- Platform: {latest['platform']}")
        lines.append(f"- Model: {latest['model']}")
        lines.append(f"- Started: {latest['session_start']}")
        tools = latest.get("tool_calls", [])
        if tools:
            lines.append(f"- Tools used: {', '.join(tools[:10])}")
        if latest.get("title"):
            lines.append(f"- Title: {latest['title'][:120]}")
    lines.append("")

    # ── Task Specification ──
    lines.append("## Task specification")
    if all_tasks:
        for task in all_tasks[:5]:
            short = task[:300].replace("\n", "\n  ")
            lines.append(f"- {short}")
    else:
        lines.append("- _(No tasks extracted)_")
    lines.append("")

    # ── Files and Functions ──
    lines.append("## Files and Functions")
    if all_files:
        for f in all_files[:20]:
            lines.append(f"- `{f}`")
    else:
        lines.append("- _(No files detected)_")
    lines.append("")

    # ── Workflow ──
    lines.append("## Workflow")
    for info in all_infos:
        session_id = info.get("session_id", "unknown")
        tools = info.get("tool_calls", [])
        if tools:
            lines.append(f"- `{session_id}`: {' → '.join(tools[:8])}")
    lines.append("")

    # ── Errors & Corrections ──
    lines.append("## Errors & Corrections")
    if all_errors:
        for err in all_errors[:15]:
            lines.append(f"- {err}")
    else:
        lines.append("- _(No errors detected)_")
    lines.append("")

    # ── Learnings ──
    lines.append("## Learnings")
    if all_learnings:
        for learn in all_learnings[:15]:
            lines.append(f"- {learn}")
    else:
        lines.append("- _(No learnings extracted)_")
    lines.append("")

    # ── Key Results ──
    lines.append("## Key results")
    if all_results:
        for result in all_results[:5]:
            short = result[:300].replace("\n", "\n  ")
            lines.append(f"- {short}")
    else:
        lines.append("- _(No results extracted)_")
    lines.append("")

    # ── Worklog ──
    lines.append("## Worklog")
    if all_worklog:
        for entry in all_worklog[:30]:
            time_str = entry.get("time", "")
            action = entry.get("action", "")
            if time_str:
                try:
                    dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    time_str = dt.strftime("%m-%d %H:%M")
                except Exception:
                    pass
            lines.append(f"- [{time_str}] {action}" if time_str else f"- {action}")
    lines.append("")

    # ── 保留已有持久记忆 ──
    for section, content in existing_sections.items():
        if content and section not in [
            "Current State", "Task specification",
            "Files and Functions", "Workflow",
            "Errors & Corrections", "Learnings",
            "Key results", "Worklog"
        ]:
            lines.append(f"## {section}")
            lines.append(content)
            lines.append("")

    return "\n".join(lines)


def read_existing_memory() -> str:
    """读取已有的 session_memory.md"""
    if MEMORY_FILE.exists():
        try:
            return MEMORY_FILE.read_text()
        except Exception:
            pass
    return ""


def run_extraction(since: Optional[datetime] = None,
                   specific_session: Optional[str] = None,
                   force: bool = False):
    """运行一次提取"""
    log("🚀 Session Memory Extract 启动")

    state = load_state()
    processed = state.get("processed_sessions", {})

    # 查找 session 文件
    files = find_session_files(since=since, specific_session=specific_session)
    log(f"📂 找到 {len(files)} 个 session 文件")

    # 过滤已处理的
    new_files = []
    for f in files:
        file_mtime = f.stat().st_mtime
        session_id = f.stem
        prev_info = processed.get(session_id, {})
        prev_mtime = prev_info.get("mtime", 0)
        if force or file_mtime > prev_mtime:
            new_files.append(f)

    log(f"📋 其中 {len(new_files)} 个需要处理")

    if not new_files:
        log("✅ 无需处理，所有 session 已是最新")
        return

    # 解析所有 session
    all_infos = []
    for f in new_files:
        data = parse_session_file(f)
        if not data:
            continue
        info = extract_session_info(data)
        all_infos.append(info)
        processed[f.stem] = {
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "mtime": f.stat().st_mtime,
            "session_id": info.get("session_id", f.stem),
        }
        log(f"  ✅ 处理: {f.name} → {info.get('title', 'N/A')[:60]}")

    if not all_infos:
        log("⚠️  没有成功解析的 session")
        return

    # 按时间倒序
    all_infos.sort(key=lambda x: x.get("session_start", ""), reverse=True)

    # 读取已有 memory
    existing = read_existing_memory()

    # 生成新的 memory
    new_memory = format_session_memory(all_infos, existing)

    # 写入
    try:
        MEMORY_FILE.write_text(new_memory)
        log(f"📄 已写入 {MEMORY_FILE} ({len(new_memory)} 字符)")
    except Exception as e:
        log(f"❌ 写入失败: {e}")

    # 保存状态
    save_state(state)

    # 统计
    log(f"📊 统计: {len(all_infos)} sessions, "
        f"{sum(len(i.get('files_modified', [])) for i in all_infos)} files, "
        f"{sum(len(i.get('errors', [])) for i in all_infos)} errors")

    # 打印摘要
    print(f"\n🧠 Session Memory Extract — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Sessions: {len(all_infos)}")
    print(f"   Files:    {sum(len(i.get('files_modified', [])) for i in all_infos)}")
    print(f"   Errors:   {sum(len(i.get('errors', [])) for i in all_infos)}")
    print(f"   Output:   {MEMORY_FILE}")
    print()

    log("✅ Session Memory Extract 完成")


def cmd_run(args):
    """运行提取"""
    since = None
    if args.since:
        since = parse_duration(args.since)
    run_extraction(since=since, specific_session=args.session, force=args.force)


def cmd_status(args):
    """查看状态"""
    state = load_state()
    processed = state.get("processed_sessions", {})
    print("🧠 Session Memory Extractor Status")
    print(f"   Last run:    {state.get('last_run', 'never')}")
    print(f"   Processed:   {len(processed)} sessions")
    print(f"   Memory file: {MEMORY_FILE}")
    if MEMORY_FILE.exists():
        size = MEMORY_FILE.stat().st_size
        print(f"   Memory size: {size:,} bytes")

    if args.verbose and processed:
        print("\n📋 Recently processed:")
        sorted_sessions = sorted(processed.items(),
                                 key=lambda x: x[1].get("processed_at", ""),
                                 reverse=True)[:10]
        for sid, pinfo in sorted_sessions:
            print(f"   {pinfo.get('processed_at', '?')[:16]}  {pinfo.get('session_id', sid)}")


def cmd_daemon(args):
    """守护进程模式"""
    interval = parse_interval(args.interval)
    print("🚀 Session Memory Extractor 守护进程启动")
    print(f"   间隔: {interval}s")
    print(f"   日志: {EXTRACT_LOG}")
    print("   按 Ctrl+C 停止\n")

    while True:
        try:
            run_extraction()
            timestamp = datetime.now().strftime("%H:%M")
            print(f"[{timestamp}] ✅ 提取完成")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋 Session Memory Extractor 已停止")
            break
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M')}] ❌ 错误: {e}")
            time.sleep(60)


def parse_duration(s: str) -> datetime:
    """解析时间范围字符串"""
    s = s.strip().lower()
    if s.endswith("h"):
        hours = int(float(s[:-1]))
        return datetime.now() - timedelta(hours=hours)
    elif s.endswith("m"):
        minutes = int(float(s[:-1]))
        return datetime.now() - timedelta(minutes=minutes)
    elif s.endswith("d"):
        days = int(float(s[:-1]))
        return datetime.now() - timedelta(days=days)
    else:
        hours = int(float(s))
        return datetime.now() - timedelta(hours=hours)


def parse_interval(s: str) -> int:
    """解析时间间隔为秒"""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    elif s.endswith("m"):
        return int(float(s[:-1]) * 60)
    elif s.endswith("s"):
        return int(float(s[:-1]))
    else:
        return int(s)


def main():
    parser = argparse.ArgumentParser(
        description="Session Memory Extractor — 从 session transcript 提取关键信息",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python session_memory_extract.py run                  # 处理最近 24h 的 session
  python session_memory_extract.py run --since 1h       # 最近 1 小时
  python session_memory_extract.py run --force           # 强制重新处理
  python session_memory_extract.py status                # 查看状态
  python session_memory_extract.py daemon --interval 30m # 守护进程 (每 30 分钟)
        """,
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    run_p = sub.add_parser("run", help="运行一次提取")
    run_p.add_argument("--since", default="24h",
                       help="处理最近 N 小时的 session (default: 24h)")
    run_p.add_argument("--session", default=None,
                       help="处理指定 session ID")
    run_p.add_argument("--force", action="store_true",
                       help="强制重新处理所有 session")

    status_p = sub.add_parser("status", help="查看状态")
    status_p.add_argument("--verbose", "-v", action="store_true",
                          help="显示详细信息")

    daemon_p = sub.add_parser("daemon", help="守护进程模式")
    daemon_p.add_argument("--interval", default="1h",
                          help="提取间隔 (default: 1h)")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()