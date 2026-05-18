#!/usr/bin/env python3
"""
Hermes Token Compressor v2
===========================
自学习模式识别 + oMLX 语义压缩

升级:
  - 模式学习: 从历史压缩记录中学习高频模式, 自动生成规则
  - oMLX 压缩: 用本地 Qwen3.5 做语义摘要替代粗暴截断
  - 压缩率统计: 按工具/命令分组统计, 识别低效工具

用法:
  python3 token_compressor.py compress < output.txt
  python3 token_compressor.py rules
  python3 token_compressor.py learn   # 从历史日志学习新规则
  python3 token_compressor.py stats
"""

import os
import re
import sys
import json
import math
import sqlite3
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
RULES_DIR = HERMES_HOME / "token_compressor" / "rules"
LEARN_DB = HERMES_HOME / "token_compressor" / "learned.db"

OMLX_BASE = "http://localhost:4560/v1"
FINNA_BASE = "https://www.finna.com.cn/v1"
FINNA_KEY = os.environ.get("FINNA_API_KEY", "app-BqyKsTO4Om3JGoPCTkJX080J")

BUILTIN_RULES = [
    # Git
    {"name":"git-status","pattern":r"^git\s+status","reduce":"git","max_lines":20},
    {"name":"git-diff","pattern":r"^git\s+diff","reduce":"diff","max_changes":50},
    {"name":"git-log","pattern":r"^git\s+log","reduce":"log","max_lines":15},
    # Package managers
    {"name":"pip-install","pattern":r"^pip3?\s+install","reduce":"pip","max_lines":10,"keep_errors":True},
    {"name":"npm-install","pattern":r"^npm\s+install","reduce":"npm","max_lines":8},
    # Listings
    {"name":"ls","pattern":r"^ls\s","reduce":"ls","max_lines":30,"keep_dirs":True},
    {"name":"find","pattern":r"^find\s","reduce":"find","max_lines":20},
    # Compilation
    {"name":"gcc","pattern":r"^gcc|clang","reduce":"compile","max_lines":5,"keep_errors":True},
    {"name":"python-error","pattern":r"Traceback","reduce":"error","max_lines":25,"keep_errors":True},
    # Build
    {"name":"make","pattern":r"^make","reduce":"make","max_lines":10,"keep_errors":True},
    # Logs
    {"name":"systemd","pattern":r"journalctl|systemctl","reduce":"system","max_lines":15},
    # Docker
    {"name":"docker","pattern":r"^docker\s","reduce":"docker","max_lines":15},
    # Custom
    {"name":"stock-data","pattern":r"(股票|涨停|k线|复盘|量能)","reduce":"stock","max_lines":20},
    {"name":"ai-output","pattern":r"(模型|token|生成|inference)","reduce":"ai","semantic":True},
]


