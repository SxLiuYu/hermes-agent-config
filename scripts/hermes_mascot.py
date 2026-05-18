#!/usr/bin/env python3
"""
Hermes Mascot v2 — 桌面精灵 · 实时情绪 + Lip-Sync + 全生态联动
=================================================================
浮动精灵窗口，实时镜像 Hermes 内部状态：

v2 新增:
  - 从 conversation_intel 读取情绪（开心/焦虑/疲惫/兴奋）
  - event_bus 监听 key moments (voice-start/voice-end/thinking/alarm/meeting-done)
  - voice_sync 跟 native_voice 联动 — 说话时张嘴动画
  - 状态栏显示 health_dashboard 摘要（CPU/内存/服务数）
  - 点击交互: 右键菜单打开各组件
  - macOS: tkinter 窗口 → 半透明浮动; 无 GUI 时 ASCII fallback
  - 动画: 情绪渐变、lip-sync 表情序列、横幅弹出

用法:
  python3 hermes_mascot.py                # GUI / ASCII 自适应
  python3 hermes_mascot.py --ascii         # 强制 ASCII 模式
  python3 hermes_mascot.py --pos TL|TR|BL|BR  # 屏幕位置
"""

import os
import sys
import time
import json
import threading
from pathlib import Path
from datetime import datetime

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SCRIPTS = HERMES_HOME / "scripts"
EVENT_STREAM = HERMES_HOME / "event_stream.jsonl"   # event_bus 输出
INTEL_STATE = HERMES_HOME / "conversation_intel_state.json"
TRIGGER_STATE = HERMES_HOME / "trigger_state.json"
DASHBOARD_CACHE = HERMES_HOME / ".dashboard_cache.json"

# ── Emotion Map ──
EMOTION_FACES = {
    "happy":      {"emoji": "😊", "color": "#44cc88", "desc": "开心"},
    "excited":    {"emoji": "🤩", "color": "#ffaa00", "desc": "兴奋"},
    "curious":    {"emoji": "🤔", "color": "#4488cc", "desc": "好奇"},
    "focused":    {"emoji": "🧐", "color": "#8844cc", "desc": "专注"},
    "tired":      {"emoji": "😴", "color": "#888888", "desc": "疲惫"},
    "anxious":    {"emoji": "😰", "color": "#cc8844", "desc": "焦虑"},
    "neutral":    {"emoji": "🤖", "color": "#4488cc", "desc": "就绪"},
    "surprised":  {"emoji": "😲", "color": "#cc44cc", "desc": "惊讶"},
    "sad":        {"emoji": "😢", "color": "#6688cc", "desc": "低落"},
    "proud":      {"emoji": "😎", "color": "#44aacc", "desc": "得意"},
}

LIPSYNC_FRAMES = {
    "talking":   ["😀", "😃", "😄", "😁", "😆"],    # 5帧张嘴序列
    "listening": ["👂", "👂"],
    "thinking":  ["🤔", "🧐", "🤔"],
    "idle":      ["🤖"],
}

