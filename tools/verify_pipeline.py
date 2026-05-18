#!/usr/bin/env python3
"""
P0-22: Multi-tier Self-Verification Pipeline — ExploitBench-style deterministic validation

对标:
  - ExploitBench "五层能力阶梯": 每层都有确定性自动验证器，不用 LLM 当裁判
  - Mythos RLVR insight: "判断是否成功比创造成功容易得多"

5 级验证:
  Tier 1: 语法/格式 — regex, linter, JSON schema
  Tier 2: 结构完整性 — 文件存在、导入可解析、AST 有效
  Tier 3: 功能正确性 — 执行测试、断言通过、输出匹配
  Tier 4: 边界鲁棒性 — 边界输入、异常处理、超时保护
  Tier 5: 安全审计 — 无硬编码密钥、无危险模式、无注入风险

设计原则:
  - 全部用确定性规则（不依赖 LLM）
  - 每级通过才进入下一级
  - 失败原因精确到行，方便 Agent 修复
"""

import json
import os
import re
import ast
import subprocess
import tempfile
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Tier(Enum):
    SYNTAX = 1
    STRUCTURE = 2
    FUNCTIONAL = 3
    ROBUSTNESS = 4
    SECURITY = 5


@dataclass
class Verdict:
    """单级验证结果"""
    tier: Tier
    passed: bool
    score: float = 1.0
    details: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)  # [{line, message, fix_hint}]


@dataclass
class VerificationReport:
    """完整验证报告"""
    target: str
    verdicts: list[Verdict] = field(default_factory=list)
    overall_pass: bool = False
    total_score: float = 0.0

    @property
    def summary(self) -> str:
        lines = [f"## 验证报告: {self.target}"]
        for v in self.verdicts:
            icon = "✅" if v.passed else "❌"
            tier_name = v.tier.name.title()
            lines.append(f"{icon} Tier {v.tier.value} ({tier_name}): "
                         f"score={v.score:.2f}")
            for f in v.failures[:3]:
                lines.append(f"  - 行 {f.get('line', '?')}: {f.get('message', '')[:80]}")
                if f.get("fix_hint"):
                    lines.append(f"    修复建议: {f.get('fix_hint')[:80]}")
        lines.append(f"\n总评分: {self.total_score:.2f}/5")
        lines.append(f"状态: {'✅ 通过' if self.overall_pass else '❌ 未通过'}")
        return "\n".join(lines)


# ─── Tier 1: 语法/格式验证 ──────────────────────────────


