#!/usr/bin/env python3
"""
Hermes Proactive Engine v2
===========================
自适应调度 + oMLX 洞察 + 状态感知

升级:
  - 自适应调度: 用户活跃时加速, 空闲时减速
  - oMLX 本地推理: 使用 oMLX Qwen3.5-4B 生成洞察 (零成本, 始终在线)
  - 状态感知: 读取 conversation_intel 用户状态, 定制洞察内容
  - 新检测: 组件健康检查扩展 (native_voice, device_sync, intel)

用法:
  python3 proactive_engine.py once
  python3 proactive_engine.py daemon
  python3 proactive_engine.py insights
"""

import json
import os
import sqlite3
import sys
import time
import yaml
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_FILE = HERMES_HOME / "trigger_state.json"
SERVICES_FILE = HERMES_HOME / "services.yaml"
SEAL_DB = HERMES_HOME / "memory_seal.db"
STATE_DB = HERMES_HOME / "state.db"
INTEL_DB = HERMES_HOME / "conversation_intel.db"
OBSIDIAN_INSIGHTS = Path.home() / "obsidian-vault" / "Insights"
INSIGHTS_LOG = HERMES_HOME / "proactive_insights.json"

OMLX_BASE = "http://localhost:4560/v1"
FINNA_BASE = "https://www.finna.com.cn/v1"
FINNA_KEY = os.environ.get("FINNA_API_KEY", "app-BqyKsTO4Om3JGoPCTkJX080J")

# Adaptive scheduling
MIN_INTERVAL = 60       # 1 min when active
MAX_INTERVAL = 600      # 10 min when idle
ACTIVITY_WINDOW = 300   # 5 min lookback for activity check


def is_user_active():
    """Check if user has been active recently (for adaptive scheduling)."""
    if not INTEL_DB.exists():
        return False
    try:
        conn = sqlite3.connect(str(INTEL_DB))
        cutoff = time.time() - ACTIVITY_WINDOW
        cnt = conn.execute("SELECT COUNT(*) FROM conversation_analysis WHERE timestamp>? AND role='user'",
            (cutoff,)).fetchone()[0]
        conn.close()
        return cnt > 0
    except:
        return False


def get_user_state():
    """Get current user state from conversation_intel."""
    if not INTEL_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(INTEL_DB))
        row = conn.execute("""SELECT valence,arousal,dominance,fatigue_score,focus_score,dominant_topic
            FROM user_state ORDER BY timestamp DESC LIMIT 1""").fetchone()
        conn.close()
        if row:
            return {"valence": row[0],"arousal": row[1],"dominance": row[2],
                    "fatigue": row[3],"focus": row[4],"topic": row[5]}
    except:
        pass
    return {}


def detect_event_bursts():
    events_dir = Path.home() / "obsidian-vault" / "Events"
    if not events_dir.exists():
        return []
    now = time.time()
    recent = []
    for f in events_dir.glob("*.md"):
        if f.stat().st_mtime > now - 3600:
            recent.append({"file": f.name, "time": f.stat().st_mtime,
                "source": f.stem.split("_")[-1] if "_" in f.stem else "unknown"})
    if len(recent) < 3:
        return []
    recent.sort(key=lambda e: e["time"])
    patterns = []
    for i in range(len(recent) - 2):
        window = [e for e in recent if e["time"] - recent[i]["time"] <= 300]
        if len(window) >= 3:
            sources = Counter(e["source"] for e in window)
            if len(sources) == 1:
                src = list(sources.keys())[0]
                patterns.append({"type":"event_burst","source":src,"count":len(window),
                    "message": f"事件爆发: {src} 在5分钟内触发 {len(window)} 次"})
            break
    return patterns[:3]


def detect_topic_trends():
    if not SEAL_DB.exists():
        return []
    conn = sqlite3.connect(str(SEAL_DB))
    now = time.time()
    recent = conn.execute("""SELECT topic, COUNT(*) as cnt, AVG(score) as avg_score
        FROM chunks WHERE created_at>? GROUP BY topic ORDER BY cnt DESC LIMIT 5""",
        (now-21600,)).fetchall()
    older = dict(conn.execute("""SELECT topic, COUNT(*) FROM chunks
        WHERE created_at>? AND created_at<=? GROUP BY topic""",
        (now-86400, now-21600)).fetchall())
    conn.close()
    trends = []
    for topic, cnt, avg in recent:
        old_cnt = older.get(topic, 0)
        if old_cnt > 0 and cnt > old_cnt * 2:
            trends.append({"type":"topic_surge","topic":topic,"recent":cnt,"older":old_cnt,
                "message": f"话题 '{topic}' 飙升: {old_cnt}->{cnt} ({cnt/old_cnt:.1f}x)"})
    return trends[:3]


def check_component_health():
    """Check all Hermes component daemons."""
    alerts = []
    components = {
        "trigger_engine": "trigger_engine.py daemon",
        "conversation_intel": "conversation_intel.py daemon",
        "event_bus": "event_bus.py start",
        "native_voice": "native_voice.py wake",
        "device_sync": "device_sync.py daemon",
    }
    for name, pattern in components.items():
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        if r.returncode != 0:
            alerts.append({"type":"component_down","component":name,
                "message": f"组件离线: {name}"})
    return alerts[:5]


def check_service_health():
    if not SERVICES_FILE.exists():
        return []
    cfg = yaml.safe_load(SERVICES_FILE.read_text())
    alerts = []
    for key, svc in cfg.get("services", {}).items():
        name = svc.get("name", key)
        if svc.get("auto_fetch"):
            last_fetch = svc.get("last_fetch")
            if last_fetch:
                try:
                    ft = datetime.fromisoformat(last_fetch.replace("Z","+00:00"))
                    if datetime.now(ft.tzinfo) - ft > timedelta(hours=2):
                        alerts.append({"type":"stale_fetch","service":name,
                            "message": f"{name} 超2小时未拉取"})
                except:
                    pass
    return alerts[:5]


