#!/usr/bin/env python3
"""
Skill Auto-Learner — 对标 Letta Sleep-time Compute

在会话结束后（Stop hook），自动分析成功的工作流，
蒸馏为可复用的 skill。让 agent 从自身经验中持续进化。

机制（对标 Letta 双层 agent 架构）:
  1. 检查 outcomes 评分（>= 7.0 的会话才有学习价值）
  2. 检测工具调用数（>= 5 次才算复杂工作流）
  3. 用 LLM 蒸馏成功模式 → SKILL.md
  4. 保存到 ~/.hermes/skills/_drafts/ 供人工 review

用法:
  python3 scripts/skill_auto_learn.py analyze    # 分析最近会话
  python3 scripts/skill_auto_learn.py draft      # 生成 skill 草稿
  python3 scripts/skill_auto_learn.py stats      # 学习统计
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
DRAFTS_DIR = HERMES_HOME / "skills" / "_drafts"
LEARNING_LOG = HERMES_HOME / "logs" / "skill_learning.jsonl"
OUTCOMES_FILE = HERMES_HOME / "logs" / "outcomes_last.json"
SESSION_MEMORY = HERMES_HOME / "session_memory.md"
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
QWEN_KEY = "app-6OzRGg93TfuDOny9NUnKMvQU"


def get_session_snapshot() -> dict:
    """获取会话快照"""
    snapshot = {
        "tool_calls": 0,
        "success": False,
        "outcome_score": 0,
        "task_type": "unknown",
        "files_modified": [],
        "errors": 0,
    }

    if not SESSION_MEMORY.exists():
        return snapshot

    text = SESSION_MEMORY.read_text()
    lines = text.split("\n")

    # 统计工具调用
    snapshot["tool_calls"] = len([l for l in lines if "Tool:" in l or "tool_call" in l.lower()])

    # 检测错误
    snapshot["errors"] = len([l for l in lines
        if "error" in l.lower() or "失败" in l or "failed" in l.lower() or "traceback" in l.lower()])

    # 检测修改的文件
    file_pattern = re.findall(r"/[\w/.-]+\.(py|sh|json|yaml|yml|toml|md|js|ts|html|css)", text)
    snapshot["files_modified"] = list(set(file_pattern))[:20]

    # 推断任务类型
    keywords = {
        "debug": ["bug", "debug", "修复", "fix", "error", "traceback"],
        "feature": ["实现", "新增", "添加", "feature", "implement", "add"],
        "refactor": ["重构", "refactor", "整理", "清理", "clean"],
        "config": ["配置", "config", "setup", "install", "部署", "deploy"],
        "optimize": ["优化", "optimize", "性能", "performance", "加速"],
        "data": ["数据", "data", "分析", "analysis", "extract", "采集"],
    }
    text_lower = text.lower()
    for task_type, kwds in keywords.items():
        if any(k in text_lower for k in kwds):
            snapshot["task_type"] = task_type
            break

    # 读取 outcomes 评分
    if OUTCOMES_FILE.exists():
        try:
            outcomes = json.loads(OUTCOMES_FILE.read_text())
            snapshot["outcome_score"] = outcomes.get("average", 0)
            snapshot["success"] = snapshot["outcome_score"] >= 6.5
        except Exception:
            pass

    # 无 error 且工具调用多 = 成功
    if snapshot["errors"] == 0 and snapshot["tool_calls"] >= 5:
        snapshot["success"] = True

    return snapshot


def should_learn(snapshot: dict) -> bool:
    """判断是否值得学习"""
    # 至少 5 次工具调用（复杂工作流）
    if snapshot["tool_calls"] < 5:
        return False
    # 成功（无错误 + 评分 >= 6.5）
    if not snapshot["success"] and snapshot["outcome_score"] < 6.5:
        return False
    # 有实质性文件修改
    if not snapshot["files_modified"]:
        return False
    return True


def generate_skill_draft(snapshot: dict) -> dict:
    """用 LLM 从会话摘要中生成 skill"""
    if not SESSION_MEMORY.exists():
        return None

    session_text = SESSION_MEMORY.read_text()[:5000]

    prompt = f"""你是一个 AI Agent 技能蒸馏器。请从以下会话记录中提取可复用的工作流，生成一个 SKILL.md 格式的技能文件。

## 会话摘要
工具调用次数: {snapshot['tool_calls']}
任务类型: {snapshot['task_type']}
修改文件: {', '.join(snapshot['files_modified'][:10])}
错误数: {snapshot['errors']}

## 最近会话记录（截取）
{session_text[:3000]}