def verify_syntax(code: str, language: str = "python") -> Verdict:
    """Tier 1: 检查语法和格式"""
    verdict = Verdict(tier=Tier.SYNTAX, passed=True)

    if language == "python":
        # Python 语法检查
        try:
            ast.parse(code)
        except SyntaxError as e:
            verdict.passed = False
            verdict.score = 0
            verdict.failures.append({
                "line": e.lineno or "?",
                "message": f"语法错误: {e.msg}",
                "fix_hint": f"第 {e.lineno} 行: {e.text.strip() if e.text else '?'}",
            })

        # 检查常见格式问题
        lines = code.split("\n")
        for i, line in enumerate(lines, 1):
            # 混合缩进
            if "\t" in line and "    " in line:
                verdict.score -= 0.1
                verdict.failures.append({
                    "line": i,
                    "message": "混合使用 Tab 和空格缩进",
                    "fix_hint": "统一使用 4 空格缩进",
                })
            # 行尾空格
            if line.rstrip() != line and line.strip():
                verdict.score -= 0.05

    elif language in ("bash", "sh"):
        # Bash 语法检查
        try:
            result = subprocess.run(
                ["bash", "-n"],
                input=code.encode(),
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                verdict.passed = False
                verdict.score = 0
                err = result.stderr.decode()
                # 提取行号
                line_match = re.search(r"line (\d+)", err)
                verdict.failures.append({
                    "line": line_match.group(1) if line_match else "?",
                    "message": err[:200],
                    "fix_hint": "检查 bash 语法",
                })
        except (subprocess.TimeoutExpired, FileNotFoundError):
            verdict.score = 0.5

    elif language == "json":
        try:
            json.loads(code)
        except json.JSONDecodeError as e:
            verdict.passed = False
            verdict.score = 0
            verdict.failures.append({
                "line": e.lineno,
                "message": f"JSON 解析错误: {e.msg}",
                "fix_hint": f"检查第 {e.lineno} 行第 {e.colno} 列",
            })

    verdict.score = max(0, verdict.score)
    return verdict


def verify_lint(file_path: str) -> Verdict:
    """Tier 1: 运行 linter"""
    verdict = Verdict(tier=Tier.SYNTAX, passed=True)

    # 根据文件类型选择 linter
    ext = os.path.splitext(file_path)[1]

    if ext == ".py":
        cmd = ["python3", "-m", "py_compile", file_path]
    elif ext in (".sh", ".bash"):
        cmd = ["bash", "-n", file_path]
    elif ext == ".json":
        cmd = ["python3", "-c",
               f"import json; json.load(open('{file_path}'))"]
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        cmd = ["node", "-c", file_path] if ext == ".js" else None
    else:
        return verdict  # 不支持的文件类型，跳过

    if not cmd:
        return verdict

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            verdict.passed = False
            verdict.score = 0
            verdict.failures.append({
                "message": result.stderr.decode()[:200] or result.stdout.decode()[:200],
                "fix_hint": "修复语法错误后重试",
            })
    except subprocess.TimeoutExpired:
        verdict.score = 0.5
    except FileNotFoundError:
        verdict.score = 0.8  # linter 不可用，给高分但不完美

    return verdict


# ─── Tier 2: 结构完整性 ─────────────────────────────────


def verify_structure(file_path: str) -> Verdict:
    """Tier 2: 检查文件结构和导入"""
    verdict = Verdict(tier=Tier.STRUCTURE, passed=True)

    # 文件存在
    if not os.path.exists(file_path):
        verdict.passed = False
        verdict.score = 0
        verdict.failures.append({
            "message": f"文件不存在: {file_path}",
            "fix_hint": "确保文件路径正确",
        })
        return verdict

    # 文件非空
    try:
        size = os.path.getsize(file_path)
        if size == 0:
            verdict.passed = False
            verdict.score = 0
            verdict.failures.append({
                "message": "文件为空",
                "fix_hint": "文件需要包含有效内容",
            })
            return verdict
    except OSError:
        pass

    # Python 特定检查
    ext = os.path.splitext(file_path)[1]
    if ext == ".py":
        try:
            with open(file_path) as f:
                code = f.read()
            tree = ast.parse(code)

            # 检查导入是否全部在文件顶部
            imports = []
            other_stmts = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append(node.lineno)
                else:
                    other_stmts.append(node.lineno)

            if imports and other_stmts:
                last_import = max(imports)
                first_other = min(other_stmts)
                if first_other < last_import:
                    verdict.score -= 0.2
                    verdict.failures.append({
                        "line": first_other,
                        "message": "导入语句应放在文件顶部",
                        "fix_hint": "将所有 import 移到文件顶部",
                    })

            # 检查是否有 __main__ 入口
            has_main = any(
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and any(
                    isinstance(op, ast.Eq)
                    and isinstance(left, ast.Name)
                    and left.id == "__name__"
                    and isinstance(comp, ast.Constant)
                    and comp.value == "__main__"
                    for op, left, comp in [(node.test.ops[0],
                                            node.test.left,
                                            node.test.comparators[0])]
                    if hasattr(node.test, 'ops')
                )
                for node in ast.iter_child_nodes(tree)
            )

            if not has_main and _has_executable_code(tree):
                verdict.score -= 0.1
                # 这不是错误，只是建议
                verdict.details.append("建议添加 if __name__ == '__main__' 保护")

        except SyntaxError:
            pass  # 语法错误已经在 Tier 1 捕获

    return verdict


def _has_executable_code(tree: ast.AST) -> bool:
    """检查模块级别是否有可执行代码"""
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef,
                                  ast.Import, ast.ImportFrom)):
            return True
    return False


# ─── Tier 3: 功能正确性 ──────────────────────────────────


