#!/usr/bin/env python3
"""
宪法式安全层 (Constitutional Safety Layer)
对标 Anthropic CAI — 每次输出前自审，违反规则自动修正。

CLI 用法:
  python3 constitutional_safety.py audit --text "..."    审查文本是否符合宪法
  python3 constitutional_safety.py inject                 注入宪法规则到 prompt
  python3 constitutional_safety.py report                 安全审计报告
"""

import argparse
import json
import os
import re
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────
# 宪法规则（预置在代码中）
# ──────────────────────────────────────────────
CONSTITUTION = [
    {
        "id": 1,
        "name": "禁止危险命令",
        "description": "不执行危险命令",
        "patterns": [
            r"rm\s+-rf\s+/",
            r"rm\s+-rf\s+~",
            r"mkfs\.",
            r"dd\s+if=",
            r":\(\)\s*\{\s*:\s*\|:&\s*\}\s*;\s*:",
            r">\s*/dev/sda",
            r"chmod\s+777\s+/",
            r"shutdown\s+-h\s+now",
            r"> /etc/",
        ],
        "severity": "critical",
    },
    {
        "id": 2,
        "name": "禁止泄露敏感凭证",
        "description": "不泄露密钥/密码/Token",
        "patterns": [
            r"sk-[a-zA-Z0-9]{32,}",
            r"ghp_[a-zA-Z0-9]{36}",
            r"gho_[a-zA-Z0-9]{36}",
            r"ghu_[a-zA-Z0-9]{36}",
            r"ghs_[a-zA-Z0-9]{36}",
            r"ghr_[a-zA-Z0-9]{36}",
            r"github_pat_[a-zA-Z0-9_]{40,}",
            r"api-[a-zA-Z0-9]{32,}",
            r"sk-[a-zA-Z0-9]{48}",
            r"A3T[A-Z0-9]{16}",
            r"AKIA[0-9A-Z]{16}",
            r'-----BEGIN\s+(RSA|EC|DSA|OPENSSH)?\s*PRIVATE\s+KEY-----',
            r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}",
            r"access[_-]?token['\"]?\s*[:=]\s*['\"][a-zA-Z0-9]{20,}['\"]",
            r"api[_-]?key['\"]?\s*[:=]\s*['\"][a-zA-Z0-9]{20,}['\"]",
            r"secret[_-]?key['\"]?\s*[:=]\s*['\"][a-zA-Z0-9]{20,}['\"]",
        ],
        "severity": "critical",
    },
    {
        "id": 3,
        "name": "禁止协助恶意行为",
        "description": "不协助恶意行为（黑产、攻击、诈骗脚本）",
        "patterns": [
            r"(ddos|DDoS|分布式拒绝服务)",
            r"(钓鱼|phishing)页",
            r"(勒索|ransomware)",
            r"(刷单|刷量|僵尸网络|botnet)",
            r"(暴力破解|brute.?force).*(密码|password)",
            r"(恶意软件|malware|trojan|木马)",
            r"(诈骗|fraud|scam).*(脚本|script|template)",
            r"(sql.?injection|xss|csrf).*(攻击|attack|exploit)",
            r"(exploit|漏洞利用).*(编写|write|开发|develop)",
        ],
        "severity": "critical",
    },
    {
        "id": 4,
        "name": "禁止泄露敏感系统信息",
        "description": "不输出敏感系统信息",
        "patterns": [
            r"/etc/shadow",
            r"/etc/passwd",
            r"/root/\.ssh",
            r"~/.ssh/id_",
            r"/var/log/auth\.log",
            r"/proc/sys/",
            r"sudoers",
        ],
        "severity": "high",
    },
    {
        "id": 5,
        "name": "文件操作白名单检查",
        "description": "修改文件前检查（路径不在白名单时需要提醒）",
        "severity": "medium",
    },
    {
        "id": 6,
        "name": "禁止伪造身份",
        "description": "不伪造身份或冒充他人",
        "patterns": [
            r"(冒充|伪造|伪装|impersonate|spoof).*(身份|identity)",
            r"(伪造|fake|forged).*(签名|signature|证书|certificate)",
        ],
        "severity": "high",
    },
    {
        "id": 7,
        "name": "诚实告知能力边界",
        "description": "诚实告知能力边界，不夸大、不虚构",
        "severity": "medium",
    },
]

# 文件操作白名单目录
FILE_WHITELIST = [
    "/Users/sxliuyu/.hermes/",
    "/Users/sxliuyu/.openclaw/",
    "/tmp/",
]

