#!/usr/bin/env python3
"""
Hermes Desktop Launcher v2
===========================
一键启动全套 Hermes v2 管线。

升级:
  - 更新所有 v2 组件参数
  - 集成 dashboard 指标
  - 组件依赖顺序启动

用法:
  python3 hermes_desktop.py start    # 启动所有
  python3 hermes_desktop.py stop     # 停止所有
  python3 hermes_desktop.py status   # 查看状态
  python3 hermes_desktop.py install  # 注册 LaunchAgent
"""

import os
import sys
import time
import signal
import subprocess
import json
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SCRIPTS_DIR = HERMES_HOME / "scripts"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_FILE = LAUNCH_AGENTS / "com.hermes.desktop.plist"
PID_FILE = HERMES_HOME / "desktop_pids.json"

# v2 Components with dependencies: start order matters
COMPONENTS = {
    "event-bus": {
        "script": "event_bus.py", "args": ["start"], "desc": "Event Bus (pub/sub)",
        "required": True, "group": "infra",
    },
    "dashboard": {
        "script": "health_dashboard.py", "args": [], "desc": "Dashboard v2 (:8765)",
        "required": True, "group": "infra",
    },
    "trigger-daemon": {
        "script": "trigger_engine.py", "args": ["daemon"], "desc": "Trigger Engine v2 (5 sources)",
        "required": True, "group": "core", "depends": ["event-bus"],
    },
    "conversation-intel": {
        "script": "conversation_intel.py", "args": ["daemon"], "desc": "Conversation Intel v2 (oMLX)",
        "required": True, "group": "core",
    },
    "proactive": {
        "script": "proactive_engine.py", "args": ["daemon"], "desc": "Proactive Engine v2 (adaptive)",
        "required": True, "group": "core", "depends": ["conversation-intel"],
    },
    "device-sync": {
        "script": "device_sync.py", "args": ["daemon"], "desc": "Device Sync v2 (auto+scp)",
        "required": False, "group": "optional",
    },
    "native-voice": {
        "script": "native_voice.py", "args": ["wake"], "desc": "Native Voice (porcupine+stream)",
        "required": False, "group": "optional",
    },
    "tray": {
        "script": "hermes_tray.py", "args": ["--bg"], "desc": "Menu Bar v2",
        "required": False, "group": "ui",
    },
    "mascot": {
        "script": "hermes_mascot.py", "args": [], "desc": "Desktop Mascot",
        "required": False, "group": "ui",
    },
}


def save_pids(pids):
    PID_FILE.write_text(json.dumps(pids, indent=2))


def load_pids():
    if PID_FILE.exists():
        try: return json.loads(PID_FILE.read_text())
        except: return {}
    return {}


def is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_start(components=None):
    if components is None:
        # Start required first, then optional
        required = [n for n, c in COMPONENTS.items() if c["required"]]
        optional = [n for n, c in COMPONENTS.items() if not c["required"]]
        components = required + optional

    pids = load_pids()
    started = {}

    print(f"\n  Hermes Desktop v2\n  {'='*30}")
    for name in components:
        cfg = COMPONENTS.get(name)
        if not cfg:
            print(f"  Unknown: {name}")
            continue

        existing = pids.get(name)
        if existing and is_running(existing):
            print(f"  {cfg['desc']:40s} UP (PID:{existing})")
            started[name] = existing
            continue

        script = SCRIPTS_DIR / cfg["script"]
        if not script.exists():
            print(f"  {cfg['desc']:40s} FILE MISSING")
            continue

        print(f"  {cfg['desc']:40s}", end=" ")
        try:
            proc = subprocess.Popen(
                ["python3", str(script)] + cfg["args"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started[name] = proc.pid
            print(f"PID:{proc.pid}")
            time.sleep(0.5)  # Stagger startups
        except Exception as e:
            print(f"FAIL: {e}")

    save_pids(started)
    print(f"  {'='*30}\n  Started: {len(started)}/{len(components)}\n")
    return started


def cmd_stop():
    pids = load_pids()
    stopped = 0
    for name, pid in pids.items():
        cfg = COMPONENTS.get(name, {})
        if is_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.3)
                if is_running(pid):
                    os.kill(pid, signal.SIGKILL)
                print(f"  Stopped: {cfg.get('desc', name)} (PID:{pid})")
                stopped += 1
            except:
                pass
        else:
            print(f"  Stale: {cfg.get('desc', name)}")
    # Kill remaining
    for pattern in ["trigger_engine.py daemon", "event_bus.py start", "health_dashboard.py"]:
        subprocess.run(["pkill", "-f", pattern], capture_output=True)
    save_pids({})
    print(f"\n  Stopped: {stopped}\n")


def cmd_status():
    pids = load_pids()
    print(f"\n  Hermes Desktop v2 Status\n  {'='*40}")

    for group in ["infra", "core", "optional", "ui"]:
        items = [(n, c) for n, c in COMPONENTS.items() if c.get("group") == group]
        if not items:
            continue
        print(f"\n  [{group.upper()}]")
        for name, cfg in items:
            pid = pids.get(name)
            if pid and is_running(pid):
                s = "UP"
            else:
                s = "DOWN"
            print(f"  {'🟢' if s == 'UP' else '⚪'} {cfg['desc']:38s} {s}")

    # Dashboard quick stats
    try:
        import sqlite3
        seal_db = HERMES_HOME / "memory_seal.db"
        if seal_db.exists():
            conn = sqlite3.connect(str(seal_db))
            sealed = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='sealed'").fetchone()[0]
            conn.close()
            print(f"\n  Memory: {sealed} sealed chunks")
    except:
        pass

    try:
        import yaml
        svc = HERMES_HOME / "services.yaml"
        if svc.exists():
            cfg = yaml.safe_load(svc.read_text())
            conn = sum(1 for s in cfg.get("services", {}).values() if s.get("status") == "connected")
            print(f"  Services: {conn} connected")
    except:
        pass


def cmd_install():
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    launcher = SCRIPTS_DIR / "hermes_desktop.py"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.desktop</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{launcher}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{HERMES_HOME}/desktop.log</string>
    <key>StandardErrorPath</key>
    <string>{HERMES_HOME}/desktop.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>
</dict>
</plist>"""
    PLIST_FILE.write_text(plist)
    subprocess.run(["launchctl", "load", str(PLIST_FILE)], capture_output=True)
    print(f"  LaunchAgent installed: {PLIST_FILE}")


def cmd_uninstall():
    if PLIST_FILE.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_FILE)], capture_output=True)
        PLIST_FILE.unlink()
        print("  LaunchAgent removed.")


def main():
    if len(sys.argv) < 2:
        print("Hermes Desktop Launcher v2\n")
        print("  start     Start all components")
        print("  stop      Stop all")
        print("  status    Show status")
        print("  install   Auto-start on login")
        return

    cmd = sys.argv[1]
    if cmd == "start":
        components = sys.argv[2:] if len(sys.argv) > 2 else None
        cmd_start(components)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()