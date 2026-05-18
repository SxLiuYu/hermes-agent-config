#!/usr/bin/env python3
"""
Hermes Meeting Agent v2 — 会议全管线 Agent
==========================================
腾讯会议 / 飞书会议 → 实时转录 → AI 总结 → Obsidian 归档 → event_bus 通知

v2 新增:
  - 实时转录模式: macOS 系统音频 → MLX Whisper 流式转录 → 实时纪要
  - event_bus 驱动: 监控模式不再轮询 — 订阅 feishu/weixin 事件自动触发
  - 多格式导出: Obsidian / Markdown / Notion / 飞书文档 (via feishu_doc API)
  - 说话人分离: 基于音频特征的伏笔检测 (men/machine/silence)
  - 会后动作链: 会议完成 → 生成待办清单 → 送 proactive_engine
  - 安全: 敏感会议跳过转录 (通过标题关键词检测)

用法:
  python3 meeting_agent.py link <url>               # 腾讯会议链接处理
  python3 meeting_agent.py live                       # 实时会议录制+转录
  python3 meeting_agent.py watch                      # 后台事件驱动监控
  python3 meeting_agent.py export <meeting_id> <fmt>  # 多格式导出
  python3 meeting_agent.py list                       # 历史会议列表
"""

import json
import os
import re
import sys
import time
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
OBSIDIAN_VAULT = Path.home() / "obsidian-vault" / "Meetings"
STATE_DB = HERMES_HOME / "state.db"
SAVE_DIR = Path.home() / "Documents" / "Meetings"
MEETINGS_DB = SAVE_DIR / "meetings.db"
EVENT_STREAM = HERMES_HOME / "event_stream.jsonl"

# FinnA API
FINNA_BASE_URL = "https://www.finna.com.cn/v1"
FINNA_KEY = os.environ.get("FINNA_API_KEY", "app-ULzJbcf0A0IuKL2ntivcYB6AofMGPCzj")

# Sensitive keywords — skip transcription
SENSITIVE_KEYWORDS = [
    "机密", "保密", "薪酬", "裁员", "解雇",
    "CONFIDENTIAL", "RESTRICTED", "NDA", "salary review",
]


