#!/usr/bin/env python3
"""
Hermes Event Bus v2
====================
统一 pub/sub + v2 组件双向互通 + trigger 桥接

升级:
  - 桥接触发: trigger_engine produce ↔ consume events
  - v2 topic: intel.state_changed, intel.delta, device.sync, voice.*
  - subscribe CLI + wildcard filter
  - event 历史查询 API

用法:
  python3 event_bus.py start
  python3 event_bus.py publish <topic> <json>
  python3 event_bus.py subscribe <pattern>  # 终端订阅
  python3 event_bus.py stats
"""

import json
import os
import sqlite3
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from collections import defaultdict

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
BUS_DB = HERMES_HOME / "event_bus.db"

TTL = {
    # Trigger
    "trigger.fired": 3600, "trigger.cooldown": 600, "trigger.polled": 300,
    "trigger.baseline_update": 86400,
    # Intel v2
    "intel.state_changed": 3600, "intel.delta_alert": 1800,
    "intel.emotion.shift": 1800, "intel.fatigue.spike": 1800,
    # Device sync
    "device.sync.completed": 3600, "device.sync.error": 1800,
    "device.status": 600,
    # Voice
    "voice.wake.detected": 300, "voice.stt.completed": 600,
    "voice.tts.played": 600, "voice.error": 1800,
    # Auto fetch
    "auto_fetch.completed": 7200, "auto_fetch.error": 3600,
    "auto_fetch.empty": 1800,
    # Memory
    "memory.chunk_created": 86400, "memory.chunk_sealed": 86400 * 30,
    "memory.chunk_compressed": 86400, "memory.chunk_pruned": 3600,
    # Proactive
    "proactive.insight": 86400, "proactive.component_down": 1800,
    "proactive.topic_surge": 3600,
    # Service
    "service.connected": 86400 * 7, "service.disconnected": 3600,
    "service.health": 3600, "service.stale": 1800,
    # Desktop
    "desktop.started": 86400, "desktop.stopped": 86400,
    "desktop.component.up": 3600, "desktop.component.down": 1800,
    # Skill
    "skill.used": 86400 * 7, "skill.error": 86400 * 3,
    # User
    "user.correction": 86400 * 30, "user.feedback": 86400,
}

_subscribers = defaultdict(set)
_sub_lock = threading.Lock()


def init_db():
    conn = sqlite3.connect(str(BUS_DB), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
        data TEXT NOT NULL, created_at REAL NOT NULL, ttl INTEGER DEFAULT 3600)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic,created_at)")
    conn.commit()
    return conn


def cleanup(conn):
    conn.execute("DELETE FROM events WHERE created_at + ttl < ?", (time.time(),))
    conn.commit()


def publish(conn, topic, data, ttl_override=None):
    ttl = ttl_override or TTL.get(topic, 3600)
    now = time.time()
    conn.execute("INSERT INTO events (topic,data,created_at,ttl) VALUES (?,?,?,?)",
        (topic, json.dumps(data, ensure_ascii=False, default=str), now, ttl))
    conn.commit()
    with _sub_lock:
        for pattern, cbs in _subscribers.items():
            if _match(pattern, topic):
                for cb in cbs:
                    try: cb(topic, data)
                    except: pass


def _match(pattern, topic):
    if pattern == "*": return True
    if pattern.endswith(".*"): return topic.startswith(pattern[:-2])
    return pattern == topic


def subscribe(pattern, callback):
    with _sub_lock:
        _subscribers[pattern].add(callback)


def unsubscribe(pattern, callback):
    with _sub_lock:
        _subscribers[pattern].discard(callback)


def query(conn, topic=None, limit=50, since=None):
    params = []
    sql = "SELECT topic,data,created_at FROM events WHERE 1=1"
    if topic:
        sql += " AND topic LIKE ?"
        params.append(topic.replace("*","%"))
    if since:
        sql += " AND created_at > ?"
        params.append(since)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{"topic":r[0], "data":json.loads(r[1]), "time":r[2]} for r in rows]


