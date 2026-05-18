#!/usr/bin/env python3
"""
execution_tracer.py — 执行轨迹树 + 失败诊断
对标 CodeTracer 论文（南大+快手）

核心功能：
  1. 轨迹节点记录：每次工具调用记录为节点
  2. 区分 EXPLORE（只读）vs MUTATE（状态变更）步骤
  3. 层级轨迹树：构建父子关系树结构
  4. 失败诊断：定位 Failure-Responsible Stage，输出证据集
  5. 反思回放：生成可注入的 prompt 片段

存储：~/.hermes/traces/{session_id}.jsonl（日志行）+ trace_tree.json（树结构）

技术约束：纯 Python 标准库，支持 subprocess 调用
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
TRACES_DIR = Path.home() / ".hermes" / "traces"

# EXPLORE tools: read-only, no side effects
EXPLORE_TOOLS = {
    "read", "search", "ls", "web_search", "web_extract",
    "grep", "find", "cat", "head", "tail", "git_log",
    "git_diff", "git_status", "git_show", "git_branch",
    "list_files", "read_file", "search_files",
    "read_lints", "glob", "view", "open",
    "vision_analyze", "diff_sandbox_review", "diff_sandbox_diff",
    "process_list", "process_log", "process_poll",
    "provider_router_status", "provider_router_route",
}

# MUTATE tools: state-changing, write side effects
MUTATE_TOOLS = {
    "write", "patch", "terminal-write", "rm", "mv", "cp",
    "git_add", "git_commit", "git_push", "git_checkout",
    "git_merge", "git_reset", "git_tag",
    "write_file", "patch_file",
    "terminal", "bash", "exec",
    "process_kill", "process_write", "process_submit", "process_close",
    "agent_factory_create", "agent_factory_delete",
    "hooks_manage_install", "hooks_manage_remove",
    "diff_sandbox_rollback", "diff_sandbox_cleanup",
}

# ──────────────────────────────────────────────────────────────────────
# Data Classes
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TraceNode:
    """单个轨迹节点，记录一次工具调用"""
    id: str
    tool: str
    action: str          # "EXPLORE" or "MUTATE"
    is_mutate: bool
    timestamp: float
    parent_id: Optional[str] = None
    summary: str = ""
    input_params: str = ""    # JSON string of input parameters
    output_result: str = ""   # JSON string or truncated output
    result_success: bool = True
    error: str = ""
    exit_code: int = 0
    depth: int = 0            # populated by build-tree
    children: list = field(default_factory=list)  # populated by build-tree

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TraceNode":
        return cls(**{k: d.get(k, None) for k in [
            "id", "tool", "action", "is_mutate", "timestamp",
            "parent_id", "summary", "input_params", "output_result",
            "result_success", "error", "exit_code", "depth", "children"
        ] if k in d})


@dataclass
class DiagnosisResult:
    """失败诊断结果"""
    failure_stage: Optional[str] = None       # 节点ID
    error_chain: list = field(default_factory=list)   # [(node_id, error), ...]
    evidence: list = field(default_factory=list)       # [evidence_str, ...]
    diagnosis: str = ""                        # 一句话诊断
    mutating_nodes: int = 0
    total_nodes: int = 0
    failed_nodes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def classify_tool(tool_name: str) -> tuple[str, bool]:
    """Classify a tool as EXPLORE or MUTATE. Returns (action, is_mutate)."""
    tool_lower = tool_name.lower().replace("_", "").replace("-", "")

    # Check MUTATE first (more specific patterns)
    for mt in MUTATE_TOOLS:
        mt_clean = mt.lower().replace("_", "").replace("-", "")
        if mt_clean in tool_lower:
            return ("MUTATE", True)

    # Check EXPLORE
    for et in EXPLORE_TOOLS:
        et_clean = et.lower().replace("_", "").replace("-", "")
        if et_clean in tool_lower:
            return ("EXPLORE", False)

    # Heuristic fallback: common write patterns
    write_patterns = ["write", "create", "delete", "remove", "kill", "patch",
                      "commit", "push", "install", "uninstall", "rollback"]
    for wp in write_patterns:
        if wp in tool_lower:
            return ("MUTATE", True)

    # Default: assume EXPLORE (safe)
    return ("EXPLORE", False)


def generate_node_id(tool: str, timestamp: float, input_params: str) -> str:
    """Generate a unique node ID from tool + timestamp + params hash."""
    raw = f"{tool}:{timestamp}:{input_params}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def ensure_traces_dir() -> Path:
    """Ensure the traces directory exists."""
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    return TRACES_DIR


def get_session_path(session_id: str) -> Path:
    """Get the JSONL path for a session."""
    ensure_traces_dir()
    return TRACES_DIR / f"{session_id}.jsonl"


def get_tree_path(session_id: str) -> Path:
    """Get the tree JSON path for a session."""
    ensure_traces_dir()
    return TRACES_DIR / f"{session_id}.trace_tree.json"


def get_diagnosis_path(session_id: str) -> Path:
    """Get the diagnosis JSON path for a session."""
    ensure_traces_dir()
    return TRACES_DIR / f"{session_id}.diagnosis.json"


def load_nodes(session_id: str) -> list[TraceNode]:
    """Load all trace nodes from a session's JSONL file."""
    path = get_session_path(session_id)
    if not path.exists():
        print(f"No trace file found: {path}", file=sys.stderr)
        return []

    nodes = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                nodes.append(TraceNode.from_dict(d))
            except json.JSONDecodeError as e:
                print(f"Skipping malformed line: {e}", file=sys.stderr)
    return nodes


