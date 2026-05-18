#!/usr/bin/env python3
"""
Hermes Menu Bar v2
===================
实时 Dashboard 指标 + v2 组件状态 + 事件流

升级:
  - 实时指标: 从 /api/all 拉取 dashboard 数据
  - 组件状态: v2 组件 up/down 实时显示
  - 用户状态: VAD 情绪 + 疲劳 + 话题
  - 快捷: 一键启动/停止各组件

依赖: pip3 install rumps pyobjc

用法:
  python3 hermes_tray.py          # 前台
  python3 hermes_tray.py --bg     # 后台
"""

import os
import sys
import json
import yaml
import subprocess
import urllib.request
from pathlib import Path

try:
    import rumps
except ImportError:
    subprocess.run(["pip3", "install", "rumps", "pyobjc-framework-Cocoa"], capture_output=True)
    import rumps

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_DB = HERMES_HOME / "state.db"
SERVICES_FILE = HERMES_HOME / "services.yaml"
TRIGGERS_FILE = HERMES_HOME / "triggers.yaml"
OBSIDIAN_VAULT = Path.home() / "obsidian-vault"
DASHBOARD_URL = "http://localhost:8765/api/all"

FACES = {
    "idle": "😴", "active": "🤖", "excited": "😎",
    "alert": "🚨", "thinking": "🤔", "happy": "😊", "busy": "💪",
}

V2_COMPONENTS = [
    ("event_bus", "event_bus.py start"),
    ("dashboard", "health_dashboard.py"),
    ("trigger", "trigger_engine.py daemon"),
    ("intel", "conversation_intel.py daemon"),
    ("proactive", "proactive_engine.py daemon"),
    ("sync", "device_sync.py daemon"),
    ("voice", "native_voice.py"),
    ("tray", "hermes_tray.py"),
    ("mascot", "hermes_mascot.py"),
]


def fetch_dashboard_metrics():
    """Pull real-time metrics from health dashboard v2."""
    try:
        req = urllib.request.urlopen(DASHBOARD_URL, timeout=3)
        return json.loads(req.read())
    except:
        return {}


def count_connected_services():
    if not SERVICES_FILE.exists():
        return 0
    try:
        cfg = yaml.safe_load(SERVICES_FILE.read_text())
        return sum(1 for s in cfg.get("services", {}).values() if s.get("status") == "connected")
    except:
        return 0