def get_recent_context(user_state):
    """Get recent conversation context, tailored by user state."""
    if not STATE_DB.exists():
        return ""
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - 1800
    rows = conn.execute("""SELECT m.content, m.role FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE m.timestamp>? AND m.role IN ('user','assistant') ORDER BY m.timestamp DESC LIMIT 15""",
        (cutoff,)).fetchall()
    conn.close()
    if not rows:
        return ""
    lines = []
    for r in reversed(rows):
        role = "User" if r["role"] == "user" else "Hermes"
        lines.append(f"[{role}] {(r['content'] or '')[:200]}")
    # State-aware prefix
    prefix = ""
    if user_state:
        topic = user_state.get("topic", "unknown")
        fatigue = user_state.get("fatigue", 0)
        focus = user_state.get("focus", 0)
        if fatigue > 0.5:
            prefix = f"[STATE: user tired (fatigue={fatigue:.2f}), topic={topic}]\n"
        elif focus > 0.6:
            prefix = f"[STATE: user focused (focus={focus:.2f}), topic={topic}]\n"
    return prefix + "\n".join(lines)


def generate_insight_omlx(context):
    """Generate insight using local oMLX model. Falls back to FinnA."""
    if not context or len(context) < 50:
        return ""

    try:
        import requests
    except ImportError:
        return ""

    prompt = f"""你是主动思考的 AI 助手。根据最近上下文，生成1-2条有价值的主动洞察。简短实用。

最近:
{context[:2500]}

用中文回复，每条一行，以项目符号开头。"""

    # Try local oMLX first
    try:
        resp = requests.post(
            f"{OMLX_BASE}/chat/completions",
            headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
            json={"model": "qwen3.5-4b-mlx", "messages": [{"role":"user","content":prompt}],
                  "max_tokens": 200, "temperature": 0.7, "stream": False},
            timeout=10)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except:
        pass

    # Fallback to FinnA
    try:
        resp = requests.post(
            f"{FINNA_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {FINNA_KEY}", "Content-Type": "application/json"},
            json={"model":"deepseek-v4-flash","messages":[{"role":"user","content":prompt}],
                  "max_tokens":200,"stream":False,"extra_body":{"enable_thinking":False}},
            timeout=15)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except:
        pass
    return ""


def save_insights(insights):
    if not insights:
        return
    now = datetime.now()
    current = []
    if INSIGHTS_LOG.exists():
        try: current = json.loads(INSIGHTS_LOG.read_text())
        except: pass
    current.extend([{**i, "generated_at": now.isoformat()} for i in insights])
    current = current[-100:]
    INSIGHTS_LOG.write_text(json.dumps(current, indent=2, ensure_ascii=False))
    OBSIDIAN_INSIGHTS.mkdir(parents=True, exist_ok=True)
    slug = now.strftime("%Y-%m-%d_%H%M")
    content = f"---\ndate: {now.isoformat()}\ntype: proactive\n---\n\n# Proactive Insights - {now.strftime('%Y-%m-%d %H:%M')}\n\n"
    for i in insights:
        content += f"- {i['message']}\n"
    (OBSIDIAN_INSIGHTS / f"{slug}_insights.md").write_text(content)


def think_once(silent=False):
    if not silent:
        print(f"\nProactive Engine v2 - {datetime.now().strftime('%H:%M:%S')}")
    all_insights = []
    user_state = get_user_state()

    # 1. Event bursts
    bursts = detect_event_bursts()
    all_insights.extend(bursts)
    for b in bursts:
        if not silent: print(f"  {b['message']}")

    # 2. Topic trends
    trends = detect_topic_trends()
    all_insights.extend(trends)
    for t in trends:
        if not silent: print(f"  {t['message']}")

    # 3. Component health
    comp = check_component_health()
    all_insights.extend(comp)
    for c in comp:
        if not silent: print(f"  {c['message']}")

    # 4. Service health
    svc = check_service_health()
    all_insights.extend(svc)
    for s in svc:
        if not silent: print(f"  {s['message']}")

    # 5. AI insight (oMLX)
    context = get_recent_context(user_state)
    if context:
        llm = generate_insight_omlx(context)
        if llm:
            all_insights.append({"type":"llm_insight","message":llm})
            if not silent: print(f"  {llm[:200]}")

    if all_insights:
        save_insights(all_insights)

    if not silent:
        total = len(all_insights)
        print(f"\n  {'Insights found' if total>0 else 'All clear'} ({total} total)")
    return all_insights


def show_insights(limit=10):
    if not INSIGHTS_LOG.exists():
        print("No insights yet.")
        return
    data = json.loads(INSIGHTS_LOG.read_text())
    print(f"\nProactive Insights ({len(data)} total)\n")
    for i in data[-limit:]:
        ts = i.get("generated_at","?")[:16]
        print(f"  [{ts}] {i.get('message','?')}")


def main():
    if len(sys.argv) < 2:
        print("Proactive Engine v2")
        print("  once      Run one cycle")
        print("  daemon    Adaptive daemon")
        print("  insights  Show recent insights")
        return
    cmd = sys.argv[1]
    if cmd == "once":
        think_once()
    elif cmd == "daemon":
        print("Proactive Engine v2 - adaptive daemon")
        print("  oMLX insight + state-aware + component health")
        try:
            while True:
                think_once(silent=True)
                active = is_user_active()
                interval = MIN_INTERVAL if active else MAX_INTERVAL
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nDone.")
    elif cmd == "insights":
        show_insights()
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()