def get_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    topics = conn.execute("SELECT topic,COUNT(*) as cnt FROM events GROUP BY topic ORDER BY cnt DESC").fetchall()
    latest = conn.execute("SELECT topic,data,created_at FROM events ORDER BY created_at DESC LIMIT 10").fetchall()
    return {"total_events":total,
        "topic_counts":[{"topic":t[0],"count":t[1]} for t in topics],
        "latest":[{"topic":r[0],"data":json.loads(r[1]),"time":r[2]} for r in latest]}


# ---- Bridge to trigger_engine ----

def bridge_trigger_fired(conn, trigger_id, name, source, message):
    """Emit trigger.fired — consumed by tray/mascot/dashboard."""
    publish(conn, "trigger.fired", {"trigger_id":trigger_id,"name":name,"source":source,"message":message})


def bridge_intel_delta(conn, alert_type, delta_val, topic, message):
    """Emit intel delta alert — consumed by proactive_engine."""
    publish(conn, f"intel.{alert_type}", {"delta":delta_val,"topic":topic,"message":message})


def bridge_device_sync(conn, target, status, detail=""):
    publish(conn, "device.sync.completed", {"target":target,"status":status,"detail":detail})


def bridge_voice(conn, event_type, detail=""):
    publish(conn, f"voice.{event_type}", {"detail":detail})


# ---- CLI ----

def cmd_start():
    conn = init_db()
    print("Event Bus v2 started")
    t = threading.Thread(target=lambda: [time.sleep(300), cleanup(conn)], daemon=True)
    t.start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopped")
        conn.close()


def cmd_publish(topic, data_json):
    conn = init_db()
    try: data = json.loads(data_json)
    except: data = {"raw": data_json}
    publish(conn, topic, data)
    print(f"Published: {topic}")
    conn.close()


def cmd_subscribe(pattern):
    conn = init_db()
    last_id = conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
    print(f"Subscribing to: {pattern} (Ctrl+C to stop)\n")

    def on_event(topic, data):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] {topic:35s} {json.dumps(data,ensure_ascii=False)[:80]}")

    subscribe(pattern, on_event)
    try:
        while True:
            new = conn.execute("SELECT topic,data FROM events WHERE id>?", (last_id,)).fetchall()
            for row in new:
                try: on_event(row[0], json.loads(row[1]))
                except: pass
            last_id = conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or last_id
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nUnsubscribed")


def cmd_stats():
    conn = init_db()
    stats = get_stats(conn)
    print("\nEvent Bus v2 Stats")
    print(f"  Total: {stats['total_events']}")
    print("\n  Topics:")
    for t in stats["topic_counts"][:15]:
        print(f"    {t['topic']:35s} {t['count']:>6}")
    if stats["latest"]:
        print("\n  Latest:")
        for e in stats["latest"][:5]:
            ts = datetime.fromtimestamp(e["time"]).strftime("%H:%M:%S")
            print(f"    [{ts}] {e['topic']:35s} {json.dumps(e['data'],ensure_ascii=False)[:60]}")
    conn.close()


def cmd_query(topic, limit=20):
    conn = init_db()
    for e in query(conn, topic, limit):
        ts = datetime.fromtimestamp(e["time"]).strftime("%H:%M:%S")
        print(f"  [{ts}] {e['topic']} | {json.dumps(e['data'],ensure_ascii=False)[:100]}")
    conn.close()


def main():
    if len(sys.argv) < 2:
        print("Event Bus v2\n  start | publish <topic> <json> | subscribe <pattern> | stats | query <topic>")
        return
    cmd = sys.argv[1]
    if cmd == "start": cmd_start()
    elif cmd == "publish" and len(sys.argv) >= 4: cmd_publish(sys.argv[2], sys.argv[3])
    elif cmd == "subscribe" and len(sys.argv) >= 3: cmd_subscribe(sys.argv[2])
    elif cmd == "stats": cmd_stats()
    elif cmd == "query" and len(sys.argv) >= 3: cmd_query(sys.argv[2])
    else: print("Usage: event_bus.py <start|publish|subscribe|stats|query>")


if __name__ == "__main__":
    main()