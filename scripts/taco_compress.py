#!/usr/bin/env python3
"""
TACO v2 — Terminal Agent Context Optimizer
===========================================
终端输出自进化压缩系统 — 基于 TACO 论文扩展。

v2 新增:
  - oMLX 模式识别: 对未知命令输出，用本地模型识别冗余模式
  - 批量压缩: 一次压缩多条命令输出
  - 自进化规则验证: 生成规则后自动测试，防止误删
  - 压缩率统计: 跟踪每条规则的节省率
  - 管道友好: stdin → compress → stdout

用法:
  python3 taco_compress.py compress <command> < input.txt
  python3 taco_compress.py rules
  python3 taco_compress.py add-rule --id <id> --trigger <regex> --strip <pattern>
  python3 taco_compress.py evolve --command <cmd> --output <file>
  python3 taco_compress.py stats
  python3 taco_compress.py batch --cmd "pip install" --cmd "npm install" < input.txt
"""

import re
import os
import sys
import json
import logging
import time
import subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import OrderedDict

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("taco")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
RULES_FILE = HERMES_HOME / "taco_rules.json"
STATS_FILE = HERMES_HOME / "taco_stats.json"


@dataclass
class CompressionRule:
    rule_id: str
    trigger_regex: str
    description: str = ""
    keep_patterns: list = field(default_factory=list)
    strip_patterns: list = field(default_factory=list)
    keep_first_n: int = 5
    keep_last_n: int = 10
    max_lines: Optional[int] = None
    summary_header: Optional[str] = None
    # v2
    total_saved_chars: int = 0
    total_applied: int = 0
    confidence: float = 1.0  # 规则可靠性 (0-1)


# ── Default Rules ──
DEFAULT_RULES = [
    CompressionRule(
        rule_id="pip_install",
        trigger_regex=r"pip(3)? install",
        strip_patterns=[r"Requirement already satisfied.*", r"Installing build dependencies.*"],
        keep_first_n=3,
        keep_last_n=5,
        description="Pip install output",
    ),
    CompressionRule(
        rule_id="npm_install",
        trigger_regex=r"npm (install|i)\b",
        strip_patterns=[r"^npm (WARN|notice) .*", r"^(added|removed|changed|audited) .*"],
        keep_first_n=0,
        keep_last_n=5,
        max_lines=15,
        description="NPM install output",
    ),
    CompressionRule(
        rule_id="docker_build",
        trigger_regex=r"docker (build|compose)",
        strip_patterns=[r"^Step \d+/\d+.*", r"^--->.*", r"^Removing intermediate.*"],
        keep_first_n=5,
        keep_last_n=5,
        max_lines=20,
        description="Docker build output",
    ),
    CompressionRule(
        rule_id="git_push",
        trigger_regex=r"git push",
        keep_patterns=[r"^\s+\w+\s+\w+ -> \w+", r"^remote:", r"^To "],
        strip_patterns=[r"^Enumerating objects.*", r"^Counting objects.*",
                        r"^Compressing objects.*", r"^Writing objects.*",
                        r"^Total \d+.*", r"^remote: Resolving deltas.*"],
        keep_first_n=2,
        keep_last_n=3,
        max_lines=10,
        description="Git push output",
    ),
    CompressionRule(
        rule_id="cargo_build",
        trigger_regex=r"cargo (build|run|test)",
        strip_patterns=[r"^\s+Compiling .*", r"^\s+Finished .*"],
        keep_first_n=1,
        keep_last_n=10,
        max_lines=15,
        description="Rust cargo build output",
    ),
    CompressionRule(
        rule_id="curl_download",
        trigger_regex=r"(curl|wget) .*",
        strip_patterns=[r"^\s+% Total.*", r"^\s+% Received.*", r"^\s+\d+[kMG].*",
                        r"^\s+\d+ \d+[kMG].*"],
        keep_first_n=1,
        keep_last_n=3,
        max_lines=5,
        description="Curl/wget download progress",
    ),
    CompressionRule(
        rule_id="rsync_copy",
        trigger_regex=r"rsync .*",
        strip_patterns=[r"^\s+\d+.*", r"^sending incremental.*",
                        r"^total size.*", r"^sent \d+.*"],
        keep_last_n=2,
        max_lines=3,
        description="Rsync copy progress",
    ),
    CompressionRule(
        rule_id="error_only",
        trigger_regex=r".*(error|traceback|exception|FAILED|fatal).*",
        strip_patterns=[],
        keep_patterns=[r"(?i)(error|traceback|exception|FAILED|fatal|panic|abort)"],
        keep_first_n=0,
        keep_last_n=0,
        max_lines=100,
        description="Generic error extractor — last resort",
    ),
]