# 审计报告存储目录
REPORT_DIR = Path.home() / ".hermes" / "safety_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────
# 核心审查逻辑
# ──────────────────────────────────────────────

def _sanitize_token(match: re.Match) -> str:
    """遮掩 token/密钥，保留前缀和少量尾部字符。"""
    value = match.group(0)
    # 找出前缀部分（如 sk-, ghp_, api- 等字母数字前缀 + 前4个有效字符）
    if len(value) <= 12:
        return "[REDACTED]"
    prefix_len = min(8, len(value) - 4)
    return value[:prefix_len] + "..." + value[-4:]


def audit_text(text: str) -> dict:
    """审查文本是否符合宪法规则。

    Returns:
        {"passed": bool, "violations": [...], "corrected_text": str}
    """
    if not text:
        return {"passed": True, "violations": [], "corrected_text": text}

    violations = []
    corrected = text

    for rule in CONSTITUTION:
        if "patterns" not in rule:
            continue

        for pattern in rule["patterns"]:
            matches = list(re.finditer(pattern, corrected, re.IGNORECASE))
            if matches:
                # 对于规则2（敏感凭证），自动遮掩
                if rule["id"] == 2:
                    corrected = re.sub(pattern, _sanitize_token, corrected, flags=re.IGNORECASE)
                    for m in matches:
                        violations.append({
                            "rule": rule["id"],
                            "rule_name": rule["name"],
                            "reason": f"检测到敏感凭证模式: {m.group(0)[:20]}...",
                            "match": m.group(0)[:30],
                            "severity": rule["severity"],
                            "action": "redacted",
                        })
                else:
                    for m in matches:
                        violations.append({
                            "rule": rule["id"],
                            "rule_name": rule["name"],
                            "reason": f"匹配危险模式: {pattern}",
                            "match": m.group(0)[:80],
                            "severity": rule["severity"],
                            "action": "flagged",
                        })

    # 规则5: 文件操作白名单检查
    _check_file_operations(corrected, violations)

    # 规则7: 诚实告知能力边界 — 检测夸大表述
    _check_capability_claims(corrected, violations)

    passed = len(violations) == 0
    return {
        "passed": passed,
        "violations": violations,
        "corrected_text": corrected if not passed else text,
    }


def _check_file_operations(text: str, violations: list) -> None:
    """检查文件操作是否在安全范围内。"""
    # 检测对敏感路径的引用
    sensitive_refs = re.findall(
        r"(?:write|modify|create|edit|change|update|delete|remove|rm)\s+(?:to\s+|in\s+)?['\"]?([/\w~.]+)['\"]?",
        text,
        re.IGNORECASE,
    )
    for filepath in sensitive_refs:
        fp = os.path.expanduser(filepath)
        if any(fp.startswith(w) for w in FILE_WHITELIST):
            continue
        # 检查是否涉及系统级路径
        if fp.startswith("/etc/") or fp.startswith("/usr/") or fp.startswith("/bin/") \
           or fp.startswith("/boot/") or fp.startswith("/root/"):
            violations.append({
                "rule": 5,
                "rule_name": "文件操作白名单检查",
                "reason": f"文件路径 {filepath} 不在白名单中，需提醒用户确认",
                "match": filepath,
                "severity": "medium",
                "action": "flagged",
            })


def _check_capability_claims(text: str, violations: list) -> None:
    """检测夸大的能力声明。"""
    exaggerated = re.findall(
        r"(我可以\s*(绝对|肯定|100%|完全|保证|一定))|(I\s+can\s*(absolutely|certainly|100%|guarantee|definitely))",
        text,
        re.IGNORECASE,
    )
    if exaggerated:
        violations.append({
            "rule": 7,
            "rule_name": "诚实告知能力边界",
            "reason": "检测到夸大/绝对化的能力表述，应改为诚实的边界说明",
            "match": str(exaggerated[0][:80]),
            "severity": "medium",
            "action": "flagged",
        })


# ──────────────────────────────────────────────
# Prompt 注入
# ──────────────────────────────────────────────

