#!/usr/bin/env python3
"""
Memory Bank — 结构化项目知识库

对标 Kilo Code Memory Bank:
  - 存储架构决策、设计模式、项目约定
  - 跨 session 自动加载上下文
  - 语义化知识图谱

目录结构:
  ~/.hermes/memory-bank/
    index.md            # 索引：所有项目+最近决策
    <project>/
      architecture.md   # 架构设计决策 (ADR)
      conventions.md    # 编码约定
      patterns.md       # 设计模式
      dependencies.md   # 依赖关系图
      decisions/        # 单个 ADR 文件

用法:
  python3 scripts/memory_bank.py add-decision "使用 Redis 做缓存层"
  python3 scripts/memory_bank.py add-convention "所有 API 返回 {'code': 0, 'data': ...}"
  python3 scripts/memory_bank.py list --project yuanfang
  python3 scripts/memory_bank.py context     # 生成上下文注入
  python3 scripts/memory_bank.py summary     # 快速摘要
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
BANK_DIR = HERMES_HOME / "memory-bank"


class MemoryBank:
    """结构化项目知识库管理器"""

    def __init__(self, project: str = "default"):
        self.project = project
        self.project_dir = BANK_DIR / project
        self._ensure_dirs()

    def _ensure_dirs(self):
        BANK_DIR.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "decisions").mkdir(exist_ok=True)

    def _ensure_file(self, name: str, header: str = ""):
        fp = self.project_dir / name
        if not fp.exists():
            fp.write_text(header)

    def add_decision(self, title: str, context: str = "", rationale: str = "",
                     consequences: str = "", status: str = "proposed"):
        """添加架构决策记录 (ADR)"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        idx = len(list((self.project_dir / "decisions").glob("*.md"))) + 1
        filename = f"{idx:04d}-{title.replace(' ', '-').lower()[:50]}.md"

        adr = f"""# ADR {idx:04d}: {title}

- **状态**: {status}
- **日期**: {ts}
- **项目**: {self.project}

## 背景
{context or '待补充'}

## 决策
{title}

## 理由
{rationale or '待补充'}

## 影响
{consequences or '待补充'}

---
*{status} @ {ts}*
"""
        (self.project_dir / "decisions" / filename).write_text(adr)

        # 更新索引
        self._update_index("decision", title, status, ts)
        print(f"📋 ADR 已记录: {filename}")

    def add_convention(self, text: str, category: str = "general"):
        """添加编码约定"""
        self._ensure_file("conventions.md", "# 编码约定\n\n")

        fp = self.project_dir / "conventions.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        entry = f"\n## {ts} — {category}\n{text}\n"
        with open(fp, "a") as f:
            f.write(entry)

        self._update_index("convention", text[:60], category, ts)
        print(f"📝 约定已记录: {category}")

    def add_pattern(self, name: str, description: str, code_example: str = ""):
        """添加设计模式"""
        self._ensure_file("patterns.md", "# 设计模式\n\n")

        fp = self.project_dir / "patterns.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        entry = f"""
## {name}
> {ts}

{description}

