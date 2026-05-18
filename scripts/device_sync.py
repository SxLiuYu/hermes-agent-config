#!/usr/bin/env python3
"""
Hermes Device Sync — 跨设备同步 v2
====================================
Mac Mini <-> 阿里云 <-> 手机 三端状态同步

协议:
  一写多读: Mac Mini 是主节点 (唯一写)
  拉取模式: 阿里云/手机定期从 Mac 拉取
  rsync 首选, scp 降级 (termux 手机无 rsync)

同步内容 (从 services.yaml 自动发现 + 硬编码补丁):
  trigger_state.json    P1  5分
  services.yaml         P2  30分
  memory_seal.db        P3  1小时
  conversation_intel.db P3  30分
  Obsidian vault        P4  30分

用法:
  python3 device_sync.py push --to aliyun
  python3 device_sync.py pull --from aliyun
  python3 device_sync.py daemon
  python3 device_sync.py status
  python3 device_sync.py discover       # 显示自动发现的同步项
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SERVICES_YAML = HERMES_HOME / "services.yaml"
SYNC_STATE = HERMES_HOME / "sync_state.json"

DEVICES = {
    "aliyun": {
        "host": "123.57.107.21",
        "user": "root",
        "port": 22,
        "hermes_path": "/root/.hermes",
        "description": "阿里云 ECS",
        "use_scp": False,
    },
    "singapore": {
        "host": "43.134.39.26",
        "user": "ubuntu",
        "port": 22,
        "hermes_path": "/home/ubuntu/.hermes",
        "description": "新加坡代理",
        "use_scp": False,
    },
    "phone": {
        "host": "192.168.1.3",
        "user": "u0_a121",
        "port": 8022,
        "hermes_path": "/data/data/com.termux/files/home/.hermes",
        "description": "手机触手 (termux)",
        "use_scp": True,  # Termux has no rsync
    },
}

# Manual sync items (priority, interval_seconds, direction)
MANUAL_SYNC_ITEMS = [
    ("trigger_state.json", 1, 300, "push"),
    ("triggers.yaml", 1, 600, "push"),
    ("memory_seal.db", 3, 3600, "push"),
    ("conversation_intel.db", 3, 1800, "push"),
]


def auto_discover_sync_items() -> list:
    """Discover sync items from services.yaml + .hermes/scripts analysis."""
    items = list(MANUAL_SYNC_ITEMS)  # start with manual base

    # From services.yaml: auto-fetch enabled services
    if SERVICES_YAML.exists():
        try:
            cfg = yaml.safe_load(SERVICES_YAML.read_text())
            services = cfg.get("services", {})

            for name, svc in services.items():
                if isinstance(svc, dict) and svc.get("auto_fetch"):
                    svc_file = f"services/{name}.json"
                    interval = svc.get("fetch_interval", 1800)
                    # Priority based on interval: faster = higher
                    priority = 2 if interval <= 600 else 3
                    items.append((svc_file, priority, interval, "push"))
        except Exception:
            pass

    # Auto-detect any state files in ~/.hermes
    for fname in ["proactive_insights.json", "trigger_log.json", "event_log.json"]:
        p = HERMES_HOME / fname
        if p.exists() and not any(fname == it[0] for it in items):
            items.append((fname, 2, 1800, "push"))

    return items


def load_state() -> dict:
    if SYNC_STATE.exists():
        try:
            return json.loads(SYNC_STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_sync": {}, "checksums": {}, "errors": []}


def save_state(state: dict):
    SYNC_STATE.write_text(json.dumps(state, indent=2, default=str))


def file_checksum(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def ssh_cmd(device: str, cmd: str, timeout: int = 30) -> tuple:
    cfg = DEVICES.get(device)
    if not cfg:
        return -1, f"Unknown device: {device}"
    ssh = [
        "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
        "-p", str(cfg["port"]), f"{cfg['user']}@{cfg['host']}", cmd,
    ]
    try:
        result = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return -1, "Timeout"
    except Exception as e:
        return -1, str(e)


def check_device(device: str) -> bool:
    code, _ = ssh_cmd(device, "echo ok")
    return code == 0


def check_rsync_available(device: str) -> bool:
    """Check if rsync is available on remote device."""
    code, out = ssh_cmd(device, "which rsync || echo 'not found'")
    return code == 0 and "not found" not in out


def rsync_push(local_path: str, device: str, remote_path: str) -> bool:
    """Push using rsync."""
    cfg = DEVICES[device]
    local = HERMES_HOME / local_path
    if not local.exists():
        return False
    cmd = [
        "rsync", "-avz", "--timeout=10",
        "-e", f"ssh -p {cfg['port']} -o ConnectTimeout=5",
        str(local),
        f"{cfg['user']}@{cfg['host']}:{remote_path}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


def scp_push(local_path: str, device: str, remote_path: str) -> bool:
    """Push using scp (fallback for termux/no-rsync)."""
    cfg = DEVICES[device]
    local = HERMES_HOME / local_path
    if not local.exists():
        return False
    cmd = [
        "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
        "-P", str(cfg["port"]),
        str(local),
        f"{cfg['user']}@{cfg['host']}:{remote_path}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


def sync_file_to_device(local_path: str, device: str) -> bool:
    """Smart sync: rsync first, scp fallback."""
    cfg = DEVICES[device]
    remote = f"{cfg['hermes_path']}/{local_path}"

    # Create remote directory if needed
    remote_dir = str(Path(remote).parent)
    ssh_cmd(device, f"mkdir -p {remote_dir}")

    if cfg.get("use_scp"):
        return scp_push(local_path, device, remote)
    else:
        # Try rsync, fallback to scp
        if rsync_push(local_path, device, remote):
            return True
        return scp_push(local_path, device, remote)


def push_to_device(device: str, force: bool = False):
    if not check_device(device):
        print(f"❌ {device} unreachable")
        return

    state = load_state()
    now = time.time()
    synced = 0
    skipped = 0

    items = auto_discover_sync_items()
    print(f"  📋 {len(items)} sync items available")

    for local_path, priority, interval, direction in items:
        if direction not in ("push", "both"):
            continue

        key = f"{device}:{local_path}"
        last = state["last_sync"].get(key, 0)
        if not force and now - last < interval:
            skipped += 1
            continue

        local = HERMES_HOME / local_path
        if not local.exists():
            continue

        if force or file_checksum(local) != state["checksums"].get(key, ""):
            success = sync_file_to_device(local_path, device)
            if success:
                state["last_sync"][key] = now
                state["checksums"][key] = file_checksum(local)
                synced += 1

    save_state(state)
    print(f"  📤 {device}: {synced} synced, {skipped} skipped")


def pull_from_device(device: str, force: bool = False):
    if not check_device(device):
        print(f"❌ {device} unreachable")
        return

    state = load_state()
    now = time.time()
    synced = 0

    items = auto_discover_sync_items()
    for local_path, priority, interval, direction in items:
        if direction not in ("pull", "both"):
            continue

        key = f"pull:{device}:{local_path}"
        last = state["last_sync"].get(key, 0)
        if not force and now - last < interval:
            continue

        cfg = DEVICES[device]
        remote_file = f"{cfg['hermes_path']}/{local_path}"

        # Pull via scp
        cmd = [
            "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-P", str(cfg["port"]),
            f"{cfg['user']}@{cfg['host']}:{remote_file}",
            str(HERMES_HOME / local_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                state["last_sync"][key] = now
                synced += 1
        except Exception:
            pass

    save_state(state)
    print(f"  📥 {device}: {synced} synced")


def sync_daemon():
    print("🔄 Device Sync — daemon mode")
    print(f"   Devices: {list(DEVICES.keys())}")
    print(f"   Sync items: {len(auto_discover_sync_items())}\n")

    while True:
        for device in DEVICES:
            if check_device(device):
                push_to_device(device)
        time.sleep(60)


def show_status():
    print("\n📡 Device Sync Status\n")

    for name, cfg in DEVICES.items():
        online = check_device(name)
        status = "🟢" if online else "⚪"
        transport = "scp" if cfg.get("use_scp") else "rsync"
        print(f"  {status} {name:12s} {cfg['host']:20s} [{transport}] {cfg['description']}")

    state = load_state()
    if state["last_sync"]:
        print("\n  Last Sync:")
        for key, ts in sorted(state["last_sync"].items(), key=lambda x: x[1], reverse=True)[:5]:
            dt = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            print(f"    {dt}  {key}")

    items = auto_discover_sync_items()
    print(f"\n  📋 Auto-discovered {len(items)} sync items")

    print()


def cmd_discover():
    """Show all discovered sync items."""
    items = auto_discover_sync_items()
    print(f"\n📋 Sync Items ({len(items)} total)\n")
    print(f"  {'Item':35s} {'Prio':5s} {'Interval':>8s}  {'Dir'}")
    print(f"  {'-'*35} {'-'*5} {'-'*8}  {'-'*5}")
    for path, prio, interval, direction in items:
        local = HERMES_HOME / path
        status = "✅" if local.exists() else "❌"
        mins = f"{interval//60}m" if interval >= 60 else f"{interval}s"
        print(f"  {path:35s} {status} P{prio}  {mins:>6s}  {direction}")


def main():
    if len(sys.argv) < 2:
        print("Hermes Device Sync v2")
        print("\nCommands:")
        print("  push --to <device>    Push to device")
        print("  pull --from <device>  Pull from device")
        print("  daemon                Auto-sync daemon")
        print("  status                Show status")
        print("  discover              Show discovered items")
        return

    cmd = sys.argv[1]

    if cmd == "push":
        device = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--to" else "aliyun"
        push_to_device(device)
    elif cmd == "pull":
        device = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--from" else "aliyun"
        pull_from_device(device)
    elif cmd == "daemon":
        sync_daemon()
    elif cmd == "status":
        show_status()
    elif cmd == "discover":
        cmd_discover()
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()