def save_nodes(session_id: str, nodes: list[TraceNode]):
    """Save nodes as JSONL to the session file."""
    path = get_session_path(session_id)
    with open(path, "w") as f:
        for node in nodes:
            f.write(json.dumps(node.to_dict(), ensure_ascii=False) + "\n")


def truncate_str(s: str, max_len: int = 500) -> str:
    """Truncate a string for summary display."""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"...[truncated, total {len(s)} chars]"


# ──────────────────────────────────────────────────────────────────────
# Command: record
# ──────────────────────────────────────────────────────────────────────

def cmd_record(args):
    """Record a single trace node."""
    tool = args.tool
    action, is_mutate = classify_tool(tool)
    if args.action_override:
        action = args.action_override.upper()
        is_mutate = action == "MUTATE"
    if args.is_mutate is not None:
        is_mutate = args.is_mutate.lower() in ("true", "1", "yes")
        action = "MUTATE" if is_mutate else "EXPLORE"

    timestamp = time.time()
    node_id = generate_node_id(tool, timestamp, args.input)

    # Parse success
    success = True
    if args.success is not None:
        success = args.success.lower() in ("true", "1", "yes")

    # Build summary
    input_short = truncate_str(args.input, 200)
    output_short = truncate_str(args.output, 200)
    summary = f"[{action}] {tool}: {input_short} → {output_short}"

    node = TraceNode(
        id=node_id,
        tool=tool,
        action=action,
        is_mutate=is_mutate,
        timestamp=timestamp,
        parent_id=args.parent_id or None,
        summary=summary,
        input_params=args.input,
        output_result=args.output,
        result_success=success,
        error=args.error or "",
        exit_code=int(args.exit_code) if args.exit_code else 0,
    )

    # Append to JSONL
    path = get_session_path(args.session_id)
    with open(path, "a") as f:
        f.write(json.dumps(node.to_dict(), ensure_ascii=False) + "\n")

    output = {
        "status": "recorded",
        "node_id": node_id,
        "session_id": args.session_id,
        "tool": tool,
        "action": action,
        "is_mutate": is_mutate,
    }
    print(json.dumps(output, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────────
# Command: build-tree
# ──────────────────────────────────────────────────────────────────────

def build_trace_tree(nodes: list[TraceNode]) -> list[dict]:
    """
    Build a hierarchical trace tree from flat nodes.
    Uses parent_id to establish relationships.
    Nodes without parent_id become roots.
    """
    if not nodes:
        return []

    # Index nodes by id
    node_map: dict[str, TraceNode] = {n.id: n for n in nodes}

    # Calculate depth via BFS from roots
    roots: list[TraceNode] = []
    children_map: dict[str, list[TraceNode]] = {n.id: [] for n in nodes}

    for node in nodes:
        if node.parent_id and node.parent_id in node_map:
            children_map[node.parent_id].append(node)
        else:
            roots.append(node)
            node.parent_id = None  # normalize dangling refs

    # BFS to assign depths
    visited = set()
    queue = deque()
    for root in roots:
        root.depth = 0
        queue.append(root)
        visited.add(root.id)

    # Also handle disconnected nodes (no parent, not in roots via missing parent_id)
    for node in nodes:
        if node.id not in visited:
            node.depth = 0
            node.parent_id = None
            roots.append(node)
            queue.append(node)
            visited.add(node.id)

    while queue:
        current = queue.popleft()
        for child in children_map.get(current.id, []):
            if child.id not in visited:
                child.depth = current.depth + 1
                visited.add(child.id)
                queue.append(child)

    # Build tree dicts
    def node_to_tree(n: TraceNode) -> dict:
        d = n.to_dict()
        d["children"] = [node_to_tree(c) for c in children_map.get(n.id, [])]
        # Remove flat-list fields from tree nodes for cleanliness
        d.pop("parent_id", None)
        return d

    return [node_to_tree(r) for r in roots]


def cmd_build_tree(args):
    """Build the hierarchical trace tree."""
    nodes = load_nodes(args.session_id)
    if not nodes:
        print(json.dumps({"status": "empty", "session_id": args.session_id, "tree": []}))
        return

    tree = build_trace_tree(nodes)

    # Save tree
    tree_path = get_tree_path(args.session_id)
    with open(tree_path, "w") as f:
        json.dump({"session_id": args.session_id, "tree": tree, "node_count": len(nodes)}, f,
                  ensure_ascii=False, indent=2)

    # Save updated nodes with depth info
    save_nodes(args.session_id, nodes)

    output = {
        "status": "tree_built",
        "session_id": args.session_id,
        "node_count": len(nodes),
        "root_count": len(tree),
        "max_depth": max((n.depth for n in nodes), default=0),
        "tree_path": str(tree_path),
    }
    print(json.dumps(output, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────────
# Command: diagnose
# ──────────────────────────────────────────────────────────────────────

def diagnose_session(nodes: list[TraceNode]) -> DiagnosisResult:
    """
    Run failure diagnosis on a session's trace nodes.
    Heuristic-based, no LLM dependency.

    Algorithm:
      1. Scan all nodes for failure signals
      2. Identify the first MUTATE node that failed (Failure-Responsible Stage)
      3. Collect the error chain leading to that failure
      4. Gather evidence from preceding EXPLORE nodes that may have contributed
    """
    result = DiagnosisResult()
    result.total_nodes = len(nodes)

    if not nodes:
        result.diagnosis = "No trace nodes found — nothing to diagnose."
        return result

    # Sort by timestamp
    sorted_nodes = sorted(nodes, key=lambda n: n.timestamp)

    # Count categories
    result.mutating_nodes = sum(1 for n in nodes if n.is_mutate)
    result.failed_nodes = sum(1 for n in nodes if not n.result_success or n.error or n.exit_code != 0)
    failed_nodes = [n for n in sorted_nodes if not n.result_success or n.error or n.exit_code != 0]

    # Find the FIRST failed MUTATE node — this is the Failure-Responsible Stage
    first_failed_mutate = None
    for node in sorted_nodes:
        if node.is_mutate and (not node.result_success or node.error or node.exit_code != 0):
            first_failed_mutate = node
            break

    if first_failed_mutate:
        result.failure_stage = first_failed_mutate.id

        # Build error chain: all failed nodes up to and including the failure stage
        for node in sorted_nodes:
            if node == first_failed_mutate:
                result.error_chain.append({
                    "node_id": node.id,
                    "tool": node.tool,
                    "action": node.action,
                    "error": node.error or f"exit_code={node.exit_code}",
                    "success": node.result_success,
                })
                break
            if not node.result_success or node.error or node.exit_code != 0:
                result.error_chain.append({
                    "node_id": node.id,
                    "tool": node.tool,
                    "action": node.action,
                    "error": node.error or f"exit_code={node.exit_code}",
                    "success": node.result_success,
                })

        # Gather evidence: EXPLORE nodes immediately before the failure
        failure_idx = sorted_nodes.index(first_failed_mutate)
        evidence_start = max(0, failure_idx - 5)
        for node in sorted_nodes[evidence_start:failure_idx]:
            result.evidence.append(
                f"[{node.action}] {node.tool}: {truncate_str(node.output_result, 200)}"
            )

        # One-line diagnosis
        error_detail = first_failed_mutate.error or f"exit code {first_failed_mutate.exit_code}"
        result.diagnosis = (
            f"Failure traced to MUTATE node '{first_failed_mutate.id}' "
            f"({first_failed_mutate.tool}): {truncate_str(error_detail, 150)}. "
            f"{len(result.error_chain)} node(s) in error chain, "
            f"{len(result.evidence)} evidence item(s) collected."
        )

        # Additional evidence: summarize all preceding failed nodes
        if len(result.error_chain) > 1:
            result.diagnosis += f" Preceded by {len(result.error_chain) - 1} earlier failures."

    elif failed_nodes:
        # Only EXPLORE failures — less severe
        result.diagnosis = (
            f"No MUTATE failures detected, but {len(failed_nodes)} EXPLORE node(s) "
            f"reported errors. First: '{failed_nodes[0].id}' ({failed_nodes[0].tool}). "
            "These may indicate missing files, permissions, or network issues."
        )
        for fn in failed_nodes[:3]:
            result.error_chain.append({
                "node_id": fn.id,
                "tool": fn.tool,
                "action": fn.action,
                "error": fn.error or f"exit_code={fn.exit_code}",
            })
    else:
        result.diagnosis = (
            f"All {result.total_nodes} nodes completed successfully. "
            f"No failures detected."
        )

    return result


def cmd_diagnose(args):
    """Run failure diagnosis on a session."""
    nodes = load_nodes(args.session_id)
    if not nodes:
        result = DiagnosisResult()
        result.diagnosis = "No trace data available."
        output = result.to_dict()
        output["session_id"] = args.session_id
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    result = diagnose_session(nodes)

    # Save diagnosis
    diag_path = get_diagnosis_path(args.session_id)
    output = result.to_dict()
    output["session_id"] = args.session_id

    with open(diag_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(json.dumps(output, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────────────────────────────
# Command: replay-prompt
# ──────────────────────────────────────────────────────────────────────

def cmd_replay_prompt(args):
    """
    Generate a reflection/replay prompt fragment for injection into
    the next agent call. Includes diagnosis summary, evidence, and
    actionable suggestions.
    """
    nodes = load_nodes(args.session_id)
    tree_path = get_tree_path(args.session_id)
    diag_path = get_diagnosis_path(args.session_id)

    # Load existing diagnosis or run fresh
    if diag_path.exists():
        with open(diag_path) as f:
            diagnosis_data = json.load(f)
    else:
        result = diagnose_session(nodes)
        diagnosis_data = result.to_dict()

    if not nodes:
        print("No trace data available for replay prompt.")
        return

    # Build the prompt
    lines = []
    lines.append("## Execution Trace Replay")
    lines.append(f"Session: `{args.session_id}`")
    lines.append(f"Total steps: {len(nodes)}")
    lines.append("")

    # Diagnosis summary
    lines.append("### Failure Diagnosis")
    diag = diagnosis_data.get("diagnosis", "No diagnosis available.")
    lines.append(diag)
    lines.append("")

    if diagnosis_data.get("failure_stage"):
        lines.append(f"**Failure-Responsible Stage**: `{diagnosis_data['failure_stage']}`")
        lines.append("")

    # Error chain
    error_chain = diagnosis_data.get("error_chain", [])
    if error_chain:
        lines.append("### Error Chain")
        for i, err in enumerate(error_chain, 1):
            lines.append(f"{i}. `{err.get('node_id', '?')}` — {err.get('tool', '?')} "
                         f"[{err.get('action', '?')}]: {err.get('error', '?')}")
        lines.append("")

    # Evidence
    evidence = diagnosis_data.get("evidence", [])
    if evidence:
        lines.append("### Evidence")
        for i, ev in enumerate(evidence, 1):
            lines.append(f"{i}. {ev}")
        lines.append("")

    # Suggestions
    lines.append("### Suggestions")
    if diagnosis_data.get("failure_stage"):
        lines.append("- Review the Failure-Responsible Stage: check input parameters and environment state.")
        lines.append("- Consider adding a verification step before the failing MUTATE operation.")
        lines.append("- Review preceding EXPLORE steps for misleading results.")
    lines.append("- Use `build-tree` to see the full hierarchical trace for deeper analysis.")
    lines.append("")

    prompt_text = "\n".join(lines)

    # Output: print directly so it can be captured
    print(prompt_text)

    # Also save to file for reference
    replay_path = TRACES_DIR / f"{args.session_id}.replay_prompt.txt"
    with open(replay_path, "w") as f:
        f.write(prompt_text)


# ──────────────────────────────────────────────────────────────────────
# Command: list-sessions
# ──────────────────────────────────────────────────────────────────────

def cmd_list_sessions(args):
    """List all recorded sessions."""
    ensure_traces_dir()
    sessions = []
    for f in sorted(TRACES_DIR.glob("*.jsonl")):
        session_id = f.stem
        # Count nodes
        node_count = 0
        with open(f) as fh:
            node_count = sum(1 for _ in fh)
        sessions.append({
            "session_id": session_id,
            "node_count": node_count,
            "file": str(f),
        })

    output = {
        "sessions": sessions,
        "total": len(sessions),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────────────────────────────
# Command: status
# ──────────────────────────────────────────────────────────────────────

def cmd_status(args):
    """Show comprehensive status for a session: nodes, tree, diagnosis."""
    nodes = load_nodes(args.session_id)
    tree_path = get_tree_path(args.session_id)
    diag_path = get_diagnosis_path(args.session_id)

    explore_count = sum(1 for n in nodes if n.action == "EXPLORE")
    mutate_count = sum(1 for n in nodes if n.action == "MUTATE")
    failed = sum(1 for n in nodes if not n.result_success or n.error or n.exit_code != 0)

    status = {
        "session_id": args.session_id,
        "node_count": len(nodes),
        "explore_steps": explore_count,
        "mutate_steps": mutate_count,
        "failed_steps": failed,
        "has_tree": tree_path.exists(),
        "has_diagnosis": diag_path.exists(),
        "trace_file": str(get_session_path(args.session_id)),
    }

    if diag_path.exists():
        with open(diag_path) as f:
            diag = json.load(f)
        status["diagnosis"] = diag.get("diagnosis", "")
        status["failure_stage"] = diag.get("failure_stage")

    print(json.dumps(status, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Execution Tracer — 执行轨迹树 + 失败诊断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Record a step
  python3 execution_tracer.py record --session-id demo --tool read_file --input '{"path":"foo.txt"}' --output 'line 1...'

  # Build tree
  python3 execution_tracer.py build-tree --session-id demo

  # Diagnose failures
  python3 execution_tracer.py diagnose --session-id demo

  # Generate replay prompt
  python3 execution_tracer.py replay-prompt --session-id demo

  # List all sessions
  python3 execution_tracer.py list-sessions

  # Show session status
  python3 execution_tracer.py status --session-id demo
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # record
    p_record = subparsers.add_parser("record", help="Record a trace node")
    p_record.add_argument("--session-id", required=True, help="Session identifier")
    p_record.add_argument("--tool", required=True, help="Tool name (e.g., write_file)")
    p_record.add_argument("--input", default="", help="Input parameters (JSON string)")
    p_record.add_argument("--output", default="", help="Output result (JSON string or text)")
    p_record.add_argument("--is-mutate", default=None, help="Force mutate classification (true/false)")
    p_record.add_argument("--action-override", default=None, help="Override action: EXPLORE or MUTATE")
    p_record.add_argument("--success", default=None, help="Whether the call succeeded (true/false)")
    p_record.add_argument("--error", default="", help="Error message if failed")
    p_record.add_argument("--exit-code", default="0", help="Exit code (default: 0)")
    p_record.add_argument("--parent-id", default=None, help="Parent node ID for tree building")

    # build-tree
    p_tree = subparsers.add_parser("build-tree", help="Build hierarchical trace tree")
    p_tree.add_argument("--session-id", required=True, help="Session identifier")

    # diagnose
    p_diag = subparsers.add_parser("diagnose", help="Run failure diagnosis")
    p_diag.add_argument("--session-id", required=True, help="Session identifier")

    # replay-prompt
    p_replay = subparsers.add_parser("replay-prompt", help="Generate replay/reflection prompt")
    p_replay.add_argument("--session-id", required=True, help="Session identifier")

    # list-sessions
    p_list = subparsers.add_parser("list-sessions", help="List all recorded sessions")

    # status
    p_status = subparsers.add_parser("status", help="Show session status summary")
    p_status.add_argument("--session-id", required=True, help="Session identifier")

    args = parser.parse_args()

    if args.command == "record":
        cmd_record(args)
    elif args.command == "build-tree":
        cmd_build_tree(args)
    elif args.command == "diagnose":
        cmd_diagnose(args)
    elif args.command == "replay-prompt":
        cmd_replay_prompt(args)
    elif args.command == "list-sessions":
        cmd_list_sessions(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()