{chr(10) + '```python' + chr(10) + code_example + chr(10) + '```' if code_example else ''}
"""
        with open(fp, "a") as f:
            f.write(entry)

        self._update_index("pattern", name, "design-pattern", ts)
        print(f"🎨 模式已记录: {name}")

    def add_architecture(self, title: str, content: str):
        """添加架构说明"""
        self._ensure_file("architecture.md", "# 架构设计\n\n")

        fp = self.project_dir / "architecture.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        entry = f"\n## {title}\n> {ts}\n\n{content}\n"
        with open(fp, "a") as f:
            f.write(entry)

        self._update_index("architecture", title, "architecture", ts)
        print(f"🏗️ 架构已记录: {title}")

    def _update_index(self, entry_type: str, title: str, category: str, ts: str):
        """更新总索引"""
        index_file = BANK_DIR / "index.md"
        if not index_file.exists():
            index_file.write_text("# Memory Bank 索引\n\n## 最近条目\n\n")

        entry = f"- [{entry_type}] **{title}** ({category}) — {ts} — `{self.project}`\n"

        # 插入到 "最近条目" 之后
        content = index_file.read_text()
        lines = content.split("\n")
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if "## 最近条目" in line and not inserted:
                new_lines.append(entry)
                inserted = True

        index_file.write_text("\n".join(new_lines))

    def list_items(self, item_type: str = "all", limit: int = 20):
        """列出知识库条目"""
        if item_type in ("all", "decision", "decisions"):
            dec_dir = self.project_dir / "decisions"
            decs = sorted(dec_dir.glob("*.md"))
            if decs:
                print(f"\n📋 架构决策 ({len(decs)}):")
                for d in decs[-limit:]:
                    first_line = d.read_text().split("\n")[0].replace("# ", "")
                    print(f"  {d.name}: {first_line}")

        for fname, label in [("conventions.md", "编码约定"),
                             ("patterns.md", "设计模式"),
                             ("architecture.md", "架构设计")]:
            if item_type in ("all", fname.replace(".md", "")):
                fp = self.project_dir / fname
                if fp.exists() and fp.stat().st_size > 50:
                    sections = [l for l in fp.read_text().split("\n")
                               if l.startswith("## ")]
                    if sections:
                        print(f"\n{label} ({len(sections)} 条):")
                        for s in sections[-10:]:
                            print(f"  {s}")

    def generate_context(self, max_tokens: int = 2000) -> str:
        """生成上下文，注入 agent 系统提示"""
        parts = []

        # 1. 最近决策
        dec_dir = self.project_dir / "decisions"
        recent_decs = sorted(dec_dir.glob("*.md"), reverse=True)[:3]
        if recent_decs:
            parts.append("## 最近架构决策")
            for d in recent_decs:
                content = d.read_text()
                # 提取关键信息
                for line in content.split("\n"):
                    if line.startswith("# ADR"):
                        parts.append(f"- {line.replace('# ', '')}")
                        break
                for line in content.split("\n"):
                    if line.startswith("- **状态**:"):
                        parts.append(f"  {line}")
                        break

        # 2. 核心约定 (只取最近的 5 条)
        conv_file = self.project_dir / "conventions.md"
        if conv_file.exists():
            parts.append("\n## 核心约定")
            entries = []
            for line in conv_file.read_text().split("\n"):
                if line.startswith("## ") and "—" in line:
                    entries.append(line)
            for e in entries[-5:]:
                parts.append(f"- {e}")

        # 3. 架构摘要
        arch_file = self.project_dir / "architecture.md"
        if arch_file.exists():
            content = arch_file.read_text()
            parts.append("\n## 架构摘要")
            sections = []
            for line in content.split("\n"):
                if line.startswith("## ") and "—" not in line:
                    sections.append(f"- {line}")
            parts.extend(sections[-5:])

        ctx = "\n".join(parts) if parts else "(空知识库)"
        # 粗略 token 估算 (中文约 1.5 char/token, 英文约 4 char/token)
        if len(ctx) > max_tokens * 2:
            ctx = ctx[:max_tokens * 2] + "\n...(截断)"

        return ctx

    def summary(self) -> str:
        """快速摘要"""
        total = 0
        desc = []

        dec_dir = self.project_dir / "decisions"
        decs = list(dec_dir.glob("*.md"))
        total += len(decs)
        if decs:
            recent = sorted(decs)[-1]
            desc.append(f"{len(decs)} 个架构决策 (最新: {recent.stem})")

        for fname, label in [("conventions.md", "条约定"),
                             ("patterns.md", "个模式"),
                             ("architecture.md", "条架构")]:
            fp = self.project_dir / fname
            if fp.exists():
                count = len([l for l in fp.read_text().split("\n")
                           if l.startswith("## ") and "—" in l])
                total += count
                if count:
                    desc.append(f"{count}{label}")

        if total == 0:
            return "📭 知识库为空"

        return f"📚 Memory Bank ({total} 条)\n" + "\n".join(f"  • {d}" for d in desc)


def main():
    parser = argparse.ArgumentParser(
        description="Memory Bank — 结构化项目知识库"
    )
    parser.add_argument("--project", "-p", default="default",
                       help="项目名称 (default, yuanfang, jarvis, hermes)")

    sub = parser.add_subparsers(dest="command")

    # add-decision
    ad_p = sub.add_parser("add-decision", help="添加架构决策")
    ad_p.add_argument("title", help="决策标题")
    ad_p.add_argument("--context", default="", help="背景")
    ad_p.add_argument("--rationale", default="", help="理由")
    ad_p.add_argument("--consequences", default="", help="影响")
    ad_p.add_argument("--status", default="proposed",
                     choices=["proposed", "accepted", "deprecated", "superseded"])

    # add-convention
    ac_p = sub.add_parser("add-convention", help="添加编码约定")
    ac_p.add_argument("text", help="约定内容")
    ac_p.add_argument("--category", default="general", help="分类")

    # add-pattern
    ap_p = sub.add_parser("add-pattern", help="添加设计模式")
    ap_p.add_argument("name", help="模式名称")
    ap_p.add_argument("--description", default="", help="描述")
    ap_p.add_argument("--code", default="", help="代码示例")

    # add-architecture
    aa_p = sub.add_parser("add-architecture", help="添加架构说明")
    aa_p.add_argument("title", help="标题")
    aa_p.add_argument("--content", default="", help="内容")

    # list
    list_p = sub.add_parser("list", help="列出知识库")
    list_p.add_argument("--type", default="all",
                       choices=["all", "decisions", "conventions", "patterns", "architecture"])

    # context / summary
    sub.add_parser("context", help="生成上下文注入")
    sub.add_parser("summary", help="快速摘要")

    # index
    sub.add_parser("index", help="查看全局索引")

    args = parser.parse_args()
    bank = MemoryBank(args.project)

    if args.command == "add-decision":
        bank.add_decision(args.title, args.context, args.rationale,
                         args.consequences, args.status)
    elif args.command == "add-convention":
        bank.add_convention(args.text, args.category)
    elif args.command == "add-pattern":
        bank.add_pattern(args.name, args.description or "", args.code or "")
    elif args.command == "add-architecture":
        bank.add_architecture(args.title, args.content or "")
    elif args.command == "list":
        bank.list_items(args.type)
    elif args.command == "context":
        print(bank.generate_context())
    elif args.command == "summary":
        print(bank.summary())
    elif args.command == "index":
        idx = BANK_DIR / "index.md"
        if idx.exists():
            print(idx.read_text())
        else:
            print("📭 索引为空")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()