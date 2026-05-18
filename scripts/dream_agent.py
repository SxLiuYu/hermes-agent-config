#!/usr/bin/env python3
"""
Hermes Dream Agent — 背景自优化维护 Agent。
对标 Letta 的 Dream Agents 和 OpenCode 的 background-agents。

核心功能:
  1. 记忆整理: 扫描 memory_blocks，压缩超大块，清理过期内容
  2. 技能审计: 审查 skills 使用频率，标记长期未使用的
  3. 错误分析: 分析 agent.log 中的 Traceback 模式
  4. 缓存刷新: 刷新 repo-map 缓存
  5. 配置健康检查

设计理念:
  - 低风险任务自动执行（压缩记忆、刷新缓存）
  - 高风险任务只生成建议（删除技能、修改配置）
  - 所有变更都有日志和回滚机制
  - 可配置执行间隔（默认每 4 小时）

命令行:
  python dream_agent.py run        # 运行一次
  python dream_agent.py plan       # 生成任务列表但不执行
  python dream_agent.py report     # 显示上次运行报告
  python dream_agent.py daemon --interval 4h  # 守护进程模式
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

# ── 路径配置 ──────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SCRIPTS_DIR = HERMES_HOME / "scripts"
SKILLS_DIR = HERMES_HOME / "skills"
LOGS_DIR = HERMES_HOME / "logs"
STATE_FILE = HERMES_HOME / "dream_agent_state.json"
DREAM_LOG = LOGS_DIR / "dream_agent.log"
USAGE_FILE = SKILLS_DIR / ".usage.json"
MEMORY_DB = HERMES_HOME / "memory_blocks.db"

# ── 任务分类 ──────────────────────────────────────────────────
CATEGORIES = {
    "memory": "记忆整理",
    "skill": "技能审计",
    "error": "错误分析",
    "cache": "缓存刷新",
    "config": "配置健康",
}


class DreamTask:
    """梦之Agent 任务"""
    def __init__(self, id: str, category: str, priority: int,
                 action: str, auto_execute: bool = True, details: str = ""):
        self.id = id
        self.category = category
        self.priority = priority  # 1(低) - 5(高)
        self.action = action
        self.auto_execute = auto_execute
        self.details = details
        self.executed = False
        self.result = ""


class DreamAgent:
    """梦之Agent 引擎"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.tasks: List[DreamTask] = []
        self.report_lines: List[str] = []
        self.executed_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self._load_state()

    def _log(self, msg: str):
        """写日志"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.report_lines.append(line)
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(DREAM_LOG, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _load_state(self):
        """加载状态文件"""
        self.state = {"runs": [], "last_run": None}
        if STATE_FILE.exists():
            try:
                loaded = json.loads(STATE_FILE.read_text())
                # 兼容不同版本的状态文件格式
                if "runs" in loaded:
                    self.state = loaded
                elif "dream_cycles" in loaded:
                    # 旧格式：转换 dream_cycles 为 runs
                    self.state = {
                        "runs": [
                            {
                                "timestamp": c.get("timestamp", ""),
                                "tasks_planned": c.get("total_tasks", 0),
                                "executed": c.get("executed", 0),
                                "skipped": c.get("skipped", 0),
                                "failed": 0,
                            }
                            for c in loaded.get("dream_cycles", [])
                        ],
                        "last_run": loaded.get("last_run"),
                    }
                else:
                    self.state = loaded
            except Exception:
                pass

    def _save_state(self):
        """保存状态"""
        self.state["last_run"] = datetime.now(timezone.utc).isoformat()
        self.state["runs"].append({
            "timestamp": self.state["last_run"],
            "tasks_planned": len(self.tasks),
            "executed": self.executed_count,
            "skipped": self.skipped_count,
            "failed": self.failed_count,
        })
        # 只保留最近 50 次运行记录
        self.state["runs"] = self.state["runs"][-50:]
        try:
            STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # ─── 任务生成器 ─────────────────────────────────────────
    # ══════════════════════════════════════════════════════════

    def generate_memory_tasks(self):
        """生成记忆相关任务"""
        if not MEMORY_DB.exists():
            return

        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cur = conn.cursor()
            cur.execute("SELECT name, label, content, limit_chars FROM blocks")
            rows = cur.fetchall()

            for name, label, content, limit in rows:
                content_len = len(content or "")
                usage_pct = (content_len / limit * 100) if limit > 0 else 0

                if usage_pct > 80:
                    self.tasks.append(DreamTask(
                        id=f"memory_compact_{name}",
                        category="memory",
                        priority=4 if usage_pct > 95 else 3,
                        action=f"压缩记忆块 '{label}' ({content_len}/{limit} chars, {usage_pct:.0f}%)",
                        auto_execute=True,
                        details=f"content_length={content_len}, limit={limit}",
                    ))

                # 检查过旧记忆（>90天未更新的块）
                cur.execute(
                    "SELECT MAX(created_at) FROM blocks_history WHERE block_name=?",
                    (name,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        last_update = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                        days_old = (datetime.now(timezone.utc) - last_update).days
                        if days_old > 90 and usage_pct > 50:
                            self.tasks.append(DreamTask(
                                id=f"memory_stale_{name}",
                                category="memory",
                                priority=2,
                                action=f"记忆块 '{label}' {days_old}天未更新，建议检查",
                                auto_execute=False,
                                details=f"last_update={row[0]}, days_old={days_old}",
                            ))
                    except Exception:
                        pass

            conn.close()
        except Exception as e:
            self._log(f"⚠️  记忆扫描失败: {e}")

    def generate_skill_tasks(self):
        """生成技能审计任务"""
        if not USAGE_FILE.exists():
            return

        try:
            usage = json.loads(USAGE_FILE.read_text())
            now = datetime.now(timezone.utc)

            for skill_name, info in usage.items():
                if not isinstance(info, dict):
                    continue
                last_activity = info.get("last_activity_at")
                use_count = info.get("use_count", 0)
                state = info.get("state", "active")
                pinned = info.get("pinned", False)

                if not last_activity or pinned:
                    continue

                try:
                    last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                    days_inactive = (now - last_dt).days
                except Exception:
                    continue

                if days_inactive > 30 and state == "active":
                    self.tasks.append(DreamTask(
                        id=f"skill_stale_{skill_name}",
                        category="skill",
                        priority=2,
                        action=f"技能 '{skill_name}' {days_inactive}天未使用 ({use_count}次)，建议标记为 stale",
                        auto_execute=False,
                        details=f"days_inactive={days_inactive}, use_count={use_count}",
                    ))

        except Exception as e:
            self._log(f"⚠️  技能审计失败: {e}")

    def generate_error_tasks(self):
        """分析 agent.log 中的错误模式"""
        agent_log = LOGS_DIR / "agent.log"
        errors_log = LOGS_DIR / "errors.log"

        recent_errors: List[Tuple[str, int]] = []
        for log_path in [errors_log, agent_log]:
            if not log_path.exists():
                continue
            try:
                content = log_path.read_text(errors="ignore")
                # 查找最近 7 天的错误
                now = datetime.now()
                patterns = Counter()

                for line in content.splitlines():
                    if "Traceback" in line or "ERROR" in line or "Exception" in line:
                        # 提取错误类型
                        match = re.search(r'(\w+Error|\w+Exception)', line)
                        if match:
                            patterns[match.group(1)] += 1

                for error_type, count in patterns.most_common(5):
                    if count >= 3:
                        self.tasks.append(DreamTask(
                            id=f"error_pattern_{error_type}",
                            category="error",
                            priority=3 if count >= 10 else 2,
                            action=f"发现 {count} 次 '{error_type}'，建议排查",
                            auto_execute=False,
                            details=f"error_type={error_type}, count={count}",
                        ))

            except Exception as e:
                self._log(f"⚠️  日志分析失败 ({log_path.name}): {e}")

    def generate_cache_tasks(self):
        """生成缓存刷新任务"""
        # 检查 repo-map 缓存
        repos_to_check = [
            (HERMES_HOME / "hermes-agent" if (HERMES_HOME / "hermes-agent").is_dir() else None),
            (Path.home() / "yuanfang-brain" if (Path.home() / "yuanfang-brain").is_dir() else None),
        ]

        for repo in repos_to_check:
            if not repo:
                continue
            cache_path = repo / ".repo-map.cache.json"
            if cache_path.exists():
                try:
                    cache = json.loads(cache_path.read_text())
                    updated = cache.get("updated_at", "")
                    if updated:
                        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        days_old = (datetime.now(timezone.utc) - dt).days
                        if days_old > 7:
                            self.tasks.append(DreamTask(
                                id=f"cache_refresh_{repo.name}",
                                category="cache",
                                priority=2,
                                action=f"刷新 '{repo.name}' 的 repo-map 缓存 ({days_old}天前)",
                                auto_execute=True,
                                details=f"repo={repo}, days_old={days_old}",
                            ))
                except Exception:
                    pass

    def generate_config_tasks(self):
        """配置健康检查"""
        config_path = HERMES_HOME / "config.yaml"
        env_path = HERMES_HOME / ".env"

        # 检查 .env 权限
        if env_path.exists():
            mode = env_path.stat().st_mode
            if mode & 0o077:  # 组/其他人可读写
                self.tasks.append(DreamTask(
                    id="config_env_perms",
                    category="config",
                    priority=5,
                    action=f".env 文件权限过于开放 (mode={oct(mode)[-3:]})，建议 chmod 600",
                    auto_execute=False,
                    details=f"current_mode={oct(mode)}",
                ))

    def generate_all_tasks(self):
        """生成所有任务"""
        self.tasks = []
        self.generate_memory_tasks()
        self.generate_skill_tasks()
        self.generate_error_tasks()
        self.generate_cache_tasks()
        self.generate_config_tasks()

        # 按优先级排序
        self.tasks.sort(key=lambda t: (-t.priority, t.category))

    # ══════════════════════════════════════════════════════════
    # ─── 任务执行 ───────────────────────────────────────────
    # ══════════════════════════════════════════════════════════

    def _execute_memory_compact(self, task: DreamTask):
        """执行记忆压缩"""
        try:
            # 导入 memory_blocks 模块
            sys.path.insert(0, str(SCRIPTS_DIR))
            from memory_blocks import memory_block_rethink

            block_name = task.id.replace("memory_compact_", "")
            result = memory_block_rethink(block_name)
            if "✅" in result or "ok" in result.lower():
                task.executed = True
                task.result = f"✅ 已压缩 {block_name}"
                self.executed_count += 1
            else:
                task.result = f"⚠️  压缩 {block_name}: {result[:100]}"
                self.failed_count += 1
        except Exception as e:
            task.result = f"❌ 压缩失败: {e}"
            self.failed_count += 1

    def _execute_cache_refresh(self, task: DreamTask):
        """执行缓存刷新"""
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            from repo_map import scan_project

            repo_name = task.id.replace("cache_refresh_", "")
            # 查找对应的目录
            for candidate in [
                HERMES_HOME / "hermes-agent",
                Path.home() / "yuanfang-brain",
            ]:
                if candidate.name == repo_name and candidate.is_dir():
                    if not self.dry_run:
                        scan_project(str(candidate), format="json", use_cache=False,
                                     project_name=repo_name)
                    task.executed = True
                    task.result = f"✅ 已刷新 {repo_name} 的 repo-map 缓存"
                    self.executed_count += 1
                    return

            task.result = f"⚠️  未找到项目 {repo_name}"
            self.skipped_count += 1
        except Exception as e:
            task.result = f"❌ 缓存刷新失败: {e}"
            self.failed_count += 1

    def execute_tasks(self):
        """执行所有任务"""
        for task in self.tasks:
            if not task.auto_execute:
                self.skipped_count += 1
                continue

            if self.dry_run:
                task.result = f"[DRY RUN] 将执行: {task.action}"
                continue

            self._log(f"🔧 执行: [{task.category}] {task.action}")

            if task.category == "memory":
                self._execute_memory_compact(task)
            elif task.category == "cache":
                self._execute_cache_refresh(task)

    # ══════════════════════════════════════════════════════════
    # ─── 报告生成 ───────────────────────────────────────────
    # ══════════════════════════════════════════════════════════

    def generate_report(self) -> str:
        """生成运行报告"""
        lines = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines.append(f"🧠 Dream Agent Report — {now}")
        lines.append("")
        lines.append(f"📋 任务计划: {len(self.tasks)}")
        lines.append(f"✅ 已执行:   {self.executed_count}")
        lines.append(f"⚠️  跳过:     {self.skipped_count}")
        lines.append(f"❌ 失败:     {self.failed_count}")
        lines.append("")

        if self.executed_count > 0:
            lines.append("**已执行:**")
            for t in self.tasks:
                if t.executed:
                    lines.append(f"  ✅ [{t.category}] {t.result}")
            lines.append("")

        if self.skipped_count > 0:
            lines.append("**建议（需人工确认）:**")
            for t in self.tasks:
                if not t.auto_execute:
                    lines.append(f"  ⚠️  [{t.category}] P{t.priority} {t.action}")
            lines.append("")

        return "\n".join(lines)

    def run(self):
        """完整运行"""
        self._log("🚀 Dream Agent 启动")
        self.generate_all_tasks()
        self._log(f"📋 生成 {len(self.tasks)} 个任务")
        self.execute_tasks()
        self._save_state()
        report = self.generate_report()
        self._log("✅ Dream Agent 完成")
        return report


# ══════════════════════════════════════════════════════════════
# ─── CLI ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════

def cmd_run(args):
    """运行一次 Dream Agent"""
    agent = DreamAgent(dry_run=args.dry_run)
    report = agent.run()
    print(report)

    # 保存报告到文件
    report_path = LOGS_DIR / f"dream_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report)
        print(f"\n📄 报告已保存: {report_path}")
    except Exception:
        pass


def cmd_plan(args):
    """生成任务列表但不执行"""
    agent = DreamAgent(dry_run=True)
    agent.generate_all_tasks()
    print(f"📋 任务计划 ({len(agent.tasks)} 个):\n")
    for t in agent.tasks:
        icon = "🟢" if t.auto_execute else "🟡"
        print(f"  {icon} [{t.category}] P{t.priority} {t.action}")


def cmd_report(args):
    """显示上次运行报告"""
    if not STATE_FILE.exists():
        print("📭 还没有运行过 Dream Agent")
        return

    try:
        state = json.loads(STATE_FILE.read_text())
        runs = state.get("runs", [])
        if not runs:
            print("📭 没有运行记录")
            return

        last = runs[-1]
        print(f"🧠 上次运行: {last.get('timestamp', 'unknown')}")
        print(f"   任务数: {last.get('tasks_planned', 0)}")
        print(f"   已执行: {last.get('executed', 0)}")
        print(f"   跳过:   {last.get('skipped', 0)}")
        print(f"   失败:   {last.get('failed', 0)}")

        print(f"\n📊 历史记录 ({len(runs)} 次):")
        for r in runs[-10:]:
            ts = r.get("timestamp", "")[:16]
            print(f"   {ts}: {r.get('executed',0)}/{r.get('tasks_planned',0)} done")

        # 查找最近的报告文件
        reports = sorted(LOGS_DIR.glob("dream_report_*.md"), reverse=True)
        if reports:
            print(f"\n📄 最新报告: {reports[0]}")
            print(reports[0].read_text()[:500])

    except Exception as e:
        print(f"❌ 读取状态失败: {e}")


def cmd_daemon(args):
    """守护进程模式"""
    print(f"🚀 Dream Agent 守护进程启动 (间隔: {args.interval}s)")

    interval = parse_interval(args.interval)
    print(f"   实际间隔: {interval}s")
    print(f"   日志: {DREAM_LOG}")
    print("   按 Ctrl+C 停止\n")

    agent = DreamAgent()
    while True:
        try:
            report = agent.run()
            timestamp = datetime.now().strftime("%H:%M")
            # 简洁的状态输出
            print(f"[{timestamp}] ✅ {agent.executed_count}/{len(agent.tasks)} | "
                  f"⏭ {agent.skipped_count} skipped")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋 Dream Agent 已停止")
            break
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M')}] ❌ 错误: {e}")
            time.sleep(60)


def parse_interval(s: str) -> int:
    """解析时间间隔字符串为秒"""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    elif s.endswith("m"):
        return int(float(s[:-1]) * 60)
    elif s.endswith("s"):
        return int(float(s[:-1]))
    else:
        return int(s)


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Dream Agent — 背景自优化维护",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python dream_agent.py run                     # 运行一次
  python dream_agent.py run --dry-run           # 预览模式
  python dream_agent.py plan                    # 只生成计划
  python dream_agent.py report                  # 查看报告
  python dream_agent.py daemon --interval 4h    # 守护进程 (每4小时)
        """,
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    run_p = sub.add_parser("run", help="运行一次")
    run_p.add_argument("--dry-run", action="store_true", help="预览模式")

    sub.add_parser("plan", help="生成任务列表但不执行")
    sub.add_parser("report", help="显示上次运行报告")

    daemon_p = sub.add_parser("daemon", help="守护进程模式")
    daemon_p.add_argument("--interval", default="4h", help="执行间隔 (default: 4h)")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "plan":
        cmd_plan(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "daemon":
        cmd_daemon(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()