# ── State Machine ──
class MascotState:
    def __init__(self):
        self.emotion = "neutral"
        self.activity = "idle"          # idle / listening / talking / thinking / working / alarm
        self.voice_synced = False
        self.dashboard = {}
        self.event_history = []          # last 20 events
        self.lock = threading.Lock()

    def update_from_intel(self):
        """从 conversation_intel_state 读取用户情绪."""
        if INTEL_STATE.exists():
            try:
                data = json.loads(INTEL_STATE.read_text())
                mood = data.get("user_mood", "neutral")
                if mood in EMOTION_FACES:
                    self.emotion = mood
                # 关注度
                focus = data.get("attention_level", 0.5)
                if focus > 0.8:
                    self.activity = "thinking"
            except Exception:
                pass

    def update_from_events(self, max_events=20):
        """读取 event_stream 最近事件."""
        if not EVENT_STREAM.exists():
            return
        try:
            lines = []
            with open(EVENT_STREAM) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
            recent = lines[-max_events:]
            self.event_history = []
            for line in recent:
                try:
                    self.event_history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

            # 分析最近事件
            for ev in self.event_history[-5:]:
                event_type = ev.get("type", "")
                if event_type == "voice.start":
                    self.activity = "listening"
                elif event_type == "voice.end":
                    self.activity = "thinking"
                elif event_type == "tts.start":
                    self.activity = "talking"
                    self.voice_synced = True
                elif event_type == "tts.end":
                    self.voice_synced = False
                    self.activity = "idle"
                elif event_type == "alarm":
                    self.activity = "alarm"
                elif event_type == "meeting.done":
                    self.activity = "working"  # 处理完会议

        except Exception:
            pass

    def update_dashboard(self):
        """读取 health dashboard 缓存摘要."""
        if DASHBOARD_CACHE.exists():
            try:
                data = json.loads(DASHBOARD_CACHE.read_text())
                # 只取摘要字段
                self.dashboard = {
                    "cpu": data.get("cpu_percent", 0),
                    "mem": data.get("memory_percent", 0),
                    "services_up": data.get("services_up", 0),
                    "services_total": data.get("services_total", 0),
                    "token_saved": data.get("tokens_saved_today", 0),
                }
            except Exception:
                pass

    def get_display(self) -> dict:
        """返回当前显示状态."""
        with self.lock:
            return {
                "emotion": self.emotion,
                "face": EMOTION_FACES.get(self.emotion, EMOTION_FACES["neutral"]),
                "activity": self.activity,
                "voice": self.voice_synced,
                "dashboard": self.dashboard,
            }


# ── ASCII Renderer ──
class ASCIIRenderer:
    """终端 ASCII 渲染器 — 彩色表情 + 状态栏."""

    def __init__(self):
        self.frame_idx = 0
        self.last_lines = 0

    def clear(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    def render(self, display: dict):
        face_info = display["face"]
        emotion = display["emotion"]
        activity = display["activity"]
        voice = display["voice"]
        dash = display["dashboard"]

        # Lip-sync 帧
        if activity == "talking" and voice:
            frames = LIPSYNC_FRAMES["talking"]
            emoji = frames[self.frame_idx % len(frames)]
            self.frame_idx += 1
        elif activity == "listening":
            emoji = "👂"
        elif activity == "thinking":
            emoji = "🤔"
        elif activity == "alarm":
            emoji = "🚨"
        elif activity == "working":
            emoji = "⚙️"
        else:
            emoji = face_info["emoji"]

        lines = []
        lines.append("")
        lines.append(f"     {emoji}  Hermes")
        lines.append(f"     情绪: {face_info['desc']}  |  状态: {activity}")
        lines.append("     " + "─" * 30)

        # 仪表盘摘要
        if dash:
            services = f"{dash.get('services_up', 0)}/{dash.get('services_total', 0)}"
            cpu = dash.get("cpu", 0)
            mem = dash.get("mem", 0)
            tokens = dash.get("token_saved", 0)
            lines.append(f"     🖥 CPU:{cpu}%  MEM:{mem}%  🟢{services}  💰{tokens}tk")
        else:
            lines.append("     (dashboard unavailable)")

        lines.append("     " + "─" * 30)
        lines.append(f"     {datetime.now().strftime('%H:%M:%S')}  |  q=退出")

        # 清除上一帧
        if self.last_lines:
            sys.stdout.write(f"\033[{self.last_lines}A")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self.last_lines = len(lines) + 1


# ── macOS GUI Renderer (when tkinter available) ──
class MacOSRenderer:
    """macOS tkinter 浮动窗口 — 半透明 + 可拖拽."""

    def __init__(self, position="BR"):
        self.position = position
        self.root = None
        self.label = None
        self.info_label = None
        self.dash_label = None
        self.frame_idx = 0

    def setup(self):
        try:
            import tkinter as tk
        except ImportError:
            return False

        self.root = tk.Tk()
        self.root.title("Hermes")
        self.root.overrideredirect(True)  # 无边框
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)  # 半透明
        self.root.configure(bg="#1a1a2e")

        # 窗口大小
        w, h = 200, 100
        # 定位
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos_map = {"TL": (20, 40), "TR": (sw - w - 20, 40),
                   "BL": (20, sh - h - 60), "BR": (sw - w - 20, sh - h - 60)}
        x, y = pos_map.get(self.position, (sw - w - 20, sh - h - 60))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Emoji label
        self.label = tk.Label(self.root, text="🤖", font=("Apple Color Emoji", 32),
                              bg="#1a1a2e", fg="#4488cc")
        self.label.pack(pady=(8, 0))

        # Info label
        self.info_label = tk.Label(self.root, text="就绪", font=("Helvetica", 10),
                                   bg="#1a1a2e", fg="#888888")
        self.info_label.pack()

        # Mini dashboard
        self.dash_label = tk.Label(self.root, text="", font=("Helvetica", 8),
                                   bg="#1a1a2e", fg="#666666")
        self.dash_label.pack()

        # 拖拽
        self.label.bind("<Button-1>", self.start_drag)
        self.label.bind("<B1-Motion>", self.do_drag)

        # 右键菜单
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Health Dashboard", command=lambda: os.system("open http://localhost:9100"))
        menu.add_command(label="Quit", command=self.root.destroy)
        self.label.bind("<Button-2>", lambda e: menu.post(e.x_root, e.y_root))
        self.label.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))

        self._drag_x = 0
        self._drag_y = 0
        return True

    def start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def render(self, display: dict):
        if not self.root:
            return
        face_info = display["face"]
        activity = display["activity"]
        voice = display["voice"]

        # Lip-sync
        if activity == "talking" and voice:
            frames = LIPSYNC_FRAMES["talking"]
            emoji = frames[self.frame_idx % len(frames)]
            self.frame_idx += 1
        elif activity == "listening":
            emoji = "👂"
        elif activity == "thinking":
            emoji = "🤔"
        elif activity == "alarm":
            emoji = "🚨"
        else:
            emoji = face_info["emoji"]

        color = face_info["color"]
        self.label.config(text=emoji, fg=color)
        self.info_label.config(text=f"{face_info['desc']} · {activity}")

        dash = display["dashboard"]
        if dash:
            services = f"{dash.get('services_up', 0)}/{dash.get('services_total', 0)}"
            cpu = dash.get("cpu", 0)
            mem = dash.get("mem", 0)
            self.dash_label.config(
                text=f"CPU:{cpu:.0f}% MEM:{mem:.0f}% 🟢{services}"
            )

        self.root.update()


