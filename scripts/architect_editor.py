#!/usr/bin/env python3
"""
Architect/Editor Mode — 分离规划与执行

对标 Aider Architect/Editor:
  - Architect 模式：只分析、规划、输出方案，不修改文件
  - Editor 模式：基于已有的方案执行修改

OpenCode 也有 Plan/Build 两种 primary agent，理念一致。

用法:
  python3 scripts/architect_editor.py mode architect  # 切换到规划模式
  python3 scripts/architect_editor.py mode editor      # 切换到执行模式
  python3 scripts/architect_editor.py status           # 查看当前模式

机制:
  - 在 ~/.hermes/mode 文件中记录当前模式
  - Architect 模式下，hook 拦截所有 write_file/patch 调用
  - Editor 模式下，基于 ~/.hermes/plans/ 中的最新方案执行
"""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
MODE_FILE = HERMES_HOME / "mode.json"
PLANS_DIR = HERMES_HOME / "plans"

MODE_DEFAULTS = {
    "architect": {
        "description": "规划模式：只分析和输出方案，不修改文件",
        "icon": "🏗️",
        "rules": [
            "不执行 write_file、patch、terminal(rm)、删除操作",
            "可以读取文件、搜索、浏览网页",
            "输出完整的实施方案，保存到 plans/ 目录",
            "每个方案必须包含：目标、步骤、风险、预估时间",
        ],
        "allowed_tools": ["read_file", "search_files", "web_search", "web_extract",
                         "terminal(ls)", "terminal(cat)", "terminal(git)", "memory",
                         "clarify", "skill_view", "skills_list", "session_search",
                         "browser_navigate", "browser_snapshot"],
        "blocked_tools": ["write_file", "patch", "terminal(rm)", "terminal(mv)",
                         "terminal(git push)", "delegate_task", "diff_sandbox(rollback)"],
    },
    "editor": {
        "description": "执行模式：基于已有方案执行修改",
        "icon": "🔨",
        "rules": [
            "加载 plans/ 目录下最新的方案作为行动依据",
            "严格按方案的步骤执行，不偏离",
            "每步完成后更新方案的状态（steps[].done=true）",
            "遇到问题时暂停并报告，提出修改建议",
        ],
        "allowed_tools": ["*"],
        "blocked_tools": [],
    },
    "normal": {
        "description": "正常模式：规划+执行不分离",
        "icon": "🧠",
        "rules": [],
        "allowed_tools": ["*"],
        "blocked_tools": [],
    },
}


class ModeManager:
    """管理 Agent 工作模式"""

    def __init__(self):
        self.mode_file = MODE_FILE
        self._load()

    def _load(self):
        if self.mode_file.exists():
            try:
                self.data = json.loads(self.mode_file.read_text())
            except Exception:
                self.data = {"mode": "normal", "active_since": None}
        else:
            self.data = {"mode": "normal", "active_since": None}

    def _save(self):
        self.mode_file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def set_mode(self, mode: str):
        """切换模式"""
        if mode not in MODE_DEFAULTS:
            print(f"❌ 未知模式: {mode}")
            print(f"   支持的模式: {', '.join(MODE_DEFAULTS.keys())}")
            return 1

        old_mode = self.data["mode"]
        self.data["mode"] = mode
        self.data["active_since"] = datetime.now(timezone.utc).isoformat()
        self.data["previous_mode"] = old_mode
        self._save()

        cfg = MODE_DEFAULTS[mode]
        print(f"\n{cfg['icon']} 已切换到 **{mode}** 模式 — {cfg['description']}")

        if mode == "architect":
            print("\n📋 规则:")
            for r in cfg["rules"]:
                print(f"   • {r}")
            print(f"\n🛠️ 可用工具: {len(cfg['allowed_tools'])}")
            print(f"🚫 禁止工具: {len(cfg['blocked_tools'])}")

    def get_mode(self) -> dict:
        """获取当前模式配置"""
        mode = self.data.get("mode", "normal")
        return {
            "mode": mode,
            "active_since": self.data.get("active_since"),
            "config": MODE_DEFAULTS.get(mode, MODE_DEFAULTS["normal"]),
        }

    def print_status(self):
        """打印当前状态"""
        info = self.get_mode()
        cfg = info["config"]
        since = info["active_since"]
        if since:
            since_short = since[:19].replace("T", " ")
        else:
            since_short = "session start"

        print(f"{cfg['icon']} 当前模式: **{info['mode']}**")
        print(f"   {cfg['description']}")
        print(f"   激活时间: {since_short}")

    def is_tool_allowed(self, tool_name: str) -> bool:
        """检查工具是否允许使用"""
        cfg = MODE_DEFAULTS.get(self.data.get("mode", "normal"), MODE_DEFAULTS["normal"])
        allowed = cfg["allowed_tools"]
        blocked = cfg["blocked_tools"]

        # "*" 表示全部允许
        if "*" in allowed:
            return tool_name not in blocked
        if tool_name in blocked:
            return False
        # 检查前缀匹配
        for a in allowed:
            if tool_name.startswith(a.split("(")[0]):
                return True
        return False


