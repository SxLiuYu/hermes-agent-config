#!/usr/bin/env python3
"""
Project Context Generator — 对标 Claude Code CLAUDE.md 自动生成

自动分析项目结构，生成 AI 可读的项目上下文文件。
在会话启动时自动加载（SessionStart hook），
让 agent 无需手动探索就能理解项目全貌。

生成内容:
  - 项目类型和语言
  - 目录结构概要
  - 关键配置文件
  - 测试框架
  - 依赖关系
  - 编码规范（从 linter 配置推断）

用法:
  hermes context generate [path]    # 生成项目上下文
  hermes context show [path]        # 查看已有上下文
  hermes context scan               # 扫描所有项目并生成

输出文件: .hermes/project-context/<project-hash>.md
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

HERMES_HOME = Path.home() / ".hermes"
CONTEXT_DIR = HERMES_HOME / "project-context"


def _get_project_hash(path: Path) -> str:
    """生成项目唯一 hash（基于路径）"""
    return hashlib.md5(str(path.resolve()).encode()).hexdigest()[:12]


def _safe_read(path: Path, max_lines: int = 50) -> str:
    """安全读取文件前 N 行"""
    try:
        lines = []
        with open(path) as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line.rstrip())
        return "\n".join(lines)
    except Exception:
        return ""


def _count_files(path: Path, patterns: list) -> int:
    """统计匹配模式的文件数"""
    total = 0
    for p in patterns:
        try:
            total += len(list(path.glob(f"**/{p}")))
        except Exception:
            pass
    return total


def detect_project(path: Path) -> dict:
    """检测项目类型和关键信息"""
    info = {
        "root": str(path.resolve()),
        "name": path.resolve().name,
        "type": "unknown",
        "languages": [],
        "frameworks": [],
        "package_managers": [],
        "test_frameworks": [],
        "linters": [],
        "entry_points": [],
        "config_files": [],
        "file_stats": {},
    }

    # 检测 package.json
    pkg_json = path / "package.json"
    if pkg_json.exists():
        info["type"] = "node"
        info["languages"].append("JavaScript/TypeScript")
        info["package_managers"].append("npm")
        info["config_files"].append("package.json")

        try:
            data = json.loads(pkg_json.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            # 框架检测
            framework_map = {
                "react": "React", "next": "Next.js", "vue": "Vue.js",
                "express": "Express", "fastify": "Fastify",
                "angular": "Angular", "svelte": "Svelte",
            }
            for dep, framework in framework_map.items():
                if dep in deps:
                    info["frameworks"].append(framework)

            # 测试框架
            test_map = {"jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "cypress": "Cypress"}
            for dep, test_fw in test_map.items():
                if dep in deps:
                    info["test_frameworks"].append(test_fw)

            # TypeScript
            if "typescript" in deps:
                info["languages"].append("TypeScript")
        except Exception:
            pass

    # 检测 pyproject.toml
    pyproj = path / "pyproject.toml"
    if pyproj.exists():
        if info["type"] == "unknown":
            info["type"] = "python"
        info["languages"].append("Python")
        info["config_files"].append("pyproject.toml")
        info["package_managers"].append("pip/poetry/uv")
        info["linters"].append("ruff")

        content = pyproj.read_text()
        if "django" in content.lower():
            info["frameworks"].append("Django")
        if "flask" in content.lower():
            info["frameworks"].append("Flask")
        if "fastapi" in content.lower():
            info["frameworks"].append("FastAPI")
        if "pytest" in content.lower():
            info["test_frameworks"].append("pytest")

    # 检测 setup.py
    if (path / "setup.py").exists():
        if info["type"] == "unknown":
            info["type"] = "python"
        info["languages"].append("Python")
        info["config_files"].append("setup.py")

    # 检测 requirements.txt
    if (path / "requirements.txt").exists():
        info["languages"].append("Python") if "Python" not in info["languages"] else None
        info["config_files"].append("requirements.txt")

    # 检测 Docker
    if (path / "Dockerfile").exists():
        info["frameworks"].append("Docker")
    if (path / "docker-compose.yml").exists() or (path / "docker-compose.yaml").exists():
        info["frameworks"].append("Docker Compose")

    # 检测 Git
    if (path / ".git").exists():
        info["config_files"].append(".git")

    # 检测 CI/CD
    if (path / ".github" / "workflows").exists():
        info["frameworks"].append("GitHub Actions")
    if (path / ".gitlab-ci.yml").exists():
        info["frameworks"].append("GitLab CI")

    # 检测 Makefile
    if (path / "Makefile").exists():
        info["config_files"].append("Makefile")

    # Rust
    if (path / "Cargo.toml").exists():
        info["type"] = "rust"
        info["languages"].append("Rust")
        info["config_files"].append("Cargo.toml")

    # Go
    if (path / "go.mod").exists():
        info["type"] = "go"
        info["languages"].append("Go")
        info["config_files"].append("go.mod")

    # 统计文件
    info["file_stats"] = {
        "py": _count_files(path, ["*.py"]),
        "js": _count_files(path, ["*.js"]),
        "ts": _count_files(path, ["*.ts"]),
        "html": _count_files(path, ["*.html"]),
        "css": _count_files(path, ["*.css"]),
        "sh": _count_files(path, ["*.sh"]),
        "json": _count_files(path, ["*.json"]),
        "yaml": _count_files(path, ["*.yaml", "*.yml"]),
        "md": _count_files(path, ["*.md"]),
    }

    # Entry points
    for ep in [path / "app.py", path / "main.py", path / "manage.py",
               path / "index.js", path / "src" / "index.ts",
               path / "main.rs"]:
        if ep.exists():
            info["entry_points"].append(ep.name)

    return info


def _directory_tree(path: Path, max_depth: int = 3, prefix: str = "") -> str:
    """生成目录树（浅层，跳过常见的非代码目录）"""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".hermes", ".idea", ".vscode", "dist", "build", ".next",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache", "target"}

    lines = []
    try:
        items = sorted(path.iterdir())
        dirs = [d for d in items if d.is_dir() and d.name not in skip_dirs
                and not d.name.startswith(".")]
        files = [f for f in items if f.is_file()]

        for i, d in enumerate(dirs[:8]):
            connector = "└── " if i == len(dirs[:8]) - 1 and not files else "├── "
            lines.append(f"{prefix}{connector}{d.name}/")
            if max_depth > 1:
                sub = _directory_tree(d, max_depth - 1, prefix + ("    " if connector == "└── " else "│   "))
                if sub:
                    lines.append(sub)

        # 只显示重要文件
        important = [".md", ".py", ".json", ".yaml", ".yml", ".toml", ".env.example", ".gitignore"]
        shown = 0
        for f in files:
            if any(f.name.endswith(ext) or f.name == ext for ext in important):
                if shown < 10:
                    lines.append(f"{prefix}├── {f.name}" if shown < len([x for x in files if any(x.name.endswith(e) or x.name == e for e in important)]) - 1 else f"{prefix}└── {f.name}")
                    shown += 1

        # 省略提示
        remaining_files = len([f for f in files if f.name not in {".md", ".py", ".json"}])
        if remaining_files > 10:
            lines.append(f"{prefix}    ... (+{remaining_files} more files)")

    except PermissionError:
        lines.append(f"{prefix}[权限不足]")

    return "\n".join(lines)


def _extract_conventions(path: Path) -> dict:
    """从配置文件推测编码规范"""
    conventions = {"indent": "?", "quotes": "?", "max_line": "?", "type_checking": "?"}

    # Ruff
    ruff_config = path / "ruff.toml"
    if not ruff_config.exists():
        ruff_config = path / "pyproject.toml"

    if ruff_config.exists():
        content = ruff_config.read_text()
        if "line-length" in content:
            m = re.search(r"line-length\s*=\s*(\d+)", content)
            if m:
                conventions["max_line"] = m.group(1)
        if "indent-width" in content:
            m = re.search(r"indent-width\s*=\s*(\d+)", content)
            if m:
                conventions["indent"] = f"{m.group(1)} spaces"
        if "quote-style" in content:
            m = re.search(r"quote-style\s*=\s*['\"](\w+)['\"]", content)
            if m:
                conventions["quotes"] = m.group(1)

    # ESLint
    eslint = path / ".eslintrc.json"
    if not eslint.exists():
        eslint = path / ".eslintrc.js"
    if eslint.exists():
        conventions["quotes"] = "single" if "single" in _safe_read(eslint, 30) else "?"

    # EditorConfig
    ec = path / ".editorconfig"
    if ec.exists():
        content = _safe_read(ec, 30)
        m = re.search(r"indent_size\s*=\s*(\d+)", content)
        if m:
            conventions["indent"] = f"{m.group(1)} spaces"

    return conventions


def generate_context(path: Path, force: bool = False) -> str:
    """生成项目上下文文件"""
    path = path.resolve()

    project_hash = _get_project_hash(path)
    context_file = CONTEXT_DIR / f"{project_hash}.md"

    if context_file.exists() and not force:
        return str(context_file)

    print(f"🔍 分析项目: {path.name}")

    info = detect_project(path)
    conventions = _extract_conventions(path)
    tree = _directory_tree(path)

    # 获取 git 信息
    git_info = ""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=str(path), timeout=5,
        )
        if branch.returncode == 0:
            git_info = branch.stdout.strip()

        last_commit = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            capture_output=True, text=True, cwd=str(path), timeout=5,
        )
        if last_commit.returncode == 0:
            git_info += f" ({last_commit.stdout.strip()[:60]})"
    except Exception:
        pass

    # 生成 markdown
    lines = [
        f"# Project Context: {info['name']}",
        f"",
        f"> Auto-generated by Hermes Agent on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"> Git: {git_info or 'N/A'}",
        f"",
        f"## Overview",
        f"",
        f"- **Type:** {info['type']}",
        f"- **Languages:** {', '.join(info['languages']) if info['languages'] else 'unknown'}",
        f"- **Frameworks:** {', '.join(info['frameworks']) if info['frameworks'] else 'none detected'}",
        f"- **Package Managers:** {', '.join(info['package_managers']) if info['package_managers'] else 'unknown'}",
        f"- **Test Frameworks:** {', '.join(info['test_frameworks']) if info['test_frameworks'] else 'unknown'}",
        f"- **Linters:** {', '.join(info['linters']) if info['linters'] else 'unknown'}",
        f"",
        f"## File Statistics",
        f"",
    ]

    for ext, count in sorted(info["file_stats"].items()):
        if count > 0:
            lines.append(f"- `*.{ext}`: {count} files")

    lines += [
        f"",
        f"## Entry Points",
        f"",
    ]

    if info["entry_points"]:
        for ep in info["entry_points"]:
            lines.append(f"- `{ep}`")
    else:
        lines.append("- (none auto-detected)")

    lines += [
        f"",
        f"## Directory Structure",
        f"",
        f"```",
        f"{info['name']}/",
        tree,
        f"```",
        f"",
        f"## Conventions",
        f"",
        f"- Indent: {conventions['indent']}",
        f"- Quotes: {conventions['quotes']}",
        f"- Max line: {conventions['max_line']}",
        f"- Type checking: {conventions['type_checking']}",
        f"",
        f"## Config Files",
        f"",
    ]

    for cf in info["config_files"]:
        lines.append(f"- `{cf}`")

    lines += [
        f"",
        f"---",
        f"*This file helps AI agents understand your project. Auto-update with `hermes context generate`.*",
    ]

    content = "\n".join(lines)

    # 写入文件
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context_file.write_text(content)

    print(f"✅ 上下文已生成: {context_file}")

    return str(context_file)


def scan_all_projects(search_paths: list = None) -> list:
    """扫描所有项目并生成上下文"""
    if search_paths is None:
        # 默认识别策略：查找包含 setup.py / package.json / Cargo.toml 的目录
        home = Path.home()
        candidates = [
            home / "projects",
            home / "Documents",
            home / "dev",
            home / "code",
            home / "workspace",
            Path.cwd(),
        ]
        existing = [p for p in candidates if p.exists()]
    else:
        existing = [Path(p).expanduser() for p in search_paths]

    results = []
    indicators = ["package.json", "pyproject.toml", "setup.py", "Cargo.toml",
                  "go.mod", "Makefile", "docker-compose.yml"]

    seen = set()
    for base in existing:
        if not base.exists():
            continue
        for root, dirs, _ in os.walk(str(base)):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                       ("node_modules", "venv", "__pycache__", ".git")]

            root_path = Path(root)
            for indicator in indicators:
                if (root_path / indicator).exists():
                    h = _get_project_hash(root_path)
                    if h not in seen:
                        seen.add(h)
                        try:
                            result = generate_context(root_path)
                            results.append(result)
                        except Exception as e:
                            print(f"⚠️  跳过 {root_path}: {e}")
                    break

            # 限制深度
            if len(root.split(os.sep)) - len(str(base).split(os.sep)) > 3:
                dirs[:] = []

    return results


def show_context(path: Path = None):
    """显示已有的项目上下文"""
    if path:
        h = _get_project_hash(path.resolve())
        cf = CONTEXT_DIR / f"{h}.md"
        if cf.exists():
            print(cf.read_text())
        else:
            print(f"❌ 未找到 {path} 的上下文文件")
            print(f"💡 生成: hermes context generate {path}")
    else:
        if not CONTEXT_DIR.exists():
            print("暂无项目上下文")
            return

        contexts = sorted(CONTEXT_DIR.glob("*.md"))
        print(f"📋 已有 {len(contexts)} 个项目上下文:\n")
        for cf in contexts:
            first_line = _safe_read(cf, 1).replace("# Project Context: ", "")
            print(f"  - {first_line:<30} ({cf.name})")


def inject_context(path: Path = None) -> str:
    """获取项目上下文，用于注入到 session（SessionStart hook 用）"""
    if path is None:
        path = Path.cwd()

    h = _get_project_hash(path.resolve())
    cf = CONTEXT_DIR / f"{h}.md"

    if not cf.exists():
        return ""

    content = cf.read_text()
    # 提取核心部分（去掉元数据和页脚）
    # 简单裁切：保留前面部分
    lines = content.split("\n")
    cutoff = 0
    for i, line in enumerate(lines):
        if line.startswith("---") and i > len(lines) - 5:
            cutoff = i
            break

    if cutoff:
        content = "\n".join(lines[:cutoff]).strip()

    return content


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Project Context Generator")
    sub = parser.add_subparsers(dest="command")

    gen_p = sub.add_parser("generate", help="生成项目上下文")
    gen_p.add_argument("path", nargs="?", default=".", help="项目路径")
    gen_p.add_argument("--force", action="store_true", help="强制重新生成")

    show_p = sub.add_parser("show", help="显示上下文")
    show_p.add_argument("path", nargs="?", help="项目路径")

    sub.add_parser("scan", help="扫描所有项目")

    args = parser.parse_args()

    if args.command == "generate":
        generate_context(Path(args.path), force=args.force)
    elif args.command == "show":
        show_context(Path(args.path) if args.path else None)
    elif args.command == "scan":
        results = scan_all_projects()
        print(f"\n✅ 生成了 {len(results)} 个项目上下文")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()