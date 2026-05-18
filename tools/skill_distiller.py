#!/usr/bin/env python3
"""
Skill Distillation Flywheel — 对标 LangMem procedural memory + EverOS Cases→Skills

核心机制:
  1. 批量聚合: 收集 N 个同类型 session，跨会话蒸馏最佳实践
  2. 版本化: 新蒸馏生成 <skill>-v2，保留旧版本用于 A/B 对比
  3. 反馈闭环: 追踪 skill 使用成功率，低分自动回滚

对标:
  - LangMem procedural memory: "agent 持续从情景中蒸馏可执行技能"
  - EverOS Cases→Skills 蒸馏管线
  - Mem0 procedural memory 概念

数据流:
  情景记忆(做了什么) → 收集 → 批量蒸馏 → 程序性记忆(skill) → 下次直接调用
                                              ↓
                                        执行反馈 → 评分 → 高分保留/低分回滚

架构:
  ~/.hermes/skills/_distilled/          — 蒸馏产物 (skill 目录)
  ~/.hermes/skills/_distilled/registry.json  — 版本注册表
  ~/.hermes/logs/skill_feedback.jsonl   — skill 使用反馈

用法:
  python3 tools/skill_distiller.py batch --task-type debug --min-sessions 3
  python3 tools/skill_distiller.py distill --task-type debug --sessions sess1 sess2 sess3
  python3 tools/skill_distiller.py feedback --skill <name> --success true
  python3 tools/skill_distiller.py prune --min-success-rate 0.5
  python3 tools/skill_distiller.py stats
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DISTILLED_DIR = HERMES_HOME / "skills" / "_distilled"
REGISTRY_FILE = DISTILLED_DIR / "registry.json"
LEARNING_LOG = HERMES_HOME / "logs" / "skill_learning.jsonl"
FEEDBACK_LOG = HERMES_HOME / "logs" / "skill_feedback.jsonl"
SESSIONS_DIR = HERMES_HOME / "sessions"

# LLM 配置
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
FINNA_KEY = "app-6OzRGg93TfuDOny9NUnKMvQU"  # Qwen3-32b


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def load_registry() -> dict:
    """加载版本注册表"""
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return {"skills": {}, "version": 1}
    return {"skills": {}, "version": 1}


def save_registry(reg: dict):
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def load_learning_log() -> list[dict]:
    """加载学习日志"""
    entries = []
    if not LEARNING_LOG.exists():
        return entries
    with open(LEARNING_LOG) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return entries


def load_feedback_log() -> list[dict]:
    """加载反馈日志"""
    entries = []
    if not FEEDBACK_LOG.exists():
        return entries
    with open(FEEDBACK_LOG) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return entries


def load_session(session_id: str) -> Optional[str]:
    """加载单个 session 的内容"""
    for ext in [".json", ".jsonl"]:
        path = SESSIONS_DIR / f"{session_id}{ext}"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                # 尝试提取对话文本
                if isinstance(data, list):
                    return json.dumps(data, ensure_ascii=False)
                elif isinstance(data, dict):
                    return json.dumps(data.get("messages", data), ensure_ascii=False)
            except Exception:
                pass
    return None


def find_draft_skills(task_type: str) -> list[dict]:
    """查找指定任务类型的所有草稿 skill"""
    drafts_dir = HERMES_HOME / "skills" / "_drafts"
    if not drafts_dir.exists():
        return []
    
    results = []
    for skill_dir in sorted(drafts_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        meta_file = skill_dir / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            if meta.get("task_type") == task_type:
                skill_file = skill_dir / "SKILL.md"
                results.append({
                    "name": skill_dir.name,
                    "meta": meta,
                    "content": skill_file.read_text() if skill_file.exists() else "",
                })
        except Exception:
            continue
    return results


def group_sessions_by_type(entries: list[dict]) -> dict[str, list[dict]]:
    """按 task_type 分组 session"""
    groups = {}
    for e in entries:
        # 从 learning log 推断 task_type
        # learning_log entries have: skill_name, timestamp, score, tool_calls
        # 需要从 skill_name 推断 task_type
        sn = e.get("skill_name", "")
        task_type = "unknown"
        for t in ["debug", "feature", "refactor", "config", "optimize", "data"]:
            if t in sn:
                task_type = t
                break
        
        if task_type not in groups:
            groups[task_type] = []
        groups[task_type].append(e)
    
    return groups


def distill_batch(task_type: str, drafts: list[dict]) -> Optional[dict]:
    """用 LLM 批量蒸馏：从多个 draft 中提取共性模式"""
    if len(drafts) < 2:
        log(f"需要至少 2 个 draft，当前: {len(drafts)}")
        return None
    
    # 拼接所有 draft 内容
    drafts_text = "\n\n---\n\n".join([
        f"## Draft {i+1}: {d['name']}\n评分: {d['meta'].get('outcome_score','?')}/10\n调用: {d['meta'].get('tool_calls','?')} 次\n\n{d['content'][:2000]}"
        for i, d in enumerate(drafts)
    ])
    
    prompt = f"""你是一个 AI Agent 技能蒸馏器。请从以下 {len(drafts)} 个同类任务的执行记录中，提取通用的成功模式，生成一个高质量的 SKILL.md。

