#!/usr/bin/env python3
"""
Hermes Auto-Fetch v2
=====================
自适应间隔 + 服务优先级 + 并发拉取 + 智能去重

升级:
  - 自适应间隔: 高优先级服务更频繁, 低优先级降频
  - 并发拉取: 同时拉取多个服务 (ThreadPoolExecutor)
  - 智能去重: 内容 hash 跨 session 去重
  - 优先级权重: 用户活跃时段加速

用法:
  python3 auto_fetch.py --once
  python3 auto_fetch.py --daemon
"""

import os
import sys
import yaml
import json
import sqlite3
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SERVICES_FILE = HERMES_HOME / "services.yaml"
STATE_DB = HERMES_HOME / "state.db"
AUTO_FETCH_DIR = HERMES_HOME / "auto_fetch"
OBSIDIAN_VAULT = Path.home() / "obsidian-vault"
MEM_DIR = OBSIDIAN_VAULT / "Memory"
CHUNK_DIR = OBSIDIAN_VAULT / "Chunks"

CHUNK_MAX_CHARS = 3000

# Priority intervals (minutes)
PRIORITY_INTERVALS = {
    "high": 10,    # messaging
    "medium": 20,  # collaboration
    "low": 60,     # content/media
    "default": 20,
}

SERVICE_PRIORITIES = {
    "feishu": "high",
    "weixin": "high",
    "github": "medium",
    "google-workspace": "medium",
    "notion": "low",
    "spotify": "low",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_services():
    if not SERVICES_FILE.exists():
        return {"services": {}}
    return yaml.safe_load(SERVICES_FILE.read_text())


def save_services(data):
    SERVICES_FILE.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))


def get_connected_services():
    data = load_services()
    return [sid for sid, svc in data.get("services", {}).items()
            if svc.get("status") == "connected" and svc.get("auto_fetch", True)]


def get_priority(service_id):
    return SERVICE_PRIORITIES.get(service_id, "default")


