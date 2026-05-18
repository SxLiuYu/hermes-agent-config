#!/usr/bin/env python3
"""
Hermes Health Dashboard v2
==========================
SSE 实时推送 + 全组件指标 + 依赖关系图

升级:
  - SSE (Server-Sent Events): /api/stream 实时推送无需轮询
  - 全指标: 覆盖所有 v2 组件 (voice, sync, intel, memory, proactive)
  - 依赖图: 服务间依赖关系可视化
  - /api/all: 一次性获取所有指标 JSON

启动:
  python3 health_dashboard.py
  访问: http://localhost:8765
"""

import json
import os
import sqlite3
import time
import yaml
import subprocess
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SERVICES_FILE = HERMES_HOME / "services.yaml"
TRIGGERS_FILE = HERMES_HOME / "triggers.yaml"
STATE_FILE = HERMES_HOME / "trigger_state.json"
SEAL_DB = HERMES_HOME / "memory_seal.db"
INTEL_DB = HERMES_HOME / "conversation_intel.db"
OBSIDIAN_VAULT = Path.home() / "obsidian-vault"
PORT = 8765


# ---- Data collectors ----

def get_services():
    if not SERVICES_FILE.exists():
        return []
    cfg = yaml.safe_load(SERVICES_FILE.read_text())
    return [{"id":k,"name":svc.get("name",k),"category":svc.get("category","other"),
             "status":svc.get("status","unknown"),"auto_fetch":svc.get("auto_fetch",False)}
            for k,svc in cfg.get("services",{}).items()]


def get_triggers():
    result = {"rules":[],"total_fired":0,"daemon":False,"baseline":{}}
    if TRIGGERS_FILE.exists():
        cfg = yaml.safe_load(TRIGGERS_FILE.read_text())
        result["rules"] = [{"id":t.get("id","?"),"name":t.get("name","?"),
            "source":t.get("source","?"),"enabled":t.get("enabled",True)}
            for t in cfg.get("triggers",[])]
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        result["total_fired"] = len(s.get("triggers",{}))
        result["baseline"] = s.get("baselines",{})
    r = subprocess.run(["pgrep","-f","trigger_engine.py daemon"],capture_output=True)
    result["daemon"] = r.returncode == 0
    return result


def get_memory():
    if not SEAL_DB.exists():
        return {"draft":0,"scored":0,"sealed":0,"compressed":0,"links":0,"topics":[]}
    conn = sqlite3.connect(str(SEAL_DB))
    draft = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='draft'").fetchone()[0]
    scored = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='scored'").fetchone()[0]
    sealed = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='sealed'").fetchone()[0]
    compressed = conn.execute("SELECT COUNT(*) FROM chunks WHERE compressed_at IS NOT NULL").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM chunk_links").fetchone()[0]
    topics = conn.execute("SELECT name,chunk_count,avg_score,importance FROM topics ORDER BY avg_score DESC LIMIT 8").fetchall()
    conn.close()
    return {"draft":draft,"scored":scored,"sealed":sealed,"compressed":compressed,"links":links,
            "topics":[{"name":t[0],"count":t[1],"score":round(t[2],2),"importance":round(t[3] if t[3] else 0.5,2)} for t in topics]}


def get_user_state():
    if not INTEL_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(INTEL_DB))
        row = conn.execute("SELECT valence,arousal,dominance,fatigue_score,focus_score,dominant_topic FROM user_state ORDER BY timestamp DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            return {"valence":round(row[0],2),"arousal":round(row[1],2),"dominance":round(row[2],2),
                    "fatigue":round(row[3],2),"focus":round(row[4],2),"topic":row[5]}
    except:
        pass
    return {}


def get_components():
    """Check all component daemons and their status."""
    comps = {
        "trigger_engine": {"pattern":"trigger_engine.py daemon","desc":"Trigger Engine v2"},
        "conversation_intel": {"pattern":"conversation_intel.py daemon","desc":"Conversation Intel v2"},
        "proactive_engine": {"pattern":"proactive_engine.py daemon","desc":"Proactive Engine v2"},
        "event_bus": {"pattern":"event_bus.py start","desc":"Event Bus"},
        "native_voice": {"pattern":"native_voice.py wake","desc":"Native Voice"},
        "device_sync": {"pattern":"device_sync.py daemon","desc":"Device Sync v2"},
        "dashboard": {"pattern":"health_dashboard.py","desc":"Health Dashboard"},
        "auto_fetch": {"pattern":"auto_fetch.py","desc":"Auto Fetch"},
    }
    result = []
    for name, info in comps.items():
        r = subprocess.run(["pgrep","-f",info["pattern"]],capture_output=True)
        result.append({"name":name,"desc":info["desc"],"running":r.returncode==0})
    return result