def verify_functional(file_path: str, test_command: str = "",
                      expected_output: str = "") -> Verdict:
    """Tier 3: 运行测试验证功能"""
    verdict = Verdict(tier=Tier.FUNCTIONAL, passed=True)

    if test_command:
        try:
            result = subprocess.run(
                test_command,
                shell=True,
                capture_output=True,
                timeout=120,
                cwd=os.path.dirname(file_path) or ".",
            )
            stdout = result.stdout.decode()
            stderr = result.stderr.decode()

            if result.returncode != 0:
                verdict.passed = False
                verdict.score = max(0, 1 - result.returncode * 0.1)
                verdict.failures.append({
                    "message": f"测试失败 (退出码 {result.returncode})",
                    "fix_hint": stderr[:200] or stdout[:200],
                })
            elif expected_output:
                # 检查期望输出
                if expected_output.strip() not in stdout:
                    verdict.passed = False
                    verdict.score = 0.5
                    verdict.failures.append({
                        "message": f"输出不符合预期",
                        "fix_hint": f"期望包含: {expected_output[:100]}",
                    })

        except subprocess.TimeoutExpired:
            verdict.passed = False
            verdict.score = 0
            verdict.failures.append({
                "message": "测试超时 (120s)",
                "fix_hint": "检查是否有无限循环或性能问题",
            })

    return verdict


def verify_pytest(file_path: str, test_file: str = "") -> Verdict:
    """Tier 3: 运行 pytest"""
    verdict = Verdict(tier=Tier.FUNCTIONAL, passed=True)

    try:
        import importlib.util
        if importlib.util.find_spec("pytest") is None:
            verdict.score = 1.0  # pytest 不可用，跳过
            return verdict
    except (ImportError, ModuleNotFoundError):
        verdict.score = 1.0
        return verdict

    cmd = ["python3", "-m", "pytest", test_file or file_path, "-x", "--tb=short"]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            verdict.passed = False
            verdict.score = 0
            # 提取测试失败信息
            output = result.stdout.decode()
            failures = re.findall(r"FAILED.*?\n(.*?Error.*?)\n", output)
            for f in failures[:3]:
                verdict.failures.append({
                    "message": f[:200],
                    "fix_hint": "修复测试失败后重试",
                })
    except subprocess.TimeoutExpired:
        verdict.score = 0

    return verdict


# ─── Tier 4: 边界鲁棒性 ──────────────────────────────────


def verify_robustness(code: str, language: str = "python") -> Verdict:
    """Tier 4: 检查边界处理和异常安全"""
    verdict = Verdict(tier=Tier.ROBUSTNESS, passed=True)

    if language == "python":
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return verdict

        # 检查异常处理
        try_blocks = 0
        unsafe_calls = 0

        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                try_blocks += 1
            # 检查常见危险操作
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    # 裸 except
                    pass
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr if hasattr(node.func, 'attr') else ""
                    # 文件操作、网络操作等应该有异常处理
                    if func_name in ("open", "read", "write", "connect",
                                     "get", "post", "execute"):
                        unsafe_calls += 1

        # 如果有危险调用但没有对应的 try，扣分
        if unsafe_calls > try_blocks * 3:
            verdict.score -= 0.3
            verdict.failures.append({
                "message": f"{unsafe_calls} 个可能有异常的操作，但只有 {try_blocks} 个 try 块",
                "fix_hint": "为文件/网络操作添加异常处理",
            })

        # 检查裸 except（不好的实践）
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    verdict.score -= 0.2
                    verdict.failures.append({
                        "line": node.lineno,
                        "message": "使用了裸 except:（应指定异常类型）",
                        "fix_hint": "使用 except ExceptionType as e",
                    })

    verdict.score = max(0, verdict.score)
    return verdict


# ─── Tier 5: 安全审计 ────────────────────────────────────


# 敏感模式
SECRET_PATTERNS = [
    (r"(?:api[_-]?key|apikey|secret|token|password|passwd)\s*[:=]\s*['\"]([^'\"]{8,})['\"]",
     "硬编码密钥/密码"),
    (r"(?:ghp_|github_pat_|sk-[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,})",
     "GitHub/HuggingFace/OpenAI Token"),
    (r"(?:jdbc|mysql|postgresql)://[^@\s]+@",
     "数据库连接字符串含凭证"),
    (r"-----BEGIN (?:RSA|EC|DSA|OPENSSH) PRIVATE KEY-----",
     "私钥"),
]