def get_adaptive_interval(service_id):
    """Get adaptive fetch interval based on priority and user activity."""
    priority = get_priority(service_id)
    base = PRIORITY_INTERVALS.get(priority, 20)
    # Check if user is active (recent messages)
    intel_db = HERMES_HOME / "conversation_intel.db"
    if intel_db.exists():
        try:
            conn = sqlite3.connect(str(intel_db))
            active = conn.execute("""
                SELECT COUNT(*) FROM conversation_analysis
                WHERE timestamp > ? AND role='user'""",
                (time.time() - 600,)).fetchone()[0]
            conn.close()
            if active > 0:
                base = max(5, base // 2)  # Double speed when active
        except:
            pass
    return base


def chunk_text(text, source):
    if len(text) <= CHUNK_MAX_CHARS:
        return [text]
    paras = text.split("\n\n")
    chunks, current = [], ""
    for p in paras:
        if len(current) + len(p) + 2 <= CHUNK_MAX_CHARS:
            current = (current + "\n\n" + p).strip()
        else:
            if current: chunks.append(current)
            current = p if len(p) <= CHUNK_MAX_CHARS else p[:CHUNK_MAX_CHARS-3] + "..."
    if current: chunks.append(current)
    return chunks


def content_hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def is_duplicate(content):
    """Check if content was already fetched (cross-session dedup)."""
    h = content_hash(content)
    # Check recent chunks
    if CHUNK_DIR.exists():
        for f in sorted(CHUNK_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
            if f.stat().st_size > 0 and content_hash(f.read_text()) == h:
                return True
    return False


def write_chunk(content, source, cid):
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    title = content.strip().split("\n")[0][:80] if content.strip() else "Untitled"
    date_str = datetime.now().strftime("%Y-%m-%d")
    fp = CHUNK_DIR / f"{date_str}_{source}_{cid[:8]}.md"
    fm = {"source": source, "chunk_id": cid, "date": date_str,
          "fetched_at": now_iso(), "char_count": len(content)}
    md = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n\n"
    md += f"# {title}\n\n{content}"
    fp.write_text(md, encoding="utf-8")
    return str(fp)


def update_memory(source, content):
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    sf = MEM_DIR / f"{source}.md"
    if sf.exists():
        existing = sf.read_text(encoding="utf-8")
    else:
        existing = f"---\nsource: {source}\ncreated: {date_str}\n---\n\n# {source.upper()} Memory\n\n"
    sf.write_text(existing + f"\n## {date_str} {datetime.now().strftime('%H:%M')}\n\n{content}\n")
    chunks = chunk_text(content, source)
    for i, ch in enumerate(chunks):
        write_chunk(ch, source, content_hash(ch))
    return str(sf)


# ---- Fetchers ----

def fetch_messages(source):
    if not STATE_DB.exists(): return None
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(minutes=25)).timestamp()
    rows = conn.execute("""SELECT m.role, m.content, m.timestamp FROM messages m
        JOIN sessions s ON m.session_id=s.id
        WHERE s.source=? AND m.timestamp>? AND m.role IN ('user','assistant')
        ORDER BY m.timestamp DESC LIMIT 30""", (source, cutoff)).fetchall()
    conn.close()
    if not rows: return None
    lines = []
    for r in reversed(rows):
        role = "👤" if r["role"] == "user" else "🤖"
        lines.append(f"{role} {(r['content'] or '')[:200].replace(chr(10), ' ')}")
    return "\n".join(lines) if lines else None


def fetch_github():
    token = os.environ.get("GITHUB_TOKEN")
    if not token: return None
    import subprocess
    r = subprocess.run(
        ["gh", "api", "user/events", "--jq", ".[0:5] | .[] | {type, repo: .repo.name, created_at}"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "GITHUB_TOKEN": token})
    if r.returncode != 0 or not r.stdout.strip(): return None
    lines = ["## GitHub Activity"]
    for ln in r.stdout.strip().split("\n"):
        try:
            ev = json.loads(ln)
            lines.append(f"- {ev.get('type','?')} on {ev.get('repo','?')}")
        except: pass
    return "\n".join(lines)


FETCHERS = {
    "feishu": lambda: fetch_messages("feishu"),
    "weixin": lambda: fetch_messages("weixin"),
    "github": fetch_github,
}


def update_daily_digest(results):
    date_str = datetime.now().strftime("%Y-%m-%d")
    df = OBSIDIAN_VAULT / "Daily Digest.md"
    lines = ["---", f"date: {date_str}", f"updated: {datetime.now().strftime('%H:%M')}",
             "tags: [digest, auto-fetch-v2]", "---", "",
             f"# Daily Digest - {date_str}", "",
             f"> Auto-fetch v2 | {datetime.now().strftime('%H:%M')}", ""]
    if not results:
        lines.append("_No new data_")
    else:
        for source, content in results.items():
            if content:
                lines.append(f"## {source}")
                lines.append(f"> {content[:200].replace(chr(10),' ').strip()}...")
                lines.append(f"[View](Memory/{source}.md)")
                lines.append("")
    df.parent.mkdir(parents=True, exist_ok=True)
    df.write_text("\n".join(lines), encoding="utf-8")


def update_registry(results):
    data = load_services()
    now = now_iso()
    for sid in results:
        if sid in data.get("services", {}):
            data["services"][sid]["last_fetch"] = now
            data["services"][sid]["last_fetch_result"] = "ok" if results[sid] else "empty"
    data["last_auto_fetch"] = now
    save_services(data)


def fetch_parallel(service_ids, max_workers=3):
    """Concurrent fetch with ThreadPoolExecutor."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for sid in service_ids:
            fetcher = FETCHERS.get(sid)
            if fetcher:
                futures[executor.submit(fetcher)] = sid

        for future in as_completed(futures):
            sid = futures[future]
            try:
                content = future.result(timeout=30)
                results[sid] = content
            except Exception as e:
                print(f"  {sid}: error - {e}", file=sys.stderr)
                results[sid] = None

    return results


def run_once():
    print(f"\n{'='*50}")
    print(f"  Auto-Fetch v2 - {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    connected = get_connected_services()
    fetchable = [s for s in connected if s in FETCHERS]

    if not fetchable:
        print("  No fetchable services.")
        update_daily_digest({})
        return {}

    print(f"  Fetchable: {fetchable}")

    # Concurrent fetch
    raw_results = fetch_parallel(fetchable)

    # Process + dedup + write
    results = {}
    chunk_count = 0
    for sid, content in raw_results.items():
        if not content:
            print(f"  {sid}: no new data")
            results[sid] = None
            continue
        if is_duplicate(content):
            print(f"  {sid}: duplicate (skipped)")
            results[sid] = "duplicate"
            continue
        path = update_memory(sid, content)
        chunks = chunk_text(content, sid)
        chunk_count += len(chunks)
        print(f"  {sid}: {len(content)} chars -> {len(chunks)} chunks")
        results[sid] = content

    update_registry(results)
    update_daily_digest(results)

    print(f"\n  Summary: {len([v for v in results.values() if v and v != 'duplicate'])} new, {chunk_count} chunks")
    print(f"{'='*50}\n")
    return results


def run_daemon():
    print("Auto-Fetch v2 - adaptive daemon")
    # Track per-service last fetch
    last_fetch = {}
    try:
        while True:
            connected = get_connected_services()
            fetchable = [s for s in connected if s in FETCHERS]

            # Only fetch services due for refresh
            now = time.time()
            due = []
            for sid in fetchable:
                interval = get_adaptive_interval(sid) * 60
                if sid not in last_fetch or (now - last_fetch[sid]) >= interval:
                    due.append(sid)

            if due:
                raw = fetch_parallel(due)
                for sid, content in raw.items():
                    if content and not is_duplicate(content):
                        update_memory(sid, content)
                        print(f"  [{datetime.now().strftime('%H:%M')}] {sid}: fetched")
                    last_fetch[sid] = now

            time.sleep(30)  # Check every 30s
    except KeyboardInterrupt:
        print("\nAuto-fetch stopped.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    args = ap.parse_args()

    if args.once:
        run_once()
    elif args.daemon:
        run_daemon()
    else:
        run_once()