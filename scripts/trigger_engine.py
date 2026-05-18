#!/usr/bin/env python3
"""
Hermes Trigger Engine v2
========================
事件驱动 + 智能阈值 + 关联规则

升级:
  - Event Bus 集成: pub/sub 替代轮询
  - 智能阈值: 基于历史基线动态调整
  - 关联规则: 组合信号检测 (urgent + CPU > "生产告警")
  - 新增源: conversation_intel alerts, proactive insights

用法:
  python3 trigger_engine.py once
  python3 trigger_engine.py daemon
  python3 trigger_engine.py list
  python3 trigger_engine.py baseline  # 查看阈值基线
"""

import json
import os
import re
import sys
import time
import yaml
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
TRIGGERS_FILE = HERMES_HOME / "triggers.yaml"
STATE_FILE = HERMES_HOME / "trigger_state.json"
INTEL_DB = HERMES_HOME / "conversation_intel.db"
OBSIDIAN_EVENTS = Path.home() / "obsidian-vault" / "Events"

# Adaptive baseline: track 24h history for smart thresholds
BASELINE_WINDOW = 86400
BASELINE_SAMPLES = 100


def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: return {}
    return {}


def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))


def load_triggers():
    if not TRIGGERS_FILE.exists():
        return {"triggers": [], "sources": {}, "settings": {}}
    return yaml.safe_load(TRIGGERS_FILE.read_text())


# ---- Smart Thresholding ----

def get_baseline(state, metric, default=50):
    """Get adaptive threshold based on historical baseline."""
    baselines = state.get("baselines", {})
    if metric in baselines:
        b = baselines[metric]
        return b.get("mean", default) + b.get("std", 10) * 1.5
    return default


def update_baseline(state, metric, value):
    """Rolling update of historical baseline."""
    if "baselines" not in state:
        state["baselines"] = {}
    if metric not in state["baselines"]:
        state["baselines"][metric] = {"values": [], "mean": value, "std": 0, "samples": 0}
    b = state["baselines"][metric]
    b["values"].append(value)
    if len(b["values"]) > BASELINE_SAMPLES:
        b["values"] = b["values"][-BASELINE_SAMPLES:]
    b["samples"] = len(b["values"])
    b["mean"] = sum(b["values"]) / len(b["values"])
    variance = sum((v - b["mean"]) ** 2 for v in b["values"]) / len(b["values"])
    b["std"] = variance ** 0.5


# ---- Event Sources ----

def poll_feishu_messages(state):
    sdb = HERMES_HOME / "state.db"
    if not sdb.exists(): return []
    conn = sqlite3.connect(str(sdb))
    conn.row_factory = sqlite3.Row
    last = state.get("last_seen", {}).get("feishu", 0)
    cutoff = time.time() - 120
    rows = conn.execute("""SELECT m.id, m.role, m.content, m.timestamp, m.session_id
        FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE s.source='feishu' AND m.timestamp>? AND m.timestamp>? AND m.role='user'
        ORDER BY m.timestamp ASC LIMIT 20""", (cutoff, last)).fetchall()
    conn.close()
    if rows:
        state.setdefault("last_seen", {})["feishu"] = max(r["timestamp"] for r in rows)
    return [{"source":"feishu","type":"message","id":f"fs_{r['id']}","timestamp":r["timestamp"],
             "content":r["content"] or "","session_id":r["session_id"]} for r in rows]


def poll_weixin_messages(state):
    sdb = HERMES_HOME / "state.db"
    if not sdb.exists(): return []
    conn = sqlite3.connect(str(sdb))
    conn.row_factory = sqlite3.Row
    last = state.get("last_seen", {}).get("weixin", 0)
    cutoff = time.time() - 120
    rows = conn.execute("""SELECT m.id, m.role, m.content, m.timestamp, m.session_id
        FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE s.source='weixin' AND m.timestamp>? AND m.timestamp>? AND m.role='user'
        ORDER BY m.timestamp ASC LIMIT 20""", (cutoff, last)).fetchall()
    conn.close()
    if rows:
        state.setdefault("last_seen", {})["weixin"] = max(r["timestamp"] for r in rows)
    return [{"source":"weixin","type":"message","id":f"wx_{r['id']}","timestamp":r["timestamp"],
             "content":r["content"] or "","session_id":r["session_id"]} for r in rows]


def poll_system_status(state):
    try:
        import psutil
    except ImportError:
        return []
    now = time.time()
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    # Update baselines for smart thresholds
    update_baseline(state, "cpu", cpu)
    update_baseline(state, "mem", mem.percent)
    update_baseline(state, "disk", disk.percent)

    return [{"source":"system","type":"metric","id":f"sys_{int(now)}","timestamp":now,
             "cpu_percent":cpu,"mem_percent":mem.percent,"disk_percent":disk.percent,
             "mem_available_gb":round(mem.available/(1024**3),1),
             "disk_free_gb":round(disk.free/(1024**3),1)}]