def generate_mode_context(mode: str) -> str:
    """生成模式上下文，注入到 agent prompt"""
    if mode == "normal":
        return ""

    cfg = MODE_DEFAULTS.get(mode, {})
    rules = "\n".join(f"- {r}" for r in cfg.get("rules", []))
    blocked = ", ".join(cfg.get("blocked_tools", []))
    allowed = ", ".join(cfg.get("allowed_tools", []))

    return f"""
## 🎯 Active Mode: {mode.upper()}

**{cfg.get('description', '')}**

### Rules (MUST follow):
{rules}

### Tool Constraints:
- Allowed: {allowed}
- Blocked: {blocked}
"""


def main():
    parser = argparse.ArgumentParser(
        description="Architect/Editor Mode — 分离规划与执行"
    )
    sub = parser.add_subparsers(dest="command")

    mode_p = sub.add_parser("mode", help="切换或查看模式")
    mode_p.add_argument("mode_name", nargs="?", choices=["architect", "editor", "normal"],
                       help="模式名称（不提供则查看当前）")

    sub.add_parser("status", help="查看当前模式")

    # 生成 hook 配置
    sub.add_parser("install-hook", help="安装 PreToolUse hook（architect 模式下拦截工具）")

    args = parser.parse_args()
    mgr = ModeManager()

    if args.command == "mode":
        if args.mode_name:
            return mgr.set_mode(args.mode_name)
        else:
            mgr.print_status()
    elif args.command == "status":
        mgr.print_status()
    elif args.command == "install-hook":
        install_hook()
    else:
        parser.print_help()


def install_hook():
    """安装 PreToolUse hook 来拦截被禁用的工具"""
    hook_dir = HERMES_HOME / "hooks" / "pre_tool_use"
    hook_dir.mkdir(parents=True, exist_ok=True)

    hook_script = hook_dir / "architect-mode-guard.py"
    content = """#!/usr/bin/env python3
'''
Architect Mode Guard — 在 Architect 模式下拦截 write_file/patch 等工具
由 architect_editor.py install-hook 自动安装
'''

import json
import os
import sys
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
MODE_FILE = HERMES_HOME / "mode.json"

# 从 stdin 读取工具调用信息
try:
    input_data = sys.stdin.read()
    if input_data:
        tool_info = json.loads(input_data)
    else:
        sys.exit(0)
except Exception:
    sys.exit(0)

# 检查当前模式
if MODE_FILE.exists():
    try:
        mode_data = json.loads(MODE_FILE.read_text())
        current_mode = mode_data.get("mode", "normal")
    except Exception:
        current_mode = "normal"
else:
    current_mode = "normal"

# 正常模式或编辑模式，放行
if current_mode != "architect":
    sys.exit(0)

# Architect 模式下的拦截
tool_name = tool_info.get("tool_name", "")

BLOCKED = ["write_file", "patch", "diff_sandbox(rollback)"]

# 检查是否被拦截
for b in BLOCKED:
    base = b.split("(")[0]
    if tool_name.startswith(base):
        print(f"\\n🏗️ [ARCHITECT MODE] 已拦截 {tool_name} 调用")
        print(f"   如需执行，请切换模式: python3 scripts/architect_editor.py mode editor")
        sys.exit(1)

sys.exit(0)
"""
    hook_script.write_text(content)
    hook_script.chmod(0o755)
    print("✅ PreToolUse hook 已安装到 hooks/pre_tool_use/architect-mode-guard.py")
    print("   在 Architect 模式下会自动拦截 write_file/patch 等工具调用")
    print()
    print("   在 Hermes 中注册:")
    print("   hooks_manage install pre_tool_use --script architect-mode-guard")


if __name__ == "__main__":
    exit(main() or 0)