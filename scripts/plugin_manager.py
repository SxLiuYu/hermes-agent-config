#!/usr/bin/env python3
"""
Hermes Plugin Manager — 对标 Claude Code Plugin Marketplace

标准插件包结构:
  plugin-name/
  ├── manifest.json           # 插件元数据
  ├── skills/                 # SKILL.md 文件
  ├── commands/               # 斜杠命令
  ├── hooks/                  # 生命周期钩子
  ├── config.json             # 默认配置
  └── README.md               # 说明

  manifest.json 格式:
  {
    "name": "docker-tools",
    "version": "1.0.0",
    "description": "Docker 管理工具集",
    "author": "yujinze",
    "homepage": "https://github.com/yujinze/hermes-plugins",
    "license": "MIT",
    "hermes_version": ">=0.1.0",
    "skills": ["docker", "compose"],
    "hooks": ["pre-docker-clean"],
    "commands": ["docker-logs", "docker-prune"],
    "dependencies": {"python": [">=3.9"], "tools": ["docker"]}
  }

用法:
  hermes plugin install <name>       # 通过 marketplace 安装
  hermes plugin install <path>       # 从本地路径安装
  hermes plugin uninstall <name>     # 卸载
  hermes plugin list                 # 列出已安装
  hermes plugin marketplace add <repo>  # 添加 marketplace
"""

import json
import shutil
import sys
import subprocess
from pathlib import Path
from datetime import datetime

HERMES_HOME = Path.home() / ".hermes"
PLUGINS_DIR = HERMES_HOME / "plugins"
MARKETPLACES_FILE = HERMES_HOME / "config" / "marketplaces.json"
PLUGIN_INSTALL_LOG = HERMES_HOME / "logs" / "plugin_install.jsonl"


def load_manifest(plugin_path: Path) -> dict:
    """加载并验证 manifest.json"""
    manifest_file = plugin_path / "manifest.json"
    if not manifest_file.exists():
        raise FileNotFoundError(f"缺少 manifest.json: {manifest_file}")

    manifest = json.loads(manifest_file.read_text())

    required = ["name", "version", "description"]
    for key in required:
        if key not in manifest:
            raise ValueError(f"manifest.json 缺少必填字段: {key}")

    return manifest


