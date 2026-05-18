#!/usr/bin/env python3
"""
Self-Evolving Error Correction Loop
对标 STILL-ALIVE / Reflexion

Records errors, matches similar past errors, suggests corrections.
Stores patterns in ~/.hermes/error_correction/patterns.json
and full event log in ~/.hermes/logs/error_correction.jsonl

Usage:
  python3 error_correction.py record --task "..." --error "..." --attempted "..."
  python3 error_correction.py record --task "..." --success --approach "..."
  python3 error_correction.py match --task "..."
  python3 error_correction.py inject --task "..."
  python3 error_correction.py stats
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ── Configuration ────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
PATTERNS_DIR = HERMES_HOME / "error_correction"
PATTERNS_FILE = PATTERNS_DIR / "patterns.json"
LOGS_DIR = HERMES_HOME / "logs"
LOGS_FILE = LOGS_DIR / "error_correction.jsonl"

SIMILARITY_THRESHOLD = 0.15   # minimum Jaccard + TF overlap to consider a match
MAX_MATCHES = 5               # max similar patterns to return
TOP_KEYWORDS = 20             # how many keywords to store per pattern

# Common English stop words for keyword extraction
STOP_WORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "and", "but", "or", "if", "while",
    "that", "this", "it", "its", "i", "me", "my", "we", "our", "you",
    "your", "he", "she", "they", "them", "their", "what", "which", "who",
    "whom", "just", "up", "down", "also", "about", "any", "these", "those",
    "using", "use", "used", "need", "like", "get", "got", "make", "made",
    "want", "try", "trying", "tried", "one", "two", "see", "know",
    "file", "files", "code", "function", "error", "errors", "task",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    """Create storage directories if they don't exist."""
    PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_patterns() -> dict:
    """Load patterns.json, returning empty dict if missing or corrupt."""
    if not PATTERNS_FILE.exists():
        return {}
    try:
        return json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_patterns(data: dict) -> None:
    """Atomically write patterns.json."""
    ensure_dirs()
    tmp = PATTERNS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATTERNS_FILE)