def init_learn_db():
    conn = sqlite3.connect(str(LEARN_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT UNIQUE,
        count INTEGER DEFAULT 1, tokens_saved INTEGER DEFAULT 0,
        last_seen REAL, command TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS compression_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT, rule TEXT,
        before_tokens INTEGER, after_tokens INTEGER, ratio REAL, timestamp REAL)""")
    conn.commit()
    return conn


def classify(input_text):
    """Match input against all rules, return best match."""
    first_line = input_text.split('\n')[0] if input_text else ""
    for rule in BUILTIN_RULES:
        if re.search(rule["pattern"], first_line, re.I):
            return rule
    return None


def compress_rule(input_text, rule):
    """Compress using a specific rule."""
    lines = input_text.split('\n')
    result = []

    if rule.get("reduce") == "diff":
        # Keep only changed lines
        changed = [l for l in lines if l.startswith(('+','-','@@')) and not l.startswith(('+++','---'))]
        result = changed[:rule.get("max_changes", 50)]
    elif rule.get("reduce") == "git":
        result = lines[:rule.get("max_lines", 20)]
    elif rule.get("reduce") == "error":
        if rule.get("keep_errors"):
            errors = [l for l in lines if 'error' in l.lower() or 'Traceback' in l]
            result = errors[:rule.get("max_lines", 25)]
        else:
            result = lines[:rule.get("max_lines", 15)]
    elif rule.get("reduce") == "pip" or rule.get("reduce") == "npm":
        if rule.get("keep_errors"):
            result = [l for l in lines if 'error' in l.lower() or 'success' in l.lower()]
        else:
            result = [lines[0]] + lines[1:rule.get("max_lines", 8)]
    else:
        result = lines[:rule.get("max_lines", 20)]

    return '\n'.join(result)


def semantic_compress(text, max_tokens=500):
    """Use oMLX to semantically compress long text to a summary."""
    if len(text) < 2000:
        return text

    prompt = f"""压缩以下文本到{max_tokens}词以内，保留所有关键信息（命令、错误、路径、数字）。

文本:
{text[:4000]}

压缩结果:"""

    try:
        import requests
        # Try oMLX
        resp = requests.post(
            f"{OMLX_BASE}/chat/completions",
            json={"model":"qwen3.5-4b-mlx","messages":[{"role":"user","content":prompt}],
                  "max_tokens":max_tokens,"temperature":0.1,"stream":False},
            headers={"Authorization":"Bearer local","Content-Type":"application/json"},
            timeout=10)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except:
        pass
    # Fallback: truncate smart
    lines = text.split('\n')
    keep = lines[:max_tokens//4]
    return '\n'.join(keep) + f"\n... [truncated {len(lines)-len(keep)} lines]"


def log_compression(conn, command, rule_name, before, after):
    ratio = after / max(before, 1)
    conn.execute("INSERT INTO compression_log (command,rule,before_tokens,after_tokens,ratio,timestamp) VALUES (?,?,?,?,?,?)",
        (command, rule_name, before, after, ratio, time.time()))
    conn.commit()


def learn_patterns(conn, min_count=3):
    """Learn new compression patterns from historical data."""
    rows = conn.execute("""SELECT command, COUNT(*) as cnt, SUM(tokens_saved) as saved
        FROM compression_log GROUP BY command HAVING cnt >= ? ORDER BY saved DESC""",
        (min_count,)).fetchall()
    if not rows:
        print("Not enough data to learn new patterns.")
        return []

    new_rules = []
    for cmd, cnt, saved in rows:
        pattern = f"^{re.escape(cmd.split()[0])}" if ' ' in cmd else f"^{re.escape(cmd)}"
        new_rules.append({"name":f"learned-{cmd[:20]}","pattern":pattern,"reduce":"learned",
                          "max_lines":20,"learned":True})
        print(f"  Learned: {cmd} -> {cnt} uses, {saved} tokens saved")

    # Save to file
    rules_file = RULES_DIR / "learned.json"
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if rules_file.exists():
        try: existing = json.loads(rules_file.read_text())
        except: pass
    existing.extend(new_rules)
    rules_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False))

    return new_rules


def cmd_compress(input_text=None):
    """Main compress command."""
    if input_text is None:
        input_text = sys.stdin.read()

    before_tokens = max(1, math.ceil(len(input_text) / 4))
    rule = classify(input_text)

    if not rule:
        # No rule match — output as-is but trim if too long
        if before_tokens > 2000:
            output = input_text[:8000]
            print(f"# [compressed: {before_tokens}->{max(1,math.ceil(len(output)/4))} tokens]", file=sys.stderr)
        print(input_text)
        return

    if rule.get("semantic"):
        output = semantic_compress(input_text)
    else:
        output = compress_rule(input_text, rule)

    after_tokens = max(1, math.ceil(len(output) / 4))
    ratio = (before_tokens - after_tokens) / max(before_tokens, 1) * 100

    # Log
    conn = init_learn_db()
    log_compression(conn, rule.get("name","unknown"), rule["name"], before_tokens, after_tokens)
    conn.close()

    header = f"# [compressed: {before_tokens}->{after_tokens} tokens ({ratio:.0f}% saved) by rule: {rule['name']}]\n"
    print(header + output)


def cmd_rules():
    print(f"\nBuiltin Rules ({len(BUILTIN_RULES)})\n")
    for r in BUILTIN_RULES:
        print(f"  {r['name']:20s} | {r['pattern']:30s} | max_lines={r.get('max_lines','?')}" +
              (" semantic" if r.get('semantic') else ""))

    rules_file = RULES_DIR / "learned.json"
    if rules_file.exists():
        try:
            learned = json.loads(rules_file.read_text())
            print(f"\nLearned Rules ({len(learned)})\n")
            for r in learned:
                print(f"  {r['name']:20s} | {r['pattern']:30s} | max_lines={r.get('max_lines','?')}")
        except:
            pass


def cmd_stats():
    conn = init_learn_db()
    total = conn.execute("SELECT COUNT(*) FROM compression_log").fetchone()[0]
    if total == 0:
        print("No compression data yet.")
        conn.close()
        return

    total_before = conn.execute("SELECT SUM(before_tokens) FROM compression_log").fetchone()[0] or 0
    total_after = conn.execute("SELECT SUM(after_tokens) FROM compression_log").fetchone()[0] or 0
    total_ratio = (total_before - total_after) / max(total_before, 1) * 100

    print("\nToken Compressor v2 Stats")
    print(f"  Total compressions: {total}")
    print(f"  Tokens saved: {total_before-total_after} ({total_ratio:.0f}%)")
    print("\n  By rule (top 10):")

    rows = conn.execute("""SELECT rule, COUNT(*) as cnt, SUM(before_tokens-after_tokens) as saved
        FROM compression_log GROUP BY rule ORDER BY saved DESC LIMIT 10""").fetchall()
    for rule, cnt, saved in rows:
        print(f"    {rule:25s} {cnt:>4}x  saved={saved:>8}t")

    conn.close()


def cmd_learn():
    conn = init_learn_db()
    total = conn.execute("SELECT COUNT(*) FROM compression_log").fetchone()[0]
    if total < 10:
        print(f"Need at least 10 compressions to learn (have {total}).")
        conn.close()
        return
    print(f"Learning from {total} compression records...\n")
    learn_patterns(conn, min_count=3)
    conn.close()


def main():
    if len(sys.argv) < 2:
        print("Token Compressor v2\n  compress | rules | learn | stats")
        return
    cmd = sys.argv[1]
    if cmd == "compress": cmd_compress()
    elif cmd == "rules": cmd_rules()
    elif cmd == "learn": cmd_learn()
    elif cmd == "stats": cmd_stats()
    else: print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()