## 要求
提取这个会话中**成功的关键步骤、踩过的坑、验证方法**，生成一个紧凑的 SKILL.md。
格式:
```
---
name: skill-name         # 短名，用-连接
description: 一句话描述
tags: [tag1, tag2]
---

# Skill: {标题}

## 触发条件
## 步骤
## 关键坑
## 验证
```

只输出 SKILL.md 内容，不要额外解释。"""

    payload = {
        "model": "qwen3-32b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "stream": False,
        "extra_body": {"enable_thinking": False},
    }

    try:
        req = urllib.request.Request(
            FINNA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {QWEN_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"LLM 蒸馏失败: {e}", file=sys.stderr)
        return None

    # 解析 name
    name_match = re.search(r"name:\s*([a-z0-9-]+)", content)
    skill_name = name_match.group(1) if name_match else f"auto-{snapshot['task_type']}-{int(datetime.now().timestamp()) % 10000}"

    return {"name": skill_name, "content": content.strip(), "snapshot": snapshot}


def save_draft(draft: dict):
    """保存草稿到 _drafts 目录"""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

    skill_dir = DRAFTS_DIR / draft["name"]
    skill_dir.mkdir(exist_ok=True)

    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(draft["content"])

    # 保存元数据
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "outcome_score": draft["snapshot"]["outcome_score"],
        "tool_calls": draft["snapshot"]["tool_calls"],
        "task_type": draft["snapshot"]["task_type"],
        "source": "auto-learn",
    }
    (skill_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    # 记录学习日志
    LEARNING_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "skill_name": draft["name"],
        "timestamp": meta["generated_at"],
        "score": meta["outcome_score"],
        "tool_calls": meta["tool_calls"],
    }
    with open(LEARNING_LOG, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return skill_file


def analyze_and_learn() -> str:
    """主流程：分析 → 判断 → 蒸馏 → 保存"""
    snapshot = get_session_snapshot()

    print("📊 会话分析:")
    print(f"   工具调用: {snapshot['tool_calls']}")
    print(f"   任务类型: {snapshot['task_type']}")
    print(f"   修改文件: {len(snapshot['files_modified'])} 个")
    print(f"   错误数:   {snapshot['errors']}")
    print(f"   评分:     {snapshot['outcome_score']}/10")
    print()

    if not should_learn(snapshot):
        reason = "工具调用不足" if snapshot["tool_calls"] < 5 else "未达标（评分/错误）"
        print(f"⏭️  跳过学习: {reason}")
        return None

    print("🧠 有价值的工作流！正在蒸馏 skill...")
    draft = generate_skill_draft(snapshot)

    if not draft:
        print("❌ 蒸馏失败")
        return None

    skill_file = save_draft(draft)
    print(f"✅ Skill 草稿已生成: {skill_file}")
    print(f"   查看: cat {skill_file}")
    print("💡 审核后移动到 ~/.hermes/skills/ 即可生效")

    return str(skill_file)


def get_stats() -> str:
    """学习统计"""
    if not LEARNING_LOG.exists():
        return "还没有自动学习的 skill"

    entries = []
    with open(LEARNING_LOG) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue

    if not entries:
        return "没有有效的学习记录"

    lines = [
        f"📚 自动学习统计: {len(entries)} 个 skill",
        f"   平均评分: {sum(e['score'] for e in entries)/len(entries):.1f}/10",
        f"   平均工具调用: {sum(e['tool_calls'] for e in entries)//len(entries)} 次",
    ]

    # 列表
    drafts = list(DRAFTS_DIR.glob("*/SKILL.md")) if DRAFTS_DIR.exists() else []
    if drafts:
        lines.append(f"\n  待审核草稿 ({len(drafts)} 个):")
        for d in sorted(drafts):
            meta_file = d.parent / "meta.json"
            score = ""
            if meta_file.exists():
                try:
                    m = json.loads(meta_file.read_text())
                    score = f" [评分: {m.get('outcome_score','?')}]"
                except Exception:
                    pass
            lines.append(f"    - {d.parent.name}{score}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skill Auto-Learner")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("analyze", help="分析最近会话")
    sub.add_parser("draft", help="分析 + 生成 skill 草稿")
    sub.add_parser("stats", help="学习统计")

    args = parser.parse_args()

    if args.command == "analyze":
        snapshot = get_session_snapshot()
        print(f"工具调用: {snapshot['tool_calls']}  评分: {snapshot['outcome_score']}")
        print(f"任务类型: {snapshot['task_type']}  错误: {snapshot['errors']}")
        print(f"可学习: {'✅ 是' if should_learn(snapshot) else '❌ 否'}")
    elif args.command == "draft":
        analyze_and_learn()
    elif args.command == "stats":
        print(get_stats())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()