## 任务类型: {task_type}

## 各次执行记录
{drafts_text[:6000]}

## 要求
1. 识别跨 session 重复出现的**成功模式**
2. 提取**共性的关键步骤**（不是某一次的具体操作，而是可复用的流程）
3. 记录**反复出现的坑**（多个 session 都踩过的）
4. 总结**通用验证方法**

输出格式:
```
---
name: {task_type}-v{{version}}
description: 一句话描述这个可复用技能
tags: [{task_type}, batch-distilled]
version: {{version}}
parent_skills: [列出被合并的 draft 名称]
---

# Skill: {task_type} (批量蒸馏)

## 触发条件
[什么情况下应加载此技能]

## 通用步骤 (跨 {len(drafts)} 次执行)
1. ...
2. ...

## 反复出现的关键坑
- 坑1: ... (出现在 N/{len(drafts)} 次执行中)
- 坑2: ...

## 验证方法
- ...
```

只输出 SKILL.md 内容，不要额外解释。"""

    payload = {
        "model": "qwen3-32b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2500,
        "stream": False,
        "extra_body": {"enable_thinking": False},
    }

    try:
        req = urllib.request.Request(
            FINNA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {FINNA_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"LLM 批量蒸馏失败: {e}")
        return None
    
    return {"content": content.strip(), "task_type": task_type, "draft_count": len(drafts)}


def save_distilled(result: dict, task_type: str) -> Path:
    """保存蒸馏产物到 _distilled 目录"""
    reg = load_registry()
    
    # 确定版本号
    existing = reg["skills"].get(task_type, {})
    current_version = existing.get("latest_version", 0)
    new_version = current_version + 1
    
    # 解析 skill name
    import re
    name_match = re.search(r"name:\s*([a-z0-9-]+)", result["content"])
    base_name = name_match.group(1) if name_match else f"{task_type}-v{new_version}"
    skill_name = f"{base_name}"
    
    # 创建目录
    skill_dir = DISTILLED_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    
    # 写入 SKILL.md
    (skill_dir / "SKILL.md").write_text(result["content"])
    
    # 写入元数据
    meta = {
        "name": skill_name,
        "task_type": task_type,
        "version": new_version,
        "distilled_at": datetime.now(timezone.utc).isoformat(),
        "source_count": result.get("draft_count", 0),
        "source_drafts": result.get("source_drafts", []),
        "status": "active",
        "success_rate": None,
        "usage_count": 0,
    }
    (skill_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    
    # 更新注册表
    reg["skills"][task_type] = {
        "latest_version": new_version,
        "versions": {
            **existing.get("versions", {}),
            str(new_version): {
                "name": skill_name,
                "distilled_at": meta["distilled_at"],
                "source_count": meta["source_count"],
                "status": "active",
            }
        }
    }
    reg["version"] = reg.get("version", 1) + 1
    save_registry(reg)
    
    log(f"✅ 已保存: {skill_dir} (v{new_version})")
    return skill_dir


def record_feedback(skill_name: str, success: bool, task_id: str = "",
                    score: float = 0.0, notes: str = ""):
    """记录 skill 使用反馈"""
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "skill": skill_name,
        "success": success,
        "task_id": task_id,
        "score": score,
        "notes": notes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(FEEDBACK_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    log(f"📝 反馈已记录: {skill_name} -> {'✅' if success else '❌'}")


def compute_success_rate(skill_name: str) -> tuple[float, int]:
    """计算 skill 的成功率"""
    feedbacks = load_feedback_log()
    relevant = [f for f in feedbacks if f["skill"] == skill_name]
    if not relevant:
        return 1.0, 0
    
    successes = sum(1 for f in relevant if f["success"])
    return successes / len(relevant), len(relevant)


def prune_low_performing(min_success_rate: float = 0.5, 
                         min_samples: int = 3, 
                         dry_run: bool = True) -> str:
    """剪除低成功率 skill（归档到 _archived）"""
    reg = load_registry()
    feedbacks = load_feedback_log()
    
    archived = []
    lines = [f"🔪 Skill 剪枝 (min_success_rate={min_success_rate}, min_samples={min_samples})"]
    
    for task_type, skill_info in reg["skills"].items():
        for ver, ver_info in skill_info.get("versions", {}).items():
            name = ver_info["name"]
            rate, count = compute_success_rate(name)
            
            if count < min_samples:
                lines.append(f"   ⏭️ {name}: {count} samples (不足 {min_samples})")
                continue
            
            if rate < min_success_rate:
                lines.append(f"   ❌ {name}: {rate:.0%} ({count} samples) — 标记为 deprecated")
                if not dry_run:
                    ver_info["status"] = "deprecated"
                    archived.append(name)
            else:
                lines.append(f"   ✅ {name}: {rate:.0%} ({count} samples)")
    
    if dry_run:
        lines.append(f"\n   --no-dry-run 确认剪除 {len(archived)} 个 skill")
    else:
        save_registry(reg)
        if archived:
            # 移动到 _archived
            archive_dir = DISTILLED_DIR / "_archived"
            archive_dir.mkdir(exist_ok=True)
            for name in archived:
                src = DISTILLED_DIR / name
                dst = archive_dir / name
                if src.exists():
                    import shutil
                    shutil.move(str(src), str(dst))
            lines.append(f"\n   已归档 {len(archived)} 个 skill 到 _archived/")
    
    return "\n".join(lines)


def get_stats() -> str:
    """蒸馏飞轮统计"""
    reg = load_registry()
    feedbacks = load_feedback_log()
    entries = load_learning_log()
    
    lines = [
        "🔄 Skill 蒸馏飞轮统计",
        f"   总学习记录: {len(entries)}",
        f"   已蒸馏 skill: {len(reg['skills'])}",
        f"   总反馈数: {len(feedbacks)}",
        "",
    ]
    
    if reg["skills"]:
        lines.append("   已蒸馏 skills:")
        for task_type, info in reg["skills"].items():
            latest = info.get("latest_version", "?")
            versions = info.get("versions", {})
            active_count = sum(1 for v in versions.values() if v.get("status") == "active")
            lines.append(f"     - {task_type}: v{latest} ({active_count} active, {len(versions)} total)")
        
        # 成功率排名
        lines.append(f"\n   成功率排名:")
        scored = []
        for task_type, info in reg["skills"].items():
            for ver, ver_info in info.get("versions", {}).items():
                name = ver_info["name"]
                rate, count = compute_success_rate(name)
                if count > 0:
                    scored.append((name, rate, count))
        scored.sort(key=lambda x: x[1], reverse=True)
        for name, rate, count in scored[:10]:
            emoji = "🔥" if rate > 0.8 else "⚠️" if rate > 0.5 else "💀"
            lines.append(f"     {emoji} {name}: {rate:.0%} ({count} uses)")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Skill Distillation Flywheel")
    sub = parser.add_subparsers(dest="command")
    
    # batch — 批量检查并蒸馏
    p = sub.add_parser("batch", help="批量蒸馏")
    p.add_argument("--task-type", help="任务类型 (debug/feature/refactor/config/optimize/data)")
    p.add_argument("--min-sessions", type=int, default=3, help="最少 session 数触发蒸馏")
    p.add_argument("--all", action="store_true", help="处理所有任务类型")
    
    # distill — 手动指定 session 蒸馏
    p = sub.add_parser("distill", help="指定 sessions 手动蒸馏")
    p.add_argument("--task-type", required=True, help="任务类型")
    p.add_argument("--sessions", nargs="+", help="session IDs")
    
    # feedback — 记录 skill 使用反馈
    p = sub.add_parser("feedback", help="记录 skill 使用反馈")
    p.add_argument("--skill", required=True, help="skill 名称")
    p.add_argument("--success", type=lambda s: s.lower() == "true", required=True)
    p.add_argument("--task-id", default="", help="关联任务 ID")
    p.add_argument("--score", type=float, default=0.0)
    p.add_argument("--notes", default="")
    
    # prune — 剪除低分 skill
    p = sub.add_parser("prune", help="剪除低成功率 skill")
    p.add_argument("--min-success-rate", type=float, default=0.5)
    p.add_argument("--min-samples", type=int, default=3)
    p.add_argument("--no-dry-run", action="store_true")
    
    # stats
    sub.add_parser("stats", help="蒸馏飞轮统计")
    
    args = parser.parse_args()
    
    if args.command == "batch":
        entries = load_learning_log()
        groups = group_sessions_by_type(entries)
        
        task_types = list(groups.keys()) if args.all else (
            [args.task_type] if args.task_type else list(groups.keys())
        )
        
        for task_type in task_types:
            if task_type == "unknown":
                continue
            
            # 找 draft skills
            drafts = find_draft_skills(task_type)
            
            # 也需要从 groups 检查 session 数量
            session_count = len(groups.get(task_type, []))
            
            print(f"\n📋 {task_type}: {len(drafts)} drafts, {session_count} sessions")
            
            if len(drafts) < args.min_sessions:
                print(f"   ⏭️ 不足 {args.min_sessions} 个 draft，跳过")
                continue
            
            print(f"   🧠 正在批量蒸馏...")
            result = distill_batch(task_type, drafts)
            if result:
                result["source_drafts"] = [d["name"] for d in drafts]
                skill_dir = save_distilled(result, task_type)
                print(f"   ✅ 已生成: {skill_dir}")
            else:
                print(f"   ❌ 蒸馏失败")
    
    elif args.command == "distill":
        drafts = find_draft_skills(args.task_type)
        if not drafts:
            print(f"❌ 未找到 {args.task_type} 的 draft skills")
            sys.exit(1)
        
        print(f"📋 {args.task_type}: {len(drafts)} drafts")
        result = distill_batch(args.task_type, drafts)
        if result:
            result["source_drafts"] = [d["name"] for d in drafts]
            skill_dir = save_distilled(result, args.task_type)
            print(f"✅ 已生成: {skill_dir}")
    
    elif args.command == "feedback":
        record_feedback(args.skill, args.success, args.task_id, args.score, args.notes)
        rate, count = compute_success_rate(args.skill)
        print(f"📊 {args.skill}: {rate:.0%} success ({count} uses)")
    
    elif args.command == "prune":
        print(prune_low_performing(
            args.min_success_rate, args.min_samples, 
            dry_run=not args.no_dry_run
        ))
    
    elif args.command == "stats":
        print(get_stats())
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()