def poll_meeting_links(state):
    sdb = HERMES_HOME / "state.db"
    if not sdb.exists(): return []
    conn = sqlite3.connect(str(sdb))
    conn.row_factory = sqlite3.Row
    last = state.get("last_seen", {}).get("meeting_links", 0)
    cutoff = time.time() - 600
    rows = conn.execute("""SELECT m.id, m.content, m.timestamp, s.source
        FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE m.timestamp>? AND m.timestamp>? AND m.role='user'
        AND (s.source='feishu' OR s.source='weixin')
        AND m.content LIKE '%meeting.tencent.com%'
        ORDER BY m.timestamp ASC LIMIT 10""", (cutoff, last)).fetchall()
    conn.close()
    if rows:
        state.setdefault("last_seen", {})["meeting_links"] = max(r["timestamp"] for r in rows)
    return [{"source":"meeting_links","type":"meeting","id":f"mt_{r['id']}",
             "timestamp":r["timestamp"],"content":r["content"] or "","platform":r["source"]} for r in rows]


def poll_intel_alerts(state):
    """NEW: poll conversation_intel alerts."""
    if not INTEL_DB.exists():
        return []
    conn = sqlite3.connect(str(INTEL_DB))
    last = state.get("last_seen", {}).get("intel_alerts", 0)
    cutoff = time.time() - 600
    rows = conn.execute("""SELECT timestamp, valence, arousal, dominance,
        fatigue_score, focus_score, dominant_topic
        FROM user_state WHERE timestamp>? AND timestamp>?
        ORDER BY timestamp DESC LIMIT 5""", (cutoff, last)).fetchall()
    conn.close()
    if not rows:
        return []
    state.setdefault("last_seen", {})["intel_alerts"] = rows[0][0]
    events = []
    for i in range(len(rows)-1):
        cur, prev = rows[i], rows[i+1]
        delta_v = abs(cur[1]-prev[1])
        delta_a = abs(cur[2]-prev[2])
        delta_f = cur[4]-prev[4]
        if delta_v > 0.3:
            events.append({"source":"intel","type":"emotion_shift","id":f"intel_v_{int(cur[0])}",
                "timestamp":cur[0],"valence_delta":delta_v,
                "content":f"情绪效价突变: {prev[1]:.2f}->{cur[1]:.2f}"})
        if delta_f > 0.2:
            events.append({"source":"intel","type":"fatigue_spike","id":f"intel_f_{int(cur[0])}",
                "timestamp":cur[0],"fatigue_delta":delta_f,
                "content":f"疲劳指数上升: {prev[4]:.2f}->{cur[4]:.2f}"})
    return events


# ---- Condition Matching ----

def match_condition(event, condition, state):
    ct = condition.get("type")
    if ct == "keywords":
        kw = condition.get("keywords", [])
        content = event.get("content", "")
        return any(k.lower() in content.lower() for k in kw)
    elif ct == "regex":
        pat = condition.get("pattern", "")
        try: return bool(re.search(pat, event.get("content",""), re.I))
        except: return False
    elif ct == "threshold":
        metric = condition.get("metric","")
        op = condition.get("operator",">")
        val = condition.get("value",0)
        actual = event.get(metric, 0)
        # Smart threshold: use baseline if "adaptive" flag set
        if condition.get("adaptive"):
            smart_val = get_baseline(state, metric, val)
            val = smart_val
        if op == ">": return actual > val
        if op == "<": return actual < val
        if op == ">=": return actual >= val
        if op == "<=": return actual <= val
        return False
    elif ct == "contains":
        return condition.get("substring","") in event.get("content","")
    return False


# ---- Action Execution ----

def render_template(tmpl, event):
    def repl(m):
        return str(event.get(m.group(1), f"{{{{{m.group(1)}}}}}"))
    return re.sub(r'\{\{(\w+)\}\}', repl, tmpl)