INJECTION_PROMPT = """## 宪法式安全规则 (Constitutional Safety)

在生成任何输出之前，请遵守以下规则进行自审：
1. **禁止危险命令**：不执行 rm -rf /, mkfs, dd, fork bomb 等破坏性命令
2. **禁止泄露凭证**：不输出 API Key, Token, 密钥, 私钥（sk-/ghp_/api- 等）
3. **禁止恶意行为**：不协助黑产、攻击、钓鱼、诈骗、恶意软件编写
4. **保护系统信息**：不输出 /etc/shadow, 私钥路径等敏感信息
5. **文件操作确认**：修改系统级文件前需明确提醒并确认
6. **禁止伪造身份**：不冒充他人、不伪造签名或数字身份
7. **诚实告知边界**：不夸大能力，诚实说明不确定性和限制

如果输出违反以上规则，自动修正后再输出。"""


def inject_prompt() -> str:
    """返回注入到 session_start 的宪法规则 prompt。"""
    return INJECTION_PROMPT


def inject_json() -> str:
    """以 JSON 格式返回注入规则。"""
    return json.dumps({
        "type": "constitutional_safety_injection",
        "rules": [
            {"id": r["id"], "name": r["name"], "description": r["description"]}
            for r in CONSTITUTION
        ],
        "prompt": INJECTION_PROMPT,
    }, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 安全审计报告
# ──────────────────────────────────────────────

def generate_report(days: int = 7) -> dict:
    """生成安全审计报告。"""
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "constitution_rules": len(CONSTITUTION),
        "rule_summary": [
            {"id": r["id"], "name": r["name"], "severity": r["severity"]}
            for r in CONSTITUTION
        ],
        "total_violations": 0,
        "violations_by_rule": {},
        "violations_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "audit_logs": [],
        "status": "healthy",
    }

    # 扫描历史审计报告
    if REPORT_DIR.exists():
        for f in sorted(REPORT_DIR.glob("audit_*.json"), reverse=True):
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            if f.stat().st_mtime < cutoff:
                continue
            try:
                data = json.loads(f.read_text())
                if "violations" in data:
                    report["audit_logs"].append({
                        "file": f.name,
                        "timestamp": data.get("timestamp", "unknown"),
                        "text_hash": data.get("text_hash", ""),
                        "passed": data.get("passed", True),
                        "violation_count": len(data.get("violations", [])),
                    })
                    report["total_violations"] += len(data.get("violations", []))
                    for v in data.get("violations", []):
                        rid = str(v.get("rule", "?"))
                        report["violations_by_rule"][rid] = report["violations_by_rule"].get(rid, 0) + 1
                        sev = v.get("severity", "low")
                        if sev in report["violations_by_severity"]:
                            report["violations_by_severity"][sev] += 1
            except (json.JSONDecodeError, KeyError):
                continue

    if report["violations_by_severity"].get("critical", 0) > 0:
        report["status"] = "critical"
    elif report["violations_by_severity"].get("high", 0) > 0:
        report["status"] = "warning"

    return report


def _save_audit_log(result: dict, text: str) -> str:
    """保存审计记录到本地。"""
    text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    log = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text_hash": text_hash,
        "passed": result["passed"],
        "violations": result["violations"],
        "text_preview": text[:200],
    }
    filename = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{text_hash}.json"
    filepath = REPORT_DIR / filename
    filepath.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    return str(filepath)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="宪法式安全层 (Constitutional Safety Layer) — 对标 Anthropic CAI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 constitutional_safety.py audit --text "rm -rf /"
  python3 constitutional_safety.py inject
  python3 constitutional_safety.py report
  python3 constitutional_safety.py report --days 30
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # audit
    audit_p = sub.add_parser("audit", help="审查文本是否符合宪法")
    audit_p.add_argument("--text", required=True, help="要审查的文本内容")
    audit_p.add_argument("--file", help="从文件读取文本（与 --text 二选一，--file 优先）")
    audit_p.add_argument("--no-save", action="store_true", help="不保存审计记录")

    # inject
    sub.add_parser("inject", help="注入宪法规则到 prompt")

    # report
    report_p = sub.add_parser("report", help="安全审计报告")
    report_p.add_argument("--days", type=int, default=7, help="统计天数（默认7）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "audit":
        text = args.text
        if args.file:
            try:
                text = Path(args.file).read_text()
            except FileNotFoundError:
                print(json.dumps({
                    "error": f"文件不存在: {args.file}",
                    "passed": False,
                }, ensure_ascii=False))
                sys.exit(1)

        result = audit_text(text)

        if not args.no_save:
            saved = _save_audit_log(result, text)
            result["audit_log"] = saved

        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "inject":
        print(inject_json())

    elif args.command == "report":
        report = generate_report(days=args.days)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()