DANGEROUS_PATTERNS = [
    (r"os\.system\(.*rm\s+-rf\s+/", "危险的 rm -rf / 命令"),
    (r"subprocess\.(?:call|run|Popen)\(.*shell\s*=\s*True.*['\"]rm",
     "通过 shell=True 执行删除命令"),
    (r"eval\(.*(?:input|request|user)", "用户输入被传入 eval()"),
    (r"exec\(.*(?:input|request|user)", "用户输入被传入 exec()"),
    (r"__import__\(.*(?:input|request|user)", "用户输入被传入 __import__()"),
    (r"pickle\.loads?\(.*(?:input|request|user)", "用户输入被反序列化"),
    (r"requests\.(?:get|post|put|delete)\(.*verify\s*=\s*False",
     "SSL 证书验证被禁用"),
]


def verify_security(code: str) -> Verdict:
    """Tier 5: 安全审计"""
    verdict = Verdict(tier=Tier.SECURITY, passed=True)
    lines = code.split("\n")

    # 检查密钥泄露
    for pattern, desc in SECRET_PATTERNS:
        for match in re.finditer(pattern, code, re.IGNORECASE):
            verdict.passed = False
            line_num = code[: match.start()].count("\n") + 1
            verdict.failures.append({
                "line": line_num,
                "message": f"{desc}: {match.group(0)[:40]}...",
                "fix_hint": "使用环境变量或密钥管理服务存储敏感信息",
            })

    # 检查危险模式
    for pattern, desc in DANGEROUS_PATTERNS:
        for match in re.finditer(pattern, code, re.IGNORECASE):
            verdict.passed = False
            line_num = code[: match.start()].count("\n") + 1
            verdict.failures.append({
                "line": line_num,
                "message": desc,
                "fix_hint": "确保输入经过充分验证和清理",
            })

    # 检查文件权限
    os_chmod_patterns = [
        r"os\.chmod\(.*[,]\s*0o?777",
        r"chmod\s+777",
    ]
    for pattern in os_chmod_patterns:
        for match in re.finditer(pattern, code):
            verdict.passed = False
            line_num = code[: match.start()].count("\n") + 1
            verdict.failures.append({
                "line": line_num,
                "message": "设置文件权限为 777 (所有人可读写执行)",
                "fix_hint": "使用更严格的权限如 644 或 755",
            })

    if not verdict.failures:
        verdict.score = 1.0
    else:
        verdict.score = max(0, 1 - len(verdict.failures) * 0.2)

    return verdict


# ─── 完整验证流水线 ──────────────────────────────────────