def get_recent_events(limit=5):
    events_dir = OBSIDIAN_VAULT / "Events"
    if not events_dir.exists():
        return []
    files = sorted(events_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    result = []
    for f in files:
        for line in f.read_text().split("\n"):
            if line.startswith("# "):
                result.append(line.replace("# ", "").strip())
                break
    return result


def check_component(name, pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return r.returncode == 0


class HermesTrayApp(rumps.App):
    def __init__(self):
        super().__init__(name="Hermes", title="🤖", quit_button="Quit")
        self.current_face = "idle"
        self.last_events = []
        self.metrics = {}
        self.setup_menu()
        self.timer = rumps.Timer(self.refresh_status, 5)
        self.timer.start()

    def setup_menu(self):
        self.status_title = rumps.MenuItem("Hermes v2")
        self.menu.add(self.status_title)
        self.menu.add(rumps.separator)

        # Live metrics
        self.services_item = rumps.MenuItem("Services: ...")
        self.menu.add(self.services_item)
        self.components_item = rumps.MenuItem("Components: ...")
        self.menu.add(self.components_item)
        self.memory_item = rumps.MenuItem("Memory: ...")
        self.menu.add(self.memory_item)

        self.menu.add(rumps.separator)

        # User state (from dashboard)
        self.user_item = rumps.MenuItem("User: ...")
        self.menu.add(self.user_item)

        self.menu.add(rumps.separator)

        # Quick actions
        self.menu.add(rumps.MenuItem("Run Auto-Fetch", callback=self.run_auto_fetch))
        self.menu.add(rumps.MenuItem("Check Triggers", callback=self.check_triggers))
        self.menu.add(rumps.MenuItem("Open Dashboard", callback=self.open_dashboard))
        self.menu.add(rumps.MenuItem("Open Obsidian", callback=self.open_obsidian))

        self.menu.add(rumps.separator)

        # Events submenu
        self.events_menu = rumps.MenuItem("Recent Events")
        self.menu.add(self.events_menu)

        self.menu.add(rumps.separator)

        # Component management
        self.menu.add(rumps.MenuItem("Start All Components", callback=self.start_all))
        self.menu.add(rumps.MenuItem("Stop All Components", callback=self.stop_all))

    @rumps.timer(5)
    def refresh_status(self, _):
        # Fetch real-time metrics from dashboard
        m = fetch_dashboard_metrics()

        n_services = count_connected_services()
        self.services_item.title = f"Services: {n_services} connected"

        # Components up/down
        if m.get("components"):
            up = sum(1 for c in m["components"] if c.get("running"))
            total = len(m["components"])
            self.components_item.title = f"Components: {up}/{total} up"

            # Show per-component status
            comps = m["components"]
            down_list = [c["name"] for c in comps if not c.get("running")]
            if down_list:
                self.title = FACES["alert"]
            elif up == total:
                self.title = FACES["happy"]
            else:
                self.title = FACES["active"]
        else:
            self.components_item.title = "Components: N/A"

        # Memory
        if m.get("memory"):
            mem = m["memory"]
            self.memory_item.title = f"Memory: {mem.get('sealed',0)} sealed, {mem.get('scored',0)} scored"

        # User state
        user = m.get("user_state", {})
        if user:
            topic = user.get("topic", "?")
            fatigue = user.get("fatigue", 0)
            v = user.get("valence", 0)
            fatigue_str = "tired" if fatigue > 0.5 else "ok"
            self.user_item.title = f"User: [{topic}] {fatigue_str} (V={v:.1f})"
        else:
            self.user_item.title = "User: no data"

        # Events
        events = get_recent_events(5)
        if events != self.last_events:
            self.last_events = events
            self.update_events_menu(events)

    def update_events_menu(self, events):
        self.events_menu.clear()
        if not events:
            self.events_menu.add(rumps.MenuItem("  (none)"))
        else:
            for e in events:
                self.events_menu.add(rumps.MenuItem(f"  {e[:50]}"))

    def run_auto_fetch(self, _):
        s = HERMES_HOME / "scripts" / "auto_fetch.py"
        if s.exists():
            subprocess.Popen(["python3", str(s), "--once"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rumps.notification("Auto-Fetch v2", "Running...", "Polling connected services")

    def check_triggers(self, _):
        s = HERMES_HOME / "scripts" / "trigger_engine.py"
        if s.exists():
            r = subprocess.run(["python3", str(s), "once"], capture_output=True, text=True, timeout=30)
            fired = r.stdout.count("Triggered:")
            rumps.notification("Trigger Engine v2", f"{fired} trigger(s) fired", "")

    def open_dashboard(self, _):
        subprocess.Popen(["open", "http://localhost:8765"])

    def open_obsidian(self, _):
        subprocess.Popen(["open", str(OBSIDIAN_VAULT)])

    def start_all(self, _):
        s = HERMES_HOME / "scripts" / "hermes_desktop.py"
        if s.exists():
            subprocess.Popen(["python3", str(s), "start"])
            rumps.notification("Hermes Desktop", "Starting all components...", "")

    def stop_all(self, _):
        s = HERMES_HOME / "scripts" / "hermes_desktop.py"
        if s.exists():
            subprocess.Popen(["python3", str(s), "stop"])
            rumps.notification("Hermes Desktop", "Stopping all components...", "")


def main():
    bg = "--bg" in sys.argv
    if bg:
        pid = os.fork()
        if pid > 0:
            print(f"Tray started (PID: {pid})")
            return
    HermesTrayApp().run()


if __name__ == "__main__":
    main()