def get_events(limit=10):
    events_dir = OBSIDIAN_VAULT / "Events"
    if not events_dir.exists():
        return []
    files = sorted(events_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    result = []
    for f in files:
        title = "Event"
        for line in f.read_text().split("\n"):
            if line.startswith("# "):
                title = line.replace("# ","").strip()
                break
        result.append({"file":f.name,"title":title,
            "time":datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M:%S")})
    return result


def get_proactive_insights(limit=5):
    log = HERMES_HOME / "proactive_insights.json"
    if not log.exists(): return []
    try:
        data = json.loads(log.read_text())
        return [{"message":i.get("message","?")[:200],"time":i.get("generated_at","?")[:16],
                 "type":i.get("type","?")} for i in data[-limit:]]
    except: return []


def get_all_metrics():
    """Complete metrics snapshot."""
    return {
        "timestamp": datetime.now().isoformat(),
        "services": {"total":len(get_services()),
            "connected":sum(1 for s in get_services() if s["status"]=="connected")},
        "triggers": get_triggers(),
        "memory": get_memory(),
        "user_state": get_user_state(),
        "components": get_components(),
        "events": get_events(5),
        "insights": get_proactive_insights(3),
    }


# ---- Dependency graph ----

DEPENDENCY_GRAPH = [
    {"from":"trigger_engine","to":"event_bus","label":"publishes events"},
    {"from":"conversation_intel","to":"trigger_engine","label":"sends alerts"},
    {"from":"proactive_engine","to":"state.db","label":"reads messages"},
    {"from":"proactive_engine","to":"conversation_intel","label":"reads user state"},
    {"from":"memory_seal","to":"conversation_intel","label":"reads topic importance"},
    {"from":"auto_fetch","to":"services.yaml","label":"fetches data"},
    {"from":"device_sync","to":"aliyun","label":"rsync/scp"},
    {"from":"device_sync","to":"phone","label":"scp"},
    {"from":"native_voice","to":"omlx","label":"LLM/custom"},
    {"from":"native_voice","to":"finnA","label":"LLM fallback"},
]


# ---- HTML ----

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Dashboard v2</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#0d1117; color:#c9d1d9; padding:20px; }
h1 { font-size:24px; margin-bottom:8px; color:#58a6ff; }
.subtitle { font-size:12px; color:#8b949e; margin-bottom:20px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(350px,1fr)); gap:20px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:20px; }
.card h2 { font-size:16px; margin-bottom:12px; color:#f0f6fc; }
.badge { display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600; margin:2px; }
.badge-connected { background:#1a7f37; color:#fff; }
.badge-available { background:#30363d; color:#8b949e; }
.badge-running { background:#1a7f37; color:#fff; }
.badge-stopped { background:#30363d; color:#8b949e; }
.stat-bar { display:flex; gap:15px; margin-bottom:20px; flex-wrap:wrap; }
.stat { text-align:center; flex:1; min-width:80px; }
.stat .num { font-size:28px; font-weight:700; color:#58a6ff; }
.stat .num.warn { color:#d2991d; }
.stat .label { font-size:11px; color:#8b949e; margin-top:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { padding:6px 10px; text-align:left; border-bottom:1px solid #21262d; }
th { color:#8b949e; font-weight:500; font-size:11px; text-transform:uppercase; }
tr:hover td { background:#1c2129; }
.progress-bar { height:6px; background:#21262d; border-radius:3px; margin:8px 0; overflow:hidden; }
.progress-fill { height:100%; border-radius:3px; transition:width 0.3s; }
.event-item { padding:4px 0; font-size:12px; border-bottom:1px solid #21262d; }
.event-time { color:#8b949e; margin-right:8px; }
.refresh { position:fixed; top:20px; right:20px; background:#238636; color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:13px; z-index:10; }
.status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.status-dot.on { background:#3fb950; }
.status-dot.off { background:#484f58; }
.footer { margin-top:30px; text-align:center; font-size:11px; color:#484f58; }
.category { font-size:11px; color:#58a6ff; text-transform:uppercase; margin-bottom:6px; margin-top:4px; }
.dep-node { display:inline-block; padding:4px 10px; border-radius:12px; font-size:11px; margin:2px; }
.dep-node.up { background:#1a7f37; color:#fff; }
.dep-node.down { background:#30363d; color:#8b949e; }
</style>
</head>
<body>
<h1>Hermes Health Dashboard v2</h1>
<div class="subtitle" id="ts">SSE real-time | {timestamp}</div>
<button class="refresh" onclick="location.reload()">Refresh</button>

<div class="stat-bar" id="statbar">
    <div class="stat"><div class="num">{connected}</div><div class="label">Connected</div></div>
    <div class="stat"><div class="num">{total_services}</div><div class="label">Services</div></div>
    <div class="stat"><div class="num">{triggers_active}</div><div class="label">Triggers</div></div>
    <div class="stat"><div class="num">{components_up}</div><div class="label">Components</div></div>
    <div class="stat"><div class="num">{sealed}</div><div class="label">Sealed</div></div>
</div>

<div class="grid">
    <!-- Services -->
    <div class="card"><h2>Services ({connected}/{total_services})</h2>{service_cards}</div>

    <!-- Components -->
    <div class="card"><h2>Components ({components_up}/{components_total})</h2>{component_rows}</div>

    <!-- Triggers -->
    <div class="card"><h2>Triggers ({triggers_active} active)</h2>
        <div style="margin-bottom:10px">Daemon: <span class="badge {daemon_class}">{daemon_label}</span> Fired: {triggers_fired}</div>
        {trigger_rows}
    </div>

    <!-- Memory -->
    <div class="card"><h2>Memory Pipeline</h2>
        <div class="progress-bar"><div class="progress-fill" style="width:{mem_pct}%;background:#238636"></div></div>
        <div style="display:flex;gap:15px;margin:10px 0">
            <div class="stat"><div class="num">{draft}</div><div class="label">Draft</div></div>
            <div class="stat"><div class="num">{scored}</div><div class="label">Scored</div></div>
            <div class="stat"><div class="num">{sealed}</div><div class="label">Sealed</div></div>
            <div class="stat"><div class="num">{compressed}</div><div class="label">Compressed</div></div>
        </div>
        {topic_rows}
    </div>

    <!-- User State -->
    <div class="card"><h2>User State</h2>{user_state_html}</div>

    <!-- Events -->
    <div class="card"><h2>Recent Events</h2>{event_rows}</div>

    <!-- Insights -->
    <div class="card"><h2>Proactive Insights</h2>{insight_rows}</div>

    <!-- Dependencies -->
    <div class="card"><h2>Dependency Graph</h2>{dep_rows}</div>
</div>

<div class="footer">Hermes Dashboard v2 &middot; SSE auto-update</div>

<script>
const src = new EventSource('/api/stream');
src.onmessage = function(e) {
    const data = JSON.parse(e.data);
    document.getElementById('ts').textContent = 'SSE real-time | ' + data.timestamp.slice(11,19);
    // Update stat bar
    const stats = document.getElementById('statbar').children;
    if(data.services) {
        stats[0].querySelector('.num').textContent = data.services.connected;
        stats[1].querySelector('.num').textContent = data.services.total;
    }
    if(data.components) {
        const up = data.components.filter(c=>c.running).length;
        stats[3].querySelector('.num').textContent = up;
    }
};
src.onerror = function() { /* reconnect on error */ };
</script>
</body>
</html>"""


def build_html():
    services = get_services()
    triggers = get_triggers()
    memory = get_memory()
    components = get_components()
    events = get_events()
    insights = get_proactive_insights()
    user = get_user_state()

    connected = sum(1 for s in services if s["status"]=="connected")
    total_svc = len(services)
    comp_up = sum(1 for c in components if c["running"])
    comp_total = len(components)
    ta = sum(1 for t in triggers["rules"] if t["enabled"])

    # Services by category
    cats = {}
    for s in services:
        cats.setdefault(s["category"],[]).append(s)
    svc_cards = ""
    for cat,items in cats.items():
        cat_conn = sum(1 for i in items if i["status"]=="connected")
        svc_cards += f'<div class="category">{cat} ({cat_conn}/{len(items)})</div>'
        for item in items:
            bcls = "connected" if item["status"]=="connected" else "available"
            auto = "auto" if item.get("auto_fetch") else ""
            svc_cards += f'<span class="badge badge-{bcls}">{item["name"]} {auto}</span> '
        svc_cards += "<br>"

    # Components
    comp_rows = ""
    for c in components:
        bcls = "running" if c["running"] else "stopped"
        dot = "on" if c["running"] else "off"
        comp_rows += f'<tr><td><span class="status-dot {dot}"></span>{c["name"]}</td><td>{c["desc"]}</td><td><span class="badge badge-{bcls}">{"UP" if c["running"] else "DOWN"}</span></td></tr>'

    # Triggers
    trig_rows = ""
    for t in triggers["rules"]:
        icon = "on" if t["enabled"] else "off"
        trig_rows += f'<tr><td><span class="status-dot {icon}"></span>{t["name"]}</td><td>{t["source"]}</td></tr>'
    if not trig_rows:
        trig_rows = '<tr><td colspan="2" style="color:#8b949e">No triggers</td></tr>'

    # Memory
    mt = memory["draft"]+memory["scored"]+memory["sealed"]+memory["compressed"]
    mem_pct = min(100,int((memory["sealed"]/max(mt,1))*100))
    topic_rows = ""
    for t in memory["topics"]:
        topic_rows += f'<tr><td>{t["name"]}</td><td>{t["count"]}</td><td>{t["score"]:.2f}</td><td>{t["importance"]:.2f}</td></tr>'

    # User state
    us_html = ""
    if user:
        topic = user.get("topic","?")
        v,a,d,fatigue,focus = user.get("valence",0),user.get("arousal",0),user.get("dominance",0),user.get("fatigue",0),user.get("focus",0)
        us_html = f"""
        <div class="stat-bar"><div class="stat"><div class="num">{v:.1f}</div><div class="label">Valence</div></div>
        <div class="stat"><div class="num">{a:.1f}</div><div class="label">Arousal</div></div>
        <div class="stat"><div class="num"{' class="warn"' if fatigue>0.5 else ''}>{fatigue:.2f}</div><div class="label">Fatigue</div></div>
        <div class="stat"><div class="num">{focus:.2f}</div><div class="label">Focus</div></div></div>
        <div style="font-size:12px;color:#8b949e">Topic: {topic}</div>"""
    else:
        us_html = '<div style="color:#8b949e">No user state data</div>'

    # Events
    ev_rows = ""
    for e in events:
        ev_rows += f'<div class="event-item"><span class="event-time">{e["time"]}</span>{e["title"]}</div>'
    if not ev_rows:
        ev_rows = '<div class="event-item" style="color:#8b949e">No recent events</div>'

    # Insights
    ins_rows = ""
    for i in insights:
        ins_rows += f'<div class="event-item"><span class="event-time">{i["time"]}</span>{i["message"]}</div>'
    if not ins_rows:
        ins_rows = '<div class="event-item" style="color:#8b949e">No insights yet</div>'

    # Dependency graph
    comp_status = {c["name"]:c["running"] for c in components}
    dep_rows = ""
    for dep in DEPENDENCY_GRAPH:
        f = dep["from"]
        up = comp_status.get(f, False)
        cls = "up" if up else "down"
        dep_rows += f'<span class="dep-node {cls}">{f}</span> <span style="color:#8b949e;font-size:10px">-{dep["label"]}-></span> <span class="dep-node up">{dep["to"]}</span><br>'

    daemon_cls = "running" if triggers["daemon"] else "stopped"
    daemon_lbl = "UP" if triggers["daemon"] else "DOWN"

    return HTML.format(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        connected=connected, total_services=total_svc,
        triggers_active=ta, triggers_fired=triggers["total_fired"],
        components_up=comp_up, components_total=comp_total,
        sealed=memory["sealed"], compressed=memory["compressed"],
        draft=memory["draft"], scored=memory["scored"],
        service_cards=svc_cards, component_rows=comp_rows,
        trigger_rows=trig_rows, mem_pct=mem_pct, topic_rows=topic_rows,
        user_state_html=us_html, event_rows=ev_rows, insight_rows=ins_rows,
        dep_rows=dep_rows, daemon_class=f"badge-{daemon_cls}", daemon_label=daemon_lbl,
    )


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = build_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif self.path == "/api/all":
            data = get_all_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        elif self.path == "/api/stream":
            # SSE endpoint: push metrics every 5 seconds
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    data = get_all_metrics()
                    self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_all_metrics(), ensure_ascii=False).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    print("Hermes Dashboard v2")
    print(f"  http://localhost:{PORT}")
    print("  /api/all     All metrics JSON")
    print("  /api/stream  SSE real-time")
    print("  Ctrl+C to stop\n")

    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()