# ── Mascot Daemon ──
class HermesMascot:
    def __init__(self, position="BR", force_ascii=False):
        self.state = MascotState()
        self.running = True
        self.renderer = None
        self.force_ascii = force_ascii

        # Try GUI first
        if not force_ascii and sys.platform == "darwin":
            gui = MacOSRenderer(position)
            if gui.setup():
                self.renderer = gui
                print("🖥 Hermes Mascot — macOS GUI mode")

        if self.renderer is None:
            self.renderer = ASCIIRenderer()
            print("📺 Hermes Mascot — ASCII mode")

    def run(self):
        """主循环."""
        print("🐾 Hermes Mascot v2 启动")
        print("   情绪联动: conversation_intel")
        print("   事件联动: event_bus")
        print("   仪表盘:  health_dashboard")
        print("   按 Ctrl+C 退出\n")

        poll_interval = 0.3  # 300ms for smooth animation

        try:
            while self.running:
                # 从各组件读取状态
                self.state.update_from_intel()
                self.state.update_from_events()
                self.state.update_dashboard()

                # 渲染
                display = self.state.get_display()
                self.renderer.render(display)

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n👋 Hermes Mascot stopped")
        finally:
            if isinstance(self.renderer, MacOSRenderer) and self.renderer.root:
                try:
                    self.renderer.root.destroy()
                except Exception:
                    pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Mascot v2")
    parser.add_argument("--pos", default="BR", choices=["TL", "TR", "BL", "BR"],
                        help="屏幕位置")
    parser.add_argument("--ascii", action="store_true",
                        help="强制 ASCII 模式")
    args = parser.parse_args()

    mascot = HermesMascot(position=args.pos, force_ascii=args.ascii)
    mascot.run()


if __name__ == "__main__":
    main()