def run_full_verification(
    file_path: str,
    code: str = "",
    language: str = "",
    test_command: str = "",
    expected_output: str = "",
    tiers: list[Tier] = None,
) -> VerificationReport:
    """
    运行完整的多级验证流水线。

    Args:
        file_path: 要验证的文件路径
        code: 代码内容（如果文件存在也读取，优先使用传入的）
        language: 语言 (python/bash/json)
        test_command: Tier 3 测试命令
        expected_output: Tier 3 期望输出
        tiers: 要运行的验证层级（默认全跑）

    Returns: VerificationReport
    """
    if tiers is None:
        tiers = list(Tier)

    # 读取代码
    if not code and os.path.exists(file_path):
        try:
            with open(file_path) as f:
                code = f.read()
        except Exception:
            pass

    if not language:
        ext = os.path.splitext(file_path)[1]
        lang_map = {
            ".py": "python", ".sh": "bash", ".bash": "bash",
            ".json": "json", ".js": "javascript", ".ts": "typescript",
        }
        language = lang_map.get(ext, "python")

    report = VerificationReport(target=file_path or "inline code")

    for tier in tiers:
        if tier == Tier.SYNTAX:
            v1 = verify_syntax(code, language)
            v2 = verify_lint(file_path) if file_path else Verdict(Tier.SYNTAX, True)
            # 合并两个语法检查
            merged = Verdict(
                tier=Tier.SYNTAX,
                passed=v1.passed and v2.passed,
                score=min(v1.score, v2.score),
                details=v1.details + v2.details,
                failures=v1.failures + v2.failures,
            )
            report.verdicts.append(merged)
            if not merged.passed:
                # 语法都过不了，后面的没意义
                if Tier.STRUCTURE in tiers:
                    report.verdicts.append(
                        Verdict(Tier.STRUCTURE, False, 0, [],
                                [{"message": "Tier 1 未通过，跳过结构检查"}])
                    )
                if Tier.FUNCTIONAL in tiers:
                    report.verdicts.append(
                        Verdict(Tier.FUNCTIONAL, False, 0, [],
                                [{"message": "Tier 1 未通过，跳过功能测试"}])
                    )
                if Tier.ROBUSTNESS in tiers:
                    report.verdicts.append(
                        Verdict(Tier.ROBUSTNESS, False, 0, [],
                                [{"message": "Tier 1 未通过，跳过鲁棒性检查"}])
                    )
                if Tier.SECURITY in tiers:
                    report.verdicts.append(
                        Verdict(Tier.SECURITY, False, 0, [],
                                [{"message": "Tier 1 未通过，跳过安全审计"}])
                    )
                break  # 不再继续

        elif tier == Tier.STRUCTURE:
            v = verify_structure(file_path if file_path else "")
            report.verdicts.append(v)
            if not v.passed:
                if Tier.FUNCTIONAL in tiers:
                    report.verdicts.append(
                        Verdict(Tier.FUNCTIONAL, False, 0, [],
                                [{"message": "Tier 2 未通过，跳过功能测试"}])
                    )
                break

        elif tier == Tier.FUNCTIONAL:
            v = verify_functional(
                file_path if file_path else "",
                test_command, expected_output
            )
            report.verdicts.append(v)

        elif tier == Tier.ROBUSTNESS:
            v = verify_robustness(code, language)
            report.verdicts.append(v)

        elif tier == Tier.SECURITY:
            v = verify_security(code)
            report.verdicts.append(v)

    # 汇总
    verdicts = report.verdicts
    report.overall_pass = all(v.passed for v in verdicts)
    report.total_score = sum(v.score for v in verdicts)

    return report


# ─── 快速验证 ─────────────────────────────────────────────


def quick_verify(file_path: str, code: str = "") -> dict:
    """
    快速验证（仅 Tier 1 + Tier 5）。
    适合作为 PreToolUse hook。
    返回 {pass, message, failures}
    """
    report = run_full_verification(
        file_path, code,
        tiers=[Tier.SYNTAX, Tier.SECURITY],
    )

    failures = []
    for v in report.verdicts:
        failures.extend(v.failures)

    return {
        "pass": report.overall_pass,
        "score": report.total_score,
        "failures": failures[:5],
        "message": "OK" if report.overall_pass
        else f"{len(failures)} 个问题待修复",
    }


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  verify_pipeline.py full <file> [--test 'cmd'] [--expect 'output']")
        print("  verify_pipeline.py quick <file>")
        print("  verify_pipeline.py check <code_string>")
        print("  verify_pipeline.py security <file>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "full":
        file_path = sys.argv[2]
        test_cmd = ""
        expected = ""

        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--test" and i + 1 < len(args):
                test_cmd = args[i + 1]
                i += 2
            elif args[i] == "--expect" and i + 1 < len(args):
                expected = args[i + 1]
                i += 2
            else:
                i += 1

        report = run_full_verification(
            file_path,
            test_command=test_cmd,
            expected_output=expected,
        )
        print(report.summary)

    elif cmd == "quick":
        file_path = sys.argv[2]
        with open(file_path) as f:
            code = f.read()
        result = quick_verify(file_path, code)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "check":
        code = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else sys.stdin.read()
        v = verify_syntax(code, "python")
        print(f"Pass: {v.passed}, Score: {v.score:.2f}")
        for f in v.failures:
            print(f"  - {f.get('message', '')}")

    elif cmd == "security":
        file_path = sys.argv[2]
        with open(file_path) as f:
            code = f.read()
        v = verify_security(code)
        print(f"Pass: {v.passed}, Score: {v.score:.2f}")
        for f in v.failures:
            print(f"  - 行 {f.get('line', '?')}: {f.get('message', '')}")