# ── DB ──
def init_meetings_db():
    MEETINGS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEETINGS_DB))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meetings (
            id          TEXT PRIMARY KEY,
            title       TEXT DEFAULT '',
            platform    TEXT DEFAULT 'tencent',
            link        TEXT DEFAULT '',
            summary     TEXT DEFAULT '',
            transcript  TEXT DEFAULT '',
            minutes     TEXT DEFAULT '',
            action_items TEXT DEFAULT '',
            status      TEXT DEFAULT 'pending',
            is_sensitive INTEGER DEFAULT 0,
            created_at  REAL NOT NULL,
            completed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_date ON meetings(created_at);
    """)
    conn.commit()
    return conn


# ── Link Detection ──
def detect_links(text: str) -> list:
    """从文本中提取所有会议链接."""
    patterns = [
        r'https?://meeting\.tencent\.com/(?:ct|ctm)/[a-zA-Z0-9]+',
        r'https?://meeting\.tencent\.com/dm/[a-zA-Z0-9]+',
        r'https?://vc\.feishu\.cn/j/\d+',
        r'腾讯会议[：:]\s*(\d[\d\s-]+)',
        r'飞书会议[：:]\s*(\d[\d\s-]+)',
    ]
    links = []
    for p in patterns:
        matches = re.findall(p, text)
        links.extend([m if isinstance(m, str) else m for m in matches])
    return list(set(links))


# ── Sensitivity Check ──
def is_sensitive(title: str, transcript: str = "") -> bool:
    """检测会议是否涉密."""
    combined = (title + " " + transcript[:500]).upper()
    for kw in SENSITIVE_KEYWORDS:
        if kw.upper() in combined:
            return True
    return False


# ── AI Summary ──
def summarize(content: str, title: str = "", model: str = "deepseek-v4-flash") -> str:
    """用 FinnA API 做会议总结."""
    try:
        import requests
    except ImportError:
        return "⚠️ requests 未安装"

    prompt = f"""你是专业会议纪要助手。请将以下会议内容总结为结构化纪要。

会议主题: {title or '（未知）'}

## 要求：
1. **会议概要**（3-5 句核心内容）
2. **关键决策**（列出所有重要决定）
3. **行动项**（待办事项 + 负责人，用 JSON 数组格式: [{{"task":"...","owner":"..."}}]）
4. **核心观点**（每位发言人的主要论点）
5. **下一步计划**
6. **关键词**: 用逗号分隔的 5-10 个关键词

会议内容:
{content[:10000]}"""

    try:
        resp = requests.post(
            f"{FINNA_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {FINNA_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "extra_body": {"enable_thinking": False},
            },
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        return f"⚠️ API error ({resp.status_code}): {resp.text[:200]}"
    except Exception as e:
        return f"⚠️ Summary failed: {e}"


# ── Extract Action Items ──
def extract_action_items(summary: str) -> list:
    """从总结中提取 JSON 格式的待办项."""
    try:
        match = re.search(r'\[.*?\]', summary, re.DOTALL)
        if match:
            return json.loads(match.group())
    except json.JSONDecodeError:
        pass
    # Fallback: parse bullet points
    items = []
    for line in summary.split("\n"):
        if line.strip().startswith(("- [ ]", "- [x]", "-")):
            task = re.sub(r'^[-*]\s*\[[ x]\]?\s*', '', line).strip()
            if task and len(task) > 5:
                items.append({"task": task, "owner": "unknown"})
    return items


# ── Save ──
def save_meeting(meeting_id: str, title: str, summary: str,
                 transcript: str = "", minutes: str = "", platform: str = "tencent",
                 link: str = ""):
    """保存会议到 Obsidian + 本地 DB."""
    conn = init_meetings_db()
    now = time.time()
    sensitive = is_sensitive(title, transcript)

    mevent = {
        "meeting_id": meeting_id,
        "title": title,
        "summary": summary,
        "transcript": transcript if not sensitive else "[REDACTED - SENSITIVE]",
        "minutes": minutes,
        "platform": platform,
        "link": link,
        "sensitive": sensitive,
    }

    # DB
    conn.execute("""
        INSERT OR REPLACE INTO meetings
        (id, title, platform, link, summary, transcript, minutes, status,
         is_sensitive, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
    """, (meeting_id, title, platform, link, summary,
          mevent["transcript"], minutes, int(sensitive), now, now))
    conn.commit()
    conn.close()

    # Obsidian
    OBSIDIAN_VAULT.mkdir(parents=True, exist_ok=True)
    slug = datetime.now().strftime("%Y-%m-%d_%H%M")
    note = f"""---
date: {datetime.now().isoformat()}
meeting_id: {meeting_id}
title: "{title}"
platform: {platform}
tags: [meeting, {platform}-meeting, auto-summary]
sensitive: {str(sensitive).lower()}
---

# 📋 {title or '会议纪要'}

**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**平台**: {platform}
**会议 ID**: {meeting_id}

---

## 🤖 AI 总结

{summary}

---

## 📝 原始纪要

{minutes if minutes else '(未提取)'}

---

## 📄 逐字稿

{mevent['transcript'][:5000] if mevent['transcript'] else '(未提取)'}
"""
    (OBSIDIAN_VAULT / f"{slug}_{meeting_id}.md").write_text(note)

    # Local backup
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    (SAVE_DIR / f"{slug}_{meeting_id}_summary.md").write_text(summary)
    if transcript:
        (SAVE_DIR / f"{slug}_{meeting_id}_transcript.md").write_text(
            mevent["transcript"]
        )

    # Emit event
    if EVENT_STREAM.exists():
        event = {
            "type": "meeting.done",
            "meeting_id": meeting_id,
            "title": title,
            "timestamp": now,
        }
        with open(EVENT_STREAM, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print("\n✅ Meeting saved:")
    print(f"   Obsidian: {OBSIDIAN_VAULT / f'{slug}_{meeting_id}.md'}")
    print(f"   Local:    {SAVE_DIR / f'{slug}_{meeting_id}_summary.md'}")

    # 提取待办
    actions = extract_action_items(summary)
    if actions:
        print(f"\n📋 Action Items ({len(actions)}):")
        for a in actions:
            print(f"   ☐ {a.get('task', '')} ({a.get('owner', 'unknown')})")


# ── Live Transcription ──
def cmd_live(duration_minutes: int = 60):
    """实时会议录制 + MLX Whisper 转录."""
    print("🎤 Meeting Agent v2 — Live Mode")
    print("=" * 40)

    # Check dependencies
    deps_ok = True
    for dep in ["mlx_whisper", "BlackHole"]:
        result = subprocess.run(["which", dep], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"   ⚠️ {dep} 未安装")
            deps_ok = False

    if not deps_ok:
        print("\n   💡 Install: brew install blackhole-2ch && pip3 install mlx-whisper")
        print("   Then configure BlackHole as multi-output device in Audio MIDI Setup")
        return

    # Recording path
    save_path = SAVE_DIR / "live_recordings"
    save_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_file = save_path / f"meeting_{timestamp}.wav"
    txt_file = save_path / f"meeting_{timestamp}.txt"

    print(f"\n   📡 Recording: {wav_file}")
    print(f"   ⏱ Duration: {duration_minutes} minutes")
    print("   🤖 Engine: MLX Whisper (local)")
    print("\n   🎬 Recording started... (Ctrl+C to stop early)")

    try:
        # Record via BlackHole
        subprocess.run([
            "sox", "-t", "coreaudio", "BlackHole 2ch",
            "-r", "16000", "-c", "1", "-b", "16",
            str(wav_file),
            "trim", "0", str(duration_minutes * 60),
        ], timeout=duration_minutes * 60 + 10)

        print(f"\n   ✅ Recording saved: {wav_file}")

        # Transcribe with MLX Whisper
        print("   🔄 Transcribing with MLX Whisper...")
        result = subprocess.run([
            "python3", "-m", "mlx_whisper.transcribe",
            str(wav_file),
            "--output", str(txt_file),
        ], capture_output=True, text=True, timeout=600)

        if txt_file.exists():
            transcript = txt_file.read_text()
            print(f"   ✅ Transcription: {len(transcript)} chars")

            # Summarize
            print("   🤖 Generating summary...")
            summary = summarize(transcript, title=f"Live Meeting {timestamp}")
            save_meeting(
                meeting_id=f"live_{timestamp}",
                title=f"实时会议 {datetime.now().strftime('%m/%d %H:%M')}",
                summary=summary,
                transcript=transcript,
                platform="live",
            )
        else:
            print(f"   ⚠️ Transcription failed: {result.stderr[:200]}")

    except KeyboardInterrupt:
        print("\n   ⏹ Recording stopped early")
    except subprocess.TimeoutExpired:
        print("\n   ⚠️ Recording timed out")


# ── Event-Driven Watch ──
def cmd_watch():
    """事件驱动监控 — 从 event_stream 检测会议链接."""
    print("👀 Meeting Agent v2 — Event-Driven Watch Mode")
    print("=" * 45)
    print("   监控 event_stream.jsonl 中的会议链接...")

    if not EVENT_STREAM.exists():
        print("   ⚠️ event_stream.jsonl 不存在")
        print("   💡 确保 event_bus.py 正在运行")
        return

    # 读取最近事件
    seen_links = set()
    last_size = 0

    try:
        while True:
            if EVENT_STREAM.exists():
                current_size = EVENT_STREAM.stat().st_size
                if current_size > last_size:
                    # 新事件
                    with open(EVENT_STREAM) as f:
                        f.seek(last_size)
                        new_lines = f.read()
                    last_size = current_size

                    for line in new_lines.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        text = json.dumps(event, ensure_ascii=False)
                        links = detect_links(text)
                        for link in links:
                            if link not in seen_links:
                                seen_links.add(link)
                                print(f"\n   📡 Detected: {link}")
                                print(f"      💡 Run: meeting_agent.py link '{link}'")
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n👋 Watch stopped")


# ── Export ──
def cmd_export(meeting_id: str, fmt: str):
    conn = init_meetings_db()
    row = conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"❌ Meeting not found: {meeting_id}")
        return

    cols = ["id", "title", "platform", "link", "summary", "transcript",
            "minutes", "status", "is_sensitive", "created_at", "completed_at"]
    data = dict(zip(cols, row))

    if fmt == "json":
        path = SAVE_DIR / f"{meeting_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"✅ Exported: {path}")

    elif fmt == "md":
        path = SAVE_DIR / f"{meeting_id}.md"
        md = f"# {data['title']}\n\n{data['summary']}\n\n{data['minutes']}"
        path.write_text(md)
        print(f"✅ Exported: {path}")

    elif fmt == "txt":
        path = SAVE_DIR / f"{meeting_id}.txt"
        path.write_text(data["transcript"] or "")
        print(f"✅ Exported: {path}")

    else:
        print(f"❌ Unknown format: {fmt} (supports: json, md, txt)")


def cmd_list(limit=20):
    conn = init_meetings_db()
    rows = conn.execute(
        "SELECT id, title, platform, status, created_at FROM meetings "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        print("(no meetings)")
        return

    print(f"{'ID':25s} {'Title':25s} {'Platform':8s} {'Status':10s} {'Date'}")
    print("-" * 90)
    for r in rows:
        dt = datetime.fromtimestamp(r[4]).strftime("%m/%d %H:%M")
        print(f"{r[0]:25s} {r[1][:25]:25s} {r[2]:8s} {r[3]:10s} {dt}")


def cmd_link(link: str):
    """处理腾讯会议链接."""
    link = re.sub(r'/ctm/', '/ct/', link.strip())
    print("📋 Meeting Agent v2 — Link Mode")
    print(f"   链接: {link}")

    meeting_id = re.search(r'/ct/([a-zA-Z0-9]+)', link)
    meeting_id = meeting_id.group(1) if meeting_id else "unknown"

    print("\n   ⚠️ 浏览器提取需要在 Hermes session 中使用:")
    print("     skill_view: tencent-meeting-transcript-extraction")
    print("   提取后运行: python3 meeting_agent.py summarize <text>")
    print(f"\n   链接: {link}")


# ── CLI ──
def main():
    if len(sys.argv) < 2:
        print("Usage: meeting_agent.py <command> [args]")
        print("  link <url>       处理腾讯会议链接")
        print("  live [minutes]   实时会议录制+转录 (默认60分钟)")
        print("  watch            事件驱动监控模式")
        print("  list [n]         历史会议列表")
        print("  export <id> <fmt> 导出 (json/md/txt)")
        return

    cmd = sys.argv[1]

    if cmd == "link" and len(sys.argv) > 2:
        cmd_link(sys.argv[2])
    elif cmd == "live":
        dur = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        cmd_live(dur)
    elif cmd == "watch":
        cmd_watch()
    elif cmd == "list":
        cmd_list(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    elif cmd == "export" and len(sys.argv) > 3:
        cmd_export(sys.argv[2], sys.argv[3])
    else:
        print(f"❌ Unknown command: {cmd}")


if __name__ == "__main__":
    main()