def append_log(entry: dict) -> None:
    """Append one JSON line to the event log."""
    ensure_dirs()
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(LOGS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def task_hash(task_text: str) -> str:
    """Deterministic hash of normalized task text for dedup."""
    norm = re.sub(r"\s+", " ", task_text.strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def extract_keywords(text: str, top_n: int = TOP_KEYWORDS) -> list[tuple[str, int]]:
    """
    Tokenize text, remove stop-words, return top-N (keyword, frequency) pairs.
    """
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    filtered = [t for t in tokens if t not in STOP_WORDS]
    counter = Counter(filtered)
    return counter.most_common(top_n)


def keyword_set(text: str, top_n: int = TOP_KEYWORDS) -> set[str]:
    """Return the set of top keywords for a piece of text (no frequencies)."""
    return {kw for kw, _ in extract_keywords(text, top_n)}


def tf_keyword_map(text: str, top_n: int = TOP_KEYWORDS) -> dict[str, float]:
    """
    Return {keyword: normalized_tf} for the text.
    Normalized so values sum to 1.
    """
    kws = extract_keywords(text, top_n)
    total = sum(freq for _, freq in kws) or 1
    return {kw: freq / total for kw, freq in kws}


def similarity(a_kws: dict[str, float], b_kws: set[str]) -> float:
    """
    Compute weighted overlap score between query TF map and stored keyword set.

    Score = sum(query_tf for kw in intersection) / sum(query_tf)
    Also factored with Jaccard: |intersection| / (|a| + |b| - |intersection|)
    """
    a_set = set(a_kws.keys())
    b_set = b_kws

    if not a_set or not b_set:
        return 0.0

    inter = a_set & b_set
    if not inter:
        return 0.0

    # Weighted overlap: how much of query's TF mass is covered by stored keywords
    weighted = sum(a_kws[kw] for kw in inter) / sum(a_kws.values())

    # Jaccard bonus for set overlap
    jaccard = len(inter) / len(a_set | b_set)

    # Blend: 60% weighted TF + 40% Jaccard
    return 0.6 * weighted + 0.4 * jaccard


def match_task(query_task: str, patterns: dict) -> list[dict]:
    """
    Search patterns for entries similar to query_task.

    Returns list of match dicts:
      {task, hash, score, errors: [...], keyword_overlap: [...]}
    sorted by score descending.
    """
    q_kws = tf_keyword_map(query_task)

    matches: list[dict] = []
    for pid, entry in patterns.items():
        stored_kws = set(entry.get("keywords", []))
        score = similarity(q_kws, stored_kws)
        if score >= SIMILARITY_THRESHOLD:
            matches.append({
                "task": entry["task"],
                "hash": pid,
                "score": round(score, 4),
                "errors": entry.get("errors", []),
                "keyword_overlap": sorted(set(q_kws) & stored_kws),
            })

    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:MAX_MATCHES]


# ── Commands ─────────────────────────────────────────────────────────────────

def record_failure(task: str, error: str, attempted: str) -> dict:
    """
    Record a task failure: store the error + attempted approach.
    If this exact task hash already exists, append a new error entry.
    """
    tid = task_hash(task)
    patterns = load_patterns()
    keywords = [kw for kw, _ in extract_keywords(task)]

    if tid not in patterns:
        patterns[tid] = {
            "task": task,
            "keywords": keywords,
            "errors": [],
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }

    error_entry = {
        "error_text": error,
        "attempted_approach": attempted,
        "corrections": [],   # will be populated by record_success
        "failure_count": 1,
        "first_failure": datetime.now(timezone.utc).isoformat(),
        "last_failure": datetime.now(timezone.utc).isoformat(),
    }

    # Check if similar error already recorded for this task
    existing = None
    for e in patterns[tid]["errors"]:
        if e["error_text"] == error and e["attempted_approach"] == attempted:
            existing = e
            break

    if existing:
        existing["failure_count"] += 1
        existing["last_failure"] = datetime.now(timezone.utc).isoformat()
    else:
        patterns[tid]["errors"].append(error_entry)

    # Update keywords (merge new ones)
    stored_kw_set = set(patterns[tid]["keywords"])
    for kw, _ in extract_keywords(task, top_n=TOP_KEYWORDS * 2):
        if kw not in stored_kw_set and len(patterns[tid]["keywords"]) < TOP_KEYWORDS * 2:
            patterns[tid]["keywords"].append(kw)

    patterns[tid]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_patterns(patterns)

    log_entry = {
        "event": "failure",
        "task_hash": tid,
        "task": task,
        "error": error,
        "attempted": attempted,
    }
    append_log(log_entry)

    return {"status": "recorded", "task_hash": tid, "action": "failure"}


def record_success(task: str, approach: str) -> dict:
    """
    Record a successful approach for a task.
    Links to the most recent failure entry for this task hash.
    """
    tid = task_hash(task)
    patterns = load_patterns()

    if tid not in patterns or not patterns[tid].get("errors"):
        # No prior failure — record as a standalone success
        if tid not in patterns:
            patterns[tid] = {
                "task": task,
                "keywords": [kw for kw, _ in extract_keywords(task)],
                "errors": [],
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
        # Add a success-only error entry
        error_entry = {
            "error_text": "(no prior failure)",
            "attempted_approach": "n/a",
            "corrections": [{
                "approach": approach,
                "success_count": 1,
                "last_success": datetime.now(timezone.utc).isoformat(),
            }],
            "failure_count": 0,
            "first_failure": datetime.now(timezone.utc).isoformat(),
            "last_failure": datetime.now(timezone.utc).isoformat(),
        }
        patterns[tid]["errors"].append(error_entry)
    else:
        # Link to the most recently failed error entry
        latest_error = max(
            patterns[tid]["errors"],
            key=lambda e: e.get("last_failure", e.get("first_failure", ""))
        )
        # Check if this approach already recorded
        for corr in latest_error.get("corrections", []):
            if corr["approach"] == approach:
                corr["success_count"] += 1
                corr["last_success"] = datetime.now(timezone.utc).isoformat()
                break
        else:
            latest_error.setdefault("corrections", []).append({
                "approach": approach,
                "success_count": 1,
                "last_success": datetime.now(timezone.utc).isoformat(),
            })

    patterns[tid]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_patterns(patterns)

    log_entry = {
        "event": "success",
        "task_hash": tid,
        "task": task,
        "approach": approach,
    }
    append_log(log_entry)

    return {"status": "recorded", "task_hash": tid, "action": "success"}


def cmd_record(args: argparse.Namespace) -> None:
    """Handle the 'record' subcommand."""
    if args.success:
        result = record_success(task=args.task, approach=args.approach)
    else:
        if not args.error or not args.attempted:
            print("ERROR: --error and --attempted are required for failure recording", file=sys.stderr)
            sys.exit(1)
        result = record_failure(task=args.task, error=args.error, attempted=args.attempted)

    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_match(args: argparse.Namespace) -> None:
    """Handle the 'match' subcommand."""
    patterns = load_patterns()
    if not patterns:
        print(json.dumps({"matches": [], "message": "No patterns stored yet"}, ensure_ascii=False, indent=2))
        return

    matches = match_task(args.task, patterns)

    output = {
        "query": args.task,
        "matches": [],
    }

    for m in matches:
        corrections = []
        for err in m["errors"]:
            for corr in err.get("corrections", []):
                corrections.append({
                    "error": err["error_text"],
                    "attempted": err["attempted_approach"],
                    "correction": corr["approach"],
                    "success_count": corr["success_count"],
                    "last_success": corr["last_success"],
                })

        output["matches"].append({
            "task": m["task"],
            "score": m["score"],
            "keyword_overlap": m["keyword_overlap"],
            "corrections": corrections,
        })

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_inject(args: argparse.Namespace) -> None:
    """
    Generate an injection prompt with relevant past lessons for the given task.
    Reads task from --task argument.
    """
    patterns = load_patterns()
    if not patterns:
        print("")  # No lessons
        return

    matches = match_task(args.task, patterns)
    if not matches:
        print("")
        return

    lines = ["## 🔄 历史纠错提示"]
    seen = set()

    for m in matches:
        for err in m["errors"]:
            for corr in err.get("corrections", []):
                dedup_key = (m["task"], err["error_text"], corr["approach"])
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Truncate long texts for readability
                task_short = m["task"][:100] + ("..." if len(m["task"]) > 100 else "")
                error_short = err["error_text"][:120] + ("..." if len(err["error_text"]) > 120 else "")

                lines.append(f"⚠️ 类似任务 '{task_short}' 曾因 '{error_short}' 失败。")
                lines.append(f"✅ 成功方案: '{corr['approach']}'")
                lines.append(f"💡 建议: 先用 {corr['approach']} 方案尝试")
                lines.append("")

    # If only failures without corrections exist, still warn
    for m in matches:
        for err in m["errors"]:
            if not err.get("corrections") and err["error_text"] != "(no prior failure)":
                dedup_key = (m["task"], err["error_text"], "WARN_ONLY")
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                task_short = m["task"][:100] + ("..." if len(m["task"]) > 100 else "")
                error_short = err["error_text"][:120] + ("..." if len(err["error_text"]) > 120 else "")
                lines.append(f"⚠️ 类似任务 '{task_short}' 曾因 '{error_short}' 失败（尚无成功方案）。")
                lines.append(f"💡 建议: 避免使用 {err['attempted_approach']}")
                lines.append("")

    print("\n".join(lines).strip())


def cmd_stats(args: argparse.Namespace) -> None:
    """Show learning statistics."""
    patterns = load_patterns()
    log_entries: list[dict] = []
    if LOGS_FILE.exists():
        try:
            for line in LOGS_FILE.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    log_entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            pass

    total_tasks = len(patterns)
    total_failures = sum(
        sum(e.get("failure_count", 0) for e in entry.get("errors", []))
        for entry in patterns.values()
    )
    total_successes = sum(
        sum(
            sum(c.get("success_count", 0) for c in err.get("corrections", []))
            for err in entry.get("errors", [])
        )
        for entry in patterns.values()
    )
    total_errors_with_corrections = sum(
        1 for entry in patterns.values()
        for err in entry.get("errors", [])
        if err.get("corrections")
    )
    total_errors_without_corrections = sum(
        1 for entry in patterns.values()
        for err in entry.get("errors", [])
        if not err.get("corrections")
    )

    # Top corrected approaches
    approach_stats: dict[str, int] = defaultdict(int)
    for entry in patterns.values():
        for err in entry.get("errors", []):
            for corr in err.get("corrections", []):
                approach_stats[corr["approach"]] += corr.get("success_count", 0)

    top_approaches = sorted(approach_stats.items(), key=lambda x: x[1], reverse=True)[:10]

    # Log event counts
    log_events = Counter(e.get("event", "unknown") for e in log_entries)

    stats = {
        "patterns": {
            "total_tasks": total_tasks,
            "total_failures_recorded": total_failures,
            "total_successes_recorded": total_successes,
            "errors_with_corrections": total_errors_with_corrections,
            "errors_without_corrections": total_errors_without_corrections,
            "correction_rate": (
                round(total_errors_with_corrections / max(total_errors_with_corrections + total_errors_without_corrections, 1), 4)
            ),
        },
        "log_events": dict(log_events),
        "top_corrected_approaches": [
            {"approach": app, "total_successes": cnt}
            for app, cnt in top_approaches
        ],
        "storage": {
            "patterns_file": str(PATTERNS_FILE),
            "patterns_size_bytes": PATTERNS_FILE.stat().st_size if PATTERNS_FILE.exists() else 0,
            "logs_file": str(LOGS_FILE),
            "logs_size_bytes": LOGS_FILE.stat().st_size if LOGS_FILE.exists() else 0,
            "logs_entry_count": len(log_entries),
        },
    }

    print(json.dumps(stats, ensure_ascii=False, indent=2))


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Self-Evolving Error Correction Loop — records, matches, and learns from task errors.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # record
    rec = sub.add_parser("record", help="Record a failure or success")
    rec.add_argument("--task", required=True, help="Task description")
    rec.add_argument("--success", action="store_true", help="Record as success (requires --approach)")
    rec.add_argument("--error", help="Error text (for failures)")
    rec.add_argument("--attempted", help="Attempted approach (for failures)")
    rec.add_argument("--approach", help="Successful approach (for successes)")

    # match
    mat = sub.add_parser("match", help="Find similar past errors for a task")
    mat.add_argument("--task", required=True, help="Task description to match against")

    # inject
    inj = sub.add_parser("inject", help="Generate injection prompt with past lessons")
    inj.add_argument("--task", required=True, help="Task to generate injection prompt for")

    # stats
    sub.add_parser("stats", help="Show learning statistics")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "record": cmd_record,
        "match": cmd_match,
        "inject": cmd_inject,
        "stats": cmd_stats,
    }

    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()