def install_plugin(source: str, scope: str = "user") -> dict:
    """安装插件

    source 可以是:
      - 本地路径: /path/to/plugin
      - GitHub: github:user/repo
      - Git URL: https://github.com/...
    """
    temp_dir = None

    if source.startswith("github:") or source.startswith("https://"):
        # 从 Git 拉取
        repo = source.replace("github:", "").rstrip("/")
        if not repo.startswith("http"):
            repo = f"https://github.com/{repo}.git"

        temp_dir = HERMES_HOME / "tmp" / f"plugin-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                ["git", "clone", "--depth=1", repo, str(temp_dir)],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git clone 失败: {e.stderr.decode()}")

        plugin_path = temp_dir
    else:
        plugin_path = Path(source).expanduser().resolve()
        if not plugin_path.exists():
            raise FileNotFoundError(f"路径不存在: {source}")

    # 加载 manifest
    manifest = load_manifest(plugin_path)
    name = manifest["name"]

    # 目标目录
    target = PLUGINS_DIR / name

    # 冲突检测
    if target.exists():
        old_manifest = json.loads((target / "manifest.json").read_text()) if (target / "manifest.json").exists() else {}
        if old_manifest.get("version") == manifest["version"]:
            print(f"⚠️  插件 {name} v{manifest['version']} 已安装，跳过")
            return manifest

        # 备份旧版本
        backup = PLUGINS_DIR / f"{name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.move(str(target), str(backup))
        print(f"📦 备份旧版本到 {backup.name}")

    # 复制插件
    shutil.copytree(str(plugin_path), str(target), dirs_exist_ok=True)
    print(f"✅ 安装完成: {name} v{manifest['version']}")

    # 安装 skills
    skills_dir = target / "skills"
    if skills_dir.exists():
        for skill_file in skills_dir.glob("*/SKILL.md"):
            skill_name = skill_file.parent.name
            dest = HERMES_HOME / "skills" / skill_name
            if not dest.exists():
                shutil.copytree(str(skill_file.parent), str(dest))
                print(f"   📎 安装 skill: {skill_name}")
            else:
                print(f"   ⚠️  skill 已存在，跳过: {skill_name}")

    # 安装 hooks
    hooks_dir = target / "hooks"
    if hooks_dir.exists():
        for hook_type in hooks_dir.iterdir():
            if hook_type.is_dir():
                dest_base = HERMES_HOME / "hooks" / hook_type.name
                dest_base.mkdir(parents=True, exist_ok=True)
                for hook_file in hook_type.iterdir():
                    if hook_file.is_file():
                        dest = dest_base / hook_file.name
                        shutil.copy2(str(hook_file), str(dest))
                        print(f"   🪝 安装 hook: {hook_type.name}/{hook_file.name}")

    # 安装 commands
    commands_dir = target / "commands"
    if commands_dir.exists():
        dest_dir = HERMES_HOME / "commands"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for cmd_file in commands_dir.iterdir():
            if cmd_file.is_file():
                shutil.copy2(str(cmd_file), str(dest_dir / cmd_file.name))
                print(f"   ⌨️  安装命令: {cmd_file.name}")

    # 记录安装日志
    PLUGIN_INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "name": name,
        "version": manifest["version"],
        "action": "install",
        "timestamp": datetime.now().isoformat(),
        "source": source,
    }
    with open(PLUGIN_INSTALL_LOG, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # 清理临时目录
    if temp_dir and temp_dir.exists():
        shutil.rmtree(str(temp_dir))

    return manifest


def uninstall_plugin(name: str):
    """卸载插件"""
    target = PLUGINS_DIR / name
    if not target.exists():
        print(f"❌ 插件 {name} 未安装")
        return

    manifest_file = target / "manifest.json"
    manifest = {}
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text())

    # 移除 skills
    skills_dir = target / "skills"
    if skills_dir.exists():
        for skill_file in skills_dir.glob("*/SKILL.md"):
            skill_name = skill_file.parent.name
            dest = HERMES_HOME / "skills" / skill_name
            if dest.exists():
                shutil.rmtree(str(dest))
                print(f"   🗑️  移除 skill: {skill_name}")

    # 移除 hooks（基于 common prefix 安全删除）
    hooks_dir = target / "hooks"
    if hooks_dir.exists():
        for hook_type in hooks_dir.iterdir():
            if hook_type.is_dir():
                src_hooks = {f.name for f in hook_type.iterdir() if f.is_file()}
                dest_base = HERMES_HOME / "hooks" / hook_type.name
                if dest_base.exists():
                    for f in dest_base.iterdir():
                        if f.name in src_hooks:
                            f.unlink()
                            print(f"   🗑️  移除 hook: {hook_type.name}/{f.name}")

    # 删除插件目录
    shutil.rmtree(str(target))
    print(f"✅ 已卸载: {name} v{manifest.get('version', '?')}")

    # 记录日志
    log_entry = {
        "name": name,
        "version": manifest.get("version", "?"),
        "action": "uninstall",
        "timestamp": datetime.now().isoformat(),
    }
    with open(PLUGIN_INSTALL_LOG, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def list_plugins():
    """列出已安装插件"""
    if not PLUGINS_DIR.exists():
        print("📦 没有安装的插件")
        return

    plugins = []
    for d in sorted(PLUGINS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and not d.name.endswith(".bak-"):
            mf = d / "manifest.json"
            if mf.exists():
                try:
                    m = json.loads(mf.read_text())
                    plugins.append(m)
                except Exception:
                    plugins.append({"name": d.name, "version": "?", "description": "?"})

    if not plugins:
        print("📦 没有安装的插件")
        return

    print(f"📦 已安装插件 ({len(plugins)} 个):\n")
    for p in plugins:
        print(f"  {p['name']:<20} v{p.get('version','?')}  {p.get('description','')[:50]}")


def add_marketplace(repo: str):
    """添加 marketplace"""
    MARKETPLACES_FILE.parent.mkdir(parents=True, exist_ok=True)

    if MARKETPLACES_FILE.exists():
        marketplaces = json.loads(MARKETPLACES_FILE.read_text())
    else:
        marketplaces = []

    # 去重
    if repo in marketplaces:
        print(f"⚠️  marketplace 已存在: {repo}")
        return

    marketplaces.append(repo)
    MARKETPLACES_FILE.write_text(json.dumps(marketplaces, indent=2))
    print(f"✅ 已添加 marketplace: {repo}")


def list_marketplaces():
    """列出 marketplace"""
    if not MARKETPLACES_FILE.exists():
        print("📋 没有配置 marketplace")
        print("\n💡 添加: hermes plugin marketplace add github:user/repo")
        return

    marketplaces = json.loads(MARKETPLACES_FILE.read_text())
    print(f"📋 Marketplace ({len(marketplaces)} 个):\n")
    for m in marketplaces:
        print(f"  - {m}")


def discover_from_marketplace(name: str) -> str:
    """从 marketplace 发现插件"""
    if not MARKETPLACES_FILE.exists():
        raise RuntimeError("没有配置 marketplace。用 'hermes plugin marketplace add <repo>' 添加")

    marketplaces = json.loads(MARKETPLACES_FILE.read_text())

    for repo in marketplaces:
        # 转换为 raw GitHub URL
        if "github.com" in repo:
            raw_base = repo.replace("https://github.com/", "https://raw.githubusercontent.com/")
            if not raw_base.endswith("/"):
                raw_base += "/"
            manifest_url = f"{raw_base}refs/heads/main/plugins/{name}/manifest.json"

            import urllib.request
            import urllib.error
            try:
                req = urllib.request.Request(manifest_url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    manifest = json.loads(resp.read().decode())
                    if manifest.get("name") == name:
                        return f"github:{repo}/plugins/{name}"
            except Exception:
                continue

    raise RuntimeError(f"在所有 marketplace 中都找不到插件: {name}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Plugin Manager")
    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser("install", help="安装插件")
    install_p.add_argument("source", help="插件路径/github:user/repo/插件名")
    install_p.add_argument("--scope", default="user", choices=["user", "project"])

    uninstall_p = sub.add_parser("uninstall", help="卸载插件")
    uninstall_p.add_argument("name", help="插件名")

    sub.add_parser("list", help="列出已安装插件")

    mkt_p = sub.add_parser("marketplace", help="Marketplace 管理")
    mkt_sub = mkt_p.add_subparsers(dest="mkt_command")
    mkt_add = mkt_sub.add_parser("add", help="添加 marketplace")
    mkt_add.add_argument("repo", help="github:user/repo")
    mkt_sub.add_parser("list", help="列出 marketplace")

    args = parser.parse_args()

    try:
        if args.command == "install":
            # 尝试从 marketplace 解析
            source = args.source
            if not source.startswith("/") and not source.startswith("github:") and not source.startswith("http"):
                try:
                    source = discover_from_marketplace(source)
                    print(f"🔍 从 marketplace 发现: {source}")
                except RuntimeError:
                    pass  # 按原名安装
            install_plugin(source, args.scope)

        elif args.command == "uninstall":
            uninstall_plugin(args.name)

        elif args.command == "list":
            list_plugins()

        elif args.command == "marketplace":
            if args.mkt_command == "add":
                add_marketplace(args.repo)
            elif args.mkt_command == "list":
                list_marketplaces()
            else:
                mkt_p.print_help()

        else:
            parser.print_help()

    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()