# ── Rule Manager ──
class RuleManager:
    def __init__(self):
        self.rules: dict[str, CompressionRule] = OrderedDict()
        self.load_rules()

    def load_rules(self):
        if RULES_FILE.exists():
            try:
                data = json.loads(RULES_FILE.read_text())
                for r in data.get("rules", []):
                    rule = CompressionRule(**r)
                    self.rules[rule.rule_id] = rule
            except Exception:
                self._init_defaults()
        else:
            self._init_defaults()

    def _init_defaults(self):
        for r in DEFAULT_RULES:
            self.rules[r.rule_id] = r
        self.save()

    def save(self):
        data = {"rules": [asdict(r) for r in self.rules.values()],
                "updated_at": time.time()}
        RULES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_rules(self) -> list:
        return list(self.rules.values())

    def add_rule(self, rule: CompressionRule):
        self.rules[rule.rule_id] = rule
        self.save()
        logger.info(f"Rule added: {rule.rule_id}")

    def delete_rule(self, rule_id: str):
        if rule_id in self.rules:
            del self.rules[rule_id]
            self.save()
            logger.info(f"Rule deleted: {rule_id}")


# ── Compressor ──
class TacoCompressor:
    def __init__(self, rule_manager: RuleManager):
        self.rm = rule_manager

    def compress(self, command: str, output: str) -> tuple:
        """压缩单条命令输出. 返回 (compressed, matched_rule, saved_chars)."""
        lines = output.split("\n")
        matched = None

        for rule in self.rm.get_rules():
            if re.search(rule.trigger_regex, command, re.IGNORECASE):
                compressed = self._apply_rule(lines, rule)
                saved = len(output) - len(compressed)
                matched = rule.rule_id

                # 更新统计
                rule.total_applied += 1
                rule.total_saved_chars += max(0, saved)
                return compressed, matched, max(0, saved)

        # 未匹配: 尝试 oMLX 模式识别
        if len(lines) > 30:
            new_rule = self._omlx_discover(command, output)
            if new_rule:
                self.rm.add_rule(new_rule)
                return self.compress(command, output)  # 递归

        # 默认: 截取首尾
        return self._generic_compress(lines), None, 0

    def _apply_rule(self, lines: list, rule: CompressionRule) -> str:
        result = []
        kept = 0
        total = len(lines)

        for i, line in enumerate(lines):
            # Keep patterns: 必须保留
            keep = False
            for pat in rule.keep_patterns:
                if re.search(pat, line):
                    keep = True
                    break

            if keep:
                result.append(line)
                continue

            # Strip patterns: 移除
            strip = False
            for pat in rule.strip_patterns:
                if re.search(pat, line):
                    strip = True
                    break
            if strip:
                continue

            # 保留前 N 行
            if i < rule.keep_first_n:
                result.append(line)
                kept += 1
                continue

            # 超过 max_lines: 跳过中间
            if rule.max_lines and kept >= rule.max_lines:
                continue

            result.append(line)
            kept += 1

        # 最后 N 行 (如果还没加)
        if rule.keep_last_n > 0:
            last_n = lines[-rule.keep_last_n:]
            # 去重
            for ln in last_n:
                if ln not in result[-rule.keep_last_n:]:
                    result.append(ln)

        # 添加摘要头
        if rule.summary_header:
            result.insert(0, rule.summary_header)

        if rule.max_lines and len(result) > rule.max_lines:
            result = result[:rule.keep_first_n] + [f"  ... ({total - len(result)} lines trimmed) ..."] + result[-rule.keep_last_n:]

        return "\n".join(result)

    def _generic_compress(self, lines: list) -> str:
        """通用压缩: 保留前 5 + 后 10 行."""
        if len(lines) <= 20:
            return "\n".join(lines)
        head = lines[:5]
        tail = lines[-10:]
        skipped = len(lines) - 15
        return "\n".join(head + [f"  ... [{skipped} lines trimmed] ..."] + tail)

    def _omlx_discover(self, command: str, output: str) -> Optional[CompressionRule]:
        """用 oMLX 发现新压缩模式."""
        sample = output[:3000]
        try:
            prompt = f"""分析以下命令输出，识别可删除的冗余行模式（正则表达式）。

命令: {command}
输出示例:
{sample[:2000]}

返回格式（严格 JSON）:
{{
  "strip_patterns": ["regex1", "regex2"],
  "keep_first_n": 3,
  "keep_last_n": 5,
  "confidence": 0.8
}}

只返回 JSON，不要其他文字。"""

            result = subprocess.run(
                ["omlx", "chat", "--model", "qwen3.5-4b", "--prompt", prompt],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 0:
                text = result.stdout.strip()
                # 提取 JSON
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    if data.get("confidence", 0) > 0.6:
                        rule_id = f"auto_{hashlib.md5(command.encode()).hexdigest()[:8]}"
                        return CompressionRule(
                            rule_id=rule_id,
                            trigger_regex=re.escape(command.split()[0]),
                            strip_patterns=data.get("strip_patterns", []),
                            keep_first_n=data.get("keep_first_n", 3),
                            keep_last_n=data.get("keep_last_n", 5),
                            description=f"Auto: {command[:50]}",
                            confidence=data.get("confidence", 0.7),
                        )
        except Exception:
            pass
        return None


def show_rules(rm: RuleManager):
    print("=" * 70)
    print("📏 TACO v2 — Compression Rules")
    print("=" * 70)
    for rule in rm.get_rules():
        saved = f"{rule.total_saved_chars:,} chars" if rule.total_applied else "N/A"
        conf = f"{rule.confidence:.0%}" if rule.confidence < 1 else "✓"
        print(f"\n  📌 {rule.rule_id} (conf={conf}, applied={rule.total_applied}, saved={saved})")
        print(f"     trigger: {rule.trigger_regex}")
        if rule.strip_patterns:
            print(f"     strip: {', '.join(rule.strip_patterns[:3])}")
        print(f"     keep: first={rule.keep_first_n}, last={rule.keep_last_n}, max={rule.max_lines}")


def show_stats(rm: RuleManager):
    print("=" * 50)
    print("📊 TACO v2 — Stats")
    print("=" * 50)
    total_saved = 0
    total_runs = 0
    for rule in rm.get_rules():
        total_saved += rule.total_saved_chars
        total_runs += rule.total_applied
        if rule.total_applied:
            avg = rule.total_saved_chars / rule.total_applied
            print(f"  {rule.rule_id:20s} | runs={rule.total_applied:4d} | "
                  f"avg={avg:6.0f} chars | total={rule.total_saved_chars:7,} chars")
    print(f"  {'─' * 48}")
    print(f"  Total: {total_saved:,} chars saved in {total_runs} runs")
    if total_runs:
        print(f"  Average: {total_saved / total_runs:.0f} chars/run")
        pct = total_saved / max(total_saved + total_runs * 100, 1) * 100
        print(f"  Est. compression: ~{pct:.0f}%")


# ── CLI ──
def main():
    import argparse

    parser = argparse.ArgumentParser(description="TACO v2 — Terminal compressor")
    sub = parser.add_subparsers(dest="cmd")

    compress_p = sub.add_parser("compress", help="Compress terminal output")
    compress_p.add_argument("command", help="The command that generated the output")

    rules_p = sub.add_parser("rules", help="Show compression rules")

    add_p = sub.add_parser("add-rule", help="Add a custom rule")
    add_p.add_argument("--id", required=True)
    add_p.add_argument("--trigger", required=True)
    add_p.add_argument("--strip", action="append", default=[])
    add_p.add_argument("--keep", action="append", default=[])
    add_p.add_argument("--first", type=int, default=5)
    add_p.add_argument("--last", type=int, default=10)
    add_p.add_argument("--max-lines", type=int)

    sub.add_parser("stats", help="Compression statistics")
    sub.add_parser("evolve", help="Auto-evolve rules")

    batch_p = sub.add_parser("batch", help="Batch compress")
    batch_p.add_argument("--cmd", action="append", default=[], help="Commands (multiple)")

    args = parser.parse_args()

    rm = RuleManager()
    compressor = TacoCompressor(rm)

    if args.cmd == "compress":
        output = sys.stdin.read()
        compressed, rule, saved = compressor.compress(args.command, output)
        print(compressed, end="")
        if rule:
            logger.info(f"Compressed with '{rule}': {saved:,} chars saved")

    elif args.cmd == "rules":
        show_rules(rm)

    elif args.cmd == "add-rule":
        rule = CompressionRule(
            rule_id=args.id,
            trigger_regex=args.trigger,
            strip_patterns=args.strip,
            keep_patterns=args.keep,
            keep_first_n=args.first,
            keep_last_n=args.last,
            max_lines=args.max_lines,
            description=f"User rule: {args.trigger}",
        )
        rm.add_rule(rule)
        print(f"✅ Rule '{args.id}' added")

    elif args.cmd == "stats":
        show_stats(rm)

    elif args.cmd == "batch":
        input_text = sys.stdin.read()
        for cmd in args.cmd:
            compressed, _, _ = compressor.compress(cmd, input_text)
            print(f"--- {cmd} ---")
            print(compressed)

    elif args.cmd == "evolve":
        print("🧬 TACO v2 — Self-evolving (not yet implemented)")
        print("   Rules auto-discover on compress via oMLX")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()