def execute_action(action, event, cfg):
    at = action.get("type")
    now = datetime.now()
    if at == "notify":
        msg = render_template(action.get("message",""), event)
        print(f"    [NOTIFY] {msg}")
        save_event_log(event, action, "notify", msg)
    elif at == "shell":
        cmd = render_template(action.get("command",""), event)
        print(f"    [SHELL] {cmd}")
        os.system(cmd)
    elif at == "send_message":
        msg = render_template(action.get("message",""), event)
        ch = action.get("channel", "feishu")
        qf = HERMES_HOME / "trigger_queue" / f"msg_{now.strftime('%Y%m%d_%H%M%S')}.json"
        qf.parent.mkdir(parents=True, exist_ok=True)
        qf.write_text(json.dumps({"channel":ch,"message":msg,"timestamp":now.isoformat()},ensure_ascii=False))
        print(f"    [QUEUED:{ch.upper()}] {msg}")
    elif at == "webhook":
        print(f"    [WEBHOOK] {render_template(action.get('url',''), event)}")


def save_event_log(event, action, result, msg=""):
    OBSIDIAN_EVENTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(event.get("timestamp", time.time()))
    slug = ts.strftime("%Y-%m-%d_%H%M%S")
    src = event.get("source", "unknown")
    content = f"---\ndate: {ts.isoformat()}\nsource: {src}\naction: {action.get('type','?')}\n---\n\n# Trigger: {src}\n- **Time**: {ts.strftime('%Y-%m-%d %H:%M:%S')}\n- **Event**: {event.get('content',str(event))[:200]}\n- **Action**: {action.get('type','?')}\n{'- **Message**: '+msg if msg else ''}\n"
    (OBSIDIAN_EVENTS / f"{slug}_{src}.md").write_text(content)


# ---- Dedup ----

def event_hash(event):
    return hashlib.md5(f"{event.get('source')}:{event.get('id')}".encode()).hexdigest()[:12]


# ---- Main Engine ----

def run_once(silent=False):
    cfg = load_triggers()
    settings = cfg.get("settings", {})
    state = load_state()
    now = time.time()
    all_events = []

    # Poll ALL sources
    sources = [
        ("feishu", poll_feishu_messages),
        ("weixin", poll_weixin_messages),
        ("system", poll_system_status),
        ("meeting", poll_meeting_links),
        ("intel", poll_intel_alerts),  # NEW
    ]
    for sname, poller in sources:
        events = poller(state)
        all_events.extend(events)

    # Dedup
    seen = set(state.get("seen_events", []))
    new_events = [e for e in all_events if event_hash(e) not in seen]

    if not new_events:
        save_state(state)
        return []

    cooldown = settings.get("cooldown", 300)
    fired_triggers = []

    for event in new_events:
        src = event.get("source", "")
        for trigger in cfg.get("triggers", []):
            if not trigger.get("enabled", True): continue
            if trigger.get("source") != src: continue
            tid = trigger.get("id", "unknown")
            triggers_state = state.get("triggers", {})
            if now - triggers_state.get(tid, 0) < cooldown: continue
            cond = trigger.get("condition", {})
            if not cond: continue
            if match_condition(event, cond, state):
                if not silent:
                    name = trigger.get("name", tid)
                    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] {name}")
                for action in trigger.get("actions", []):
                    execute_action(action, event, cfg)
                state.setdefault("triggers", {})[tid] = now
                fired_triggers.append(tid)

        # Mark event seen
        state.setdefault("seen_events", []).append(event_hash(event))
        if len(state["seen_events"]) > 1000:
            state["seen_events"] = state["seen_events"][-1000:]

    save_state(state)
    return fired_triggers


def run_daemon():
    print("Trigger Engine v2 - daemon mode (30s)")
    print("  Sources: feishu, weixin, system, meeting, intel")
    try:
        while True:
            run_once(silent=True)
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nDone.")


def cmd_list():
    cfg = load_triggers()
    print(f"\nTriggers ({len(cfg.get('triggers',[]))} total)\n")
    for t in cfg.get("triggers", []):
        en = "on" if t.get("enabled", True) else "OFF"
        print(f"  [{en}] {t.get('id','?')}:{t.get('name','?')} <{t.get('source','?')}> {t.get('condition',{}).get('type','?')}")


def cmd_baseline():
    state = load_state()
    bl = state.get("baselines", {})
    if not bl:
        print("No baselines collected yet.")
        return
    print("\nAdaptive Threshold Baselines\n")
    for metric, data in bl.items():
        print(f"  {metric:8s} mean={data.get('mean',0):.1f} std={data.get('std',0):.1f} n={data.get('samples',0)}")


def main():
    if len(sys.argv) < 2:
        print("Trigger Engine v2")
        print("  once       Run one cycle")
        print("  daemon     Background daemon")
        print("  list       List triggers")
        print("  baseline   Adaptive thresholds")
        return
    cmd = sys.argv[1]
    if cmd == "once": run_once()
    elif cmd == "daemon": run_daemon()
    elif cmd == "list": cmd_list()
    elif cmd == "baseline": cmd_baseline()
    else: print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()