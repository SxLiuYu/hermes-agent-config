#!/usr/bin/env python3
"""
Hermes Memory Blocks — Letta 式自编辑记忆块系统
================================================

记忆不是被动存储，而是 agent 可主动编辑的工作记忆。

核心概念:
  - Memory Blocks: 命名的、有大小限制的上下文段落
  - 自编辑: agent 通过工具调用主动修改自己的记忆
  - 层级: 核心记忆（常驻 context）+ 归档记忆（按需检索）
  - 压缩: 当块超限时，LLM 自动总结为更紧凑的形式
  - 版本历史: 每次修改存一份旧版，支持回滚

工具接口（供 agent 调用）:
  memory_block_insert(block_name, text)
  memory_block_replace(block_name, old_text, new_text)
  memory_block_rethink(block_name)
  memory_block_search(query)
  memory_block_create(name, label, limit)
  memory_block_compact()

命令行:
  python memory_blocks.py list
  python memory_blocks.py show <name>
  python memory_blocks.py insert <name> <text...>
  python memory_blocks.py replace <name> <old> <new>
  python memory_blocks.py rethink <name>
  python memory_blocks.py search <query>
  python memory_blocks.py create <name> <label> <limit>
  python memory_blocks.py compact
  python memory_blocks.py inject
  python memory_blocks.py rollback <name> [version_id]
"""

import json
import os
import sqlite3
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

# ── 路径 & 配置 ──────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "memory_blocks.db"

# LLM API 配置（用于 rethink / compact 压缩总结）
LLM_API_URL = os.environ.get("MEMORY_BLOCKS_LLM_URL", os.environ.get("HERMES_LLM_URL", ""))
LLM_API_KEY = os.environ.get("MEMORY_BLOCKS_LLM_KEY", os.environ.get("HERMES_LLM_KEY", ""))
LLM_MODEL = os.environ.get("MEMORY_BLOCKS_LLM_MODEL", "deepseek-chat")

# 默认记忆块模板
DEFAULT_BLOCKS = [
    {
        "name": "user_profile",
        "label": "用户档案",
        "limit": 500,
        "content": "",
        "editable": True,
        "in_context": True,
    },
    {
        "name": "project_context",
        "label": "项目上下文",
        "limit": 1000,
        "content": "",
        "editable": True,
        "in_context": True,
    },
    {
        "name": "lessons_learned",
        "label": "经验教训",
        "limit": 2000,
        "content": "",
        "editable": True,
        "in_context": False,
    },
]

# 内置压缩 prompt
COMPACT_PROMPT = """你是一个记忆压缩助手。请将以下记忆块内容压缩为更紧凑的形式，保留所有关键信息。

【记忆块名称】{label}
【当前内容】
{content}

【压缩要求】
1. 保留所有重要事实、日期、人名、决策、结论
2. 合并冗余信息，删除重复内容
3. 使用简洁的列表或结构化格式
4. 压缩后总字数不超过 {limit} 字符
5. 如果内容已经足够紧凑，保持原样

请直接输出压缩后的内容，不要加任何前缀或解释。"""

RETHINK_PROMPT = """你是一个记忆整理助手。请重新整理以下记忆块，使其更加结构化、有条理。

【记忆块名称】{label}
【当前内容】
{content}

【整理要求】
1. 按主题或时间线重新组织信息
2. 提炼核心要点，放在开头
3. 删除过时、矛盾或不再相关的信息
4. 使用清晰的层级结构
5. 输出不超过 {limit} 字符

请直接输出整理后的内容，不要加任何前缀或解释。"""


# ═══════════════════════════════════════════════════════════════
#  数据库层
# ═══════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    """获取数据库连接，自动初始化表结构。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    _seed_defaults(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    """初始化表结构。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            limit_chars INTEGER NOT NULL DEFAULT 1000,
            in_context INTEGER NOT NULL DEFAULT 1,
            editable INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blocks_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER NOT NULL,
            block_name TEXT NOT NULL,
            content TEXT NOT NULL,
            limit_chars INTEGER,
            in_context INTEGER,
            action TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE CASCADE
        )
    """)
    # 迁移：补齐可能缺失的列
    for col, col_type in [
        ("editable", "INTEGER NOT NULL DEFAULT 1"),
        ("label", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE blocks ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _seed_defaults(conn: sqlite3.Connection):
    """如果 blocks 表为空，插入默认记忆块。"""
    count = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    if count == 0:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for blk in DEFAULT_BLOCKS:
            conn.execute(
                """INSERT INTO blocks (name, label, content, limit_chars, in_context, editable, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    blk["name"],
                    blk["label"],
                    blk["content"],
                    blk["limit"],
                    1 if blk["in_context"] else 0,
                    1 if blk["editable"] else 0,
                    now,
                    now,
                ),
            )
        conn.commit()


# ═══════════════════════════════════════════════════════════════
#  核心操作
# ═══════════════════════════════════════════════════════════════

def _archive_block(conn: sqlite3.Connection, block_name: str, action: str):
    """将块的当前版本存入历史表。"""
    row = conn.execute(
        "SELECT id, name, content, limit_chars, in_context FROM blocks WHERE name = ?",
        (block_name,),
    ).fetchone()
    if row:
        conn.execute(
            """INSERT INTO blocks_history (block_id, block_name, content, limit_chars, in_context, action)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (row["id"], row["name"], row["content"], row["limit_chars"], row["in_context"], action),
        )


def _touch_block(conn: sqlite3.Connection, block_name: str):
    """更新块的 updated_at 时间戳。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE blocks SET updated_at = ? WHERE name = ?", (now, block_name))


# ── 工具函数 ──────────────────────────────────────────────────

def memory_block_create(
    name: str,
    label: str = "",
    limit: int = 1000,
    in_context: bool = True,
    editable: bool = True,
    initial_content: str = "",
) -> Dict[str, Any]:
    """
    创建新的记忆块。

    Args:
        name: 块唯一名称（英文标识符）
        label: 显示标签（中文描述）
        limit: 字符数上限
        in_context: 是否注入 system prompt
        editable: 是否允许编辑
        initial_content: 初始内容

    Returns:
        {"ok": True, "block": {...}} 或 {"ok": False, "error": "..."}
    """
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM blocks WHERE name = ?", (name,)).fetchone()
        if existing:
            return {"ok": False, "error": f"记忆块 '{name}' 已存在"}

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO blocks (name, label, content, limit_chars, in_context, editable, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, label, initial_content, limit, 1 if in_context else 0, 1 if editable else 0, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (name,)).fetchone()
        return {"ok": True, "block": _row_to_dict(row)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def memory_block_insert(block_name: str, text: str) -> Dict[str, Any]:
    """
    向指定记忆块追加内容。

    Args:
        block_name: 记忆块名称
        text: 要追加的文本

    Returns:
        {"ok": True, "block": {...}, "truncated": bool}
    """
    if not text or not text.strip():
        return {"ok": False, "error": "要插入的文本为空"}

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不存在"}
        if not row["editable"]:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不可编辑"}

        _archive_block(conn, block_name, "insert")

        current = row["content"]
        # 智能拼接：如果已有内容且不以换行结尾，加换行
        separator = "\n\n" if current and not current.endswith("\n") else ""
        new_content = current + separator + text.strip()

        limit = row["limit_chars"]
        truncated = len(new_content) > limit

        conn.execute("UPDATE blocks SET content = ? WHERE name = ?", (new_content[:limit], block_name))
        _touch_block(conn, block_name)
        conn.commit()

        updated = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        return {
            "ok": True,
            "block": _row_to_dict(updated),
            "truncated": truncated,
            "char_count": len(new_content[:limit]),
            "limit": limit,
            "usage_pct": round(len(new_content[:limit]) / limit * 100, 1),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def memory_block_replace(block_name: str, old_text: str, new_text: str) -> Dict[str, Any]:
    """
    替换记忆块中的指定文本。

    Args:
        block_name: 记忆块名称
        old_text: 要被替换的文本（精确匹配）
        new_text: 替换后的文本（传空字符串即为删除）

    Returns:
        {"ok": True, "block": {...}, "replaced": bool}
    """
    if not old_text:
        return {"ok": False, "error": "old_text 不能为空，使用 memory_block_insert 来追加内容"}

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不存在"}
        if not row["editable"]:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不可编辑"}

        current = row["content"]
        if old_text not in current:
            return {"ok": False, "error": "未找到要替换的文本", "replaced": False}

        _archive_block(conn, block_name, "replace")
        new_content = current.replace(old_text, new_text, 1)

        conn.execute("UPDATE blocks SET content = ? WHERE name = ?", (new_content, block_name))
        _touch_block(conn, block_name)
        conn.commit()

        updated = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        return {"ok": True, "block": _row_to_dict(updated), "replaced": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def memory_block_rethink(block_name: str) -> Dict[str, Any]:
    """
    调用 LLM 重新整理/压缩指定记忆块。

    Args:
        block_name: 记忆块名称

    Returns:
        {"ok": True, "block": {...}, "compressed": bool}
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不存在"}
        if not row["editable"]:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不可编辑"}

        content = row["content"]
        limit = row["limit_chars"]
        label = row["label"]

        if not content.strip():
            return {"ok": True, "block": _row_to_dict(row), "compressed": False, "message": "内容为空，无需整理"}

        # 如果内容已在限制内，尝试整理（而非压缩）
        needs_compression = len(content) > limit
        prompt_template = COMPACT_PROMPT if needs_compression else RETHINK_PROMPT
        prompt = prompt_template.format(label=label, content=content, limit=limit)

        new_content = _call_llm(prompt)

        if not new_content:
            return {"ok": False, "error": "LLM 调用失败，未获得有效响应"}

        # 处理 LLM 返回内容（可能被引号包裹）
        new_content = new_content.strip().strip('"').strip("'")

        _archive_block(conn, block_name, "rethink")
        conn.execute("UPDATE blocks SET content = ? WHERE name = ?", (new_content, block_name))
        _touch_block(conn, block_name)
        conn.commit()

        updated = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        return {
            "ok": True,
            "block": _row_to_dict(updated),
            "compressed": needs_compression,
            "old_len": len(content),
            "new_len": len(new_content),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def memory_block_search(query: str, limit: int = 5) -> Dict[str, Any]:
    """
    搜索所有记忆块（含归档块）。

    使用简单的关键词匹配（不区分大小写），按匹配度排序。

    Args:
        query: 搜索关键词
        limit: 返回结果数量上限

    Returns:
        {"ok": True, "results": [...]}
    """
    if not query or not query.strip():
        return {"ok": False, "error": "搜索关键词为空"}

    conn = get_db()
    try:
        keywords = query.lower().split()
        rows = conn.execute(
            "SELECT * FROM blocks ORDER BY updated_at DESC"
        ).fetchall()

        results = []
        for row in rows:
            content_lower = row["content"].lower()
            # 计算匹配分数：每个关键词命中 +1，连续命中额外加分
            score = 0
            for kw in keywords:
                count = content_lower.count(kw)
                score += count
            if score > 0:
                # 名称匹配加权
                name_lower = row["name"].lower()
                label_lower = row["label"].lower()
                for kw in keywords:
                    if kw in name_lower:
                        score += 5
                    if kw in label_lower:
                        score += 3

                # 截取匹配上下文
                snippets = _extract_snippets(row["content"], keywords, max_snippets=3, context_chars=80)

                results.append({
                    "name": row["name"],
                    "label": row["label"],
                    "content": row["content"],
                    "limit_chars": row["limit_chars"],
                    "in_context": bool(row["in_context"]),
                    "score": score,
                    "snippets": snippets,
                    "updated_at": row["updated_at"],
                })

        # 按分数降序排列
        results.sort(key=lambda x: x["score"], reverse=True)
        return {"ok": True, "results": results[:limit], "total_matches": len(results)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def memory_block_compact() -> Dict[str, Any]:
    """
    压缩所有超出限制的记忆块。

    遍历所有块，对超出 limit_chars 的块调用 LLM 压缩。

    Returns:
        {"ok": True, "compacted": [...], "errors": [...]}
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM blocks WHERE LENGTH(content) > limit_chars AND editable = 1"
        ).fetchall()

        compacted = []
        errors = []

        for row in rows:
            try:
                result = memory_block_rethink(row["name"])
                if result["ok"]:
                    compacted.append(row["name"])
                else:
                    errors.append({"name": row["name"], "error": result.get("error", "未知错误")})
            except Exception as e:
                errors.append({"name": row["name"], "error": str(e)})

        return {"ok": True, "compacted": compacted, "errors": errors, "total_compacted": len(compacted)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


# ── 查询 & 注入 ──────────────────────────────────────────────

def get_block(block_name: str) -> Optional[Dict[str, Any]]:
    """获取单个记忆块。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_blocks() -> List[Dict[str, Any]]:
    """列出所有记忆块。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM blocks ORDER BY in_context DESC, name ASC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def inject_context() -> str:
    """
    生成 system prompt 注入文本。

    返回所有 in_context=True 的记忆块，格式化为 Markdown 段落。
    可在启动时注入到 system prompt 中以实现 Letta 式的持久化上下文。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM blocks WHERE in_context = 1 AND content != '' ORDER BY name ASC"
        ).fetchall()

        if not rows:
            return ""

        sections = []
        for row in rows:
            label = row["label"] or row["name"]
            sections.append(f"<!-- MEMORY_BLOCK: {row['name']} -->\n## {label}\n\n{row['content']}")

        return "\n\n---\n\n".join(sections)
    finally:
        conn.close()


def get_block_history(block_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """获取记忆块的版本历史。"""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM blocks_history
               WHERE block_name = ?
               ORDER BY archived_at DESC LIMIT ?""",
            (block_name, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "block_id": r["block_id"],
                "block_name": r["block_name"],
                "content": r["content"],
                "limit_chars": r["limit_chars"],
                "in_context": bool(r["in_context"]),
                "action": r["action"],
                "archived_at": r["archived_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def rollback_block(block_name: str, version_id: Optional[int] = None) -> Dict[str, Any]:
    """
    回滚记忆块到历史版本。

    Args:
        block_name: 记忆块名称
        version_id: 目标历史版本 ID。如果为 None，回滚到上一个版本。

    Returns:
        {"ok": True, "block": {...}}
    """
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"记忆块 '{block_name}' 不存在"}

        if version_id is not None:
            hist = conn.execute(
                "SELECT * FROM blocks_history WHERE id = ? AND block_name = ?",
                (version_id, block_name),
            ).fetchone()
        else:
            hist = conn.execute(
                "SELECT * FROM blocks_history WHERE block_name = ? ORDER BY archived_at DESC LIMIT 1",
                (block_name,),
            ).fetchone()

        if not hist:
            return {"ok": False, "error": "没有可用的历史版本"}

        # 存档当前版本
        _archive_block(conn, block_name, "pre-rollback")
        conn.execute(
            "UPDATE blocks SET content = ? WHERE name = ?",
            (hist["content"], block_name),
        )
        _touch_block(conn, block_name)
        conn.commit()

        updated = conn.execute("SELECT * FROM blocks WHERE name = ?", (block_name,)).fetchone()
        return {
            "ok": True,
            "block": _row_to_dict(updated),
            "rolled_back_to_version": hist["id"],
            "archived_at": hist["archived_at"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """将 sqlite3.Row 转为普通字典。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "label": row["label"],
        "content": row["content"],
        "limit_chars": row["limit_chars"],
        "in_context": bool(row["in_context"]),
        "editable": bool(row["editable"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "char_count": len(row["content"]),
        "usage_pct": round(len(row["content"]) / row["limit_chars"] * 100, 1) if row["limit_chars"] > 0 else 0,
    }


def _extract_snippets(
    content: str,
    keywords: List[str],
    max_snippets: int = 3,
    context_chars: int = 80,
) -> List[str]:
    """从内容中提取包含关键词的上下文片段。"""
    content_lower = content.lower()
    snippets = []
    used_ranges = []

    for kw in keywords:
        idx = 0
        while idx < len(content_lower):
            pos = content_lower.find(kw, idx)
            if pos == -1:
                break

            start = max(0, pos - context_chars // 2)
            end = min(len(content), pos + len(kw) + context_chars // 2)

            # 避免重复片段
            overlap = any(abs(start - s) < context_chars for s, e in used_ranges)
            if not overlap:
                snippet = content[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(content):
                    snippet = snippet + "…"
                snippets.append(snippet)
                used_ranges.append((start, end))

            idx = pos + len(kw)

            if len(snippets) >= max_snippets:
                return snippets

    return snippets


def _call_llm(prompt: str, max_retries: int = 2) -> Optional[str]:
    """
    调用 LLM API 进行文本压缩/整理。

    支持 OpenAI 兼容 API。如果未配置，返回 None。
    """
    if not LLM_API_URL:
        return None

    import urllib.request
    import urllib.error

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个专业的记忆压缩助手，输出紧凑、结构化、信息密度高的文本。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode("utf-8")

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(LLM_API_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return content
        except Exception:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                # 静默失败——调用方会处理 None
                pass
    return None


# ═══════════════════════════════════════════════════════════════
#  CLI 接口
# ═══════════════════════════════════════════════════════════════

def _format_block(blk: Dict[str, Any], verbose: bool = False) -> str:
    """格式化单个记忆块为可读文本。"""
    ctx_tag = "📌 常驻" if blk["in_context"] else "📁 归档"
    edit_tag = "✏️ 可编辑" if blk["editable"] else "🔒 只读"
    bar_len = min(20, int(blk["usage_pct"] / 5))
    bar_filled = "█" * bar_len
    bar_empty = "░" * (20 - bar_len)
    bar_color = "🟢" if blk["usage_pct"] < 60 else ("🟡" if blk["usage_pct"] < 90 else "🔴")

    lines = [
        f"╭─ {blk['name']}  {ctx_tag}  {edit_tag}",
        f"├─ 标签: {blk['label']}",
        f"├─ 限制: {blk['limit_chars']} 字符  │  当前: {blk['char_count']} 字符 ({blk['usage_pct']}%)",
        f"├─ 用量: {bar_color} [{bar_filled}{bar_empty}]",
        f"├─ 更新: {blk['updated_at']}",
    ]
    if verbose:
        lines.append("├─ 内容:")
        for line in blk["content"].split("\n"):
            lines.append(f"│  {line}")
    else:
        preview = blk["content"][:120].replace("\n", " ")
        if len(blk["content"]) > 120:
            preview += "…"
        lines.append(f"├─ 预览: {preview}")

    lines.append("╰─" + "─" * 40)
    return "\n".join(lines)


def _format_results(results: List[Dict[str, Any]]) -> str:
    """格式化搜索结果。"""
    lines = [f"共找到 {len(results)} 个结果:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"  {i}. [{r['name']}] {r['label']}  (分数: {r['score']})")
        for snippet in r.get("snippets", [])[:2]:
            lines.append(f"     …{snippet}…")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Memory Blocks — Letta 式自编辑记忆块系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s list                         列出所有记忆块
  %(prog)s show user_profile            查看记忆块详情
  %(prog)s insert user_profile "新信息"  追加内容
  %(prog)s replace user_profile "旧" "新" 替换内容
  %(prog)s rethink user_profile          LLM 整理压缩
  %(prog)s search "关键词"               搜索所有块
  %(prog)s create my_block "我的块" 800  创建新块
  %(prog)s compact                       压缩所有超限块
  %(prog)s inject                        输出注入文本
  %(prog)s history user_profile          查看版本历史
  %(prog)s rollback user_profile         回滚到上一版本
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="操作命令")

    # list
    subparsers.add_parser("list", help="列出所有记忆块")

    # show
    p_show = subparsers.add_parser("show", help="查看记忆块详情")
    p_show.add_argument("name", help="记忆块名称")
    p_show.add_argument("-v", "--verbose", action="store_true", help="显示完整内容")

    # insert
    p_insert = subparsers.add_parser("insert", help="向记忆块追加内容")
    p_insert.add_argument("name", help="记忆块名称")
    p_insert.add_argument("text", help="要追加的文本")

    # replace
    p_replace = subparsers.add_parser("replace", help="替换记忆块内容")
    p_replace.add_argument("name", help="记忆块名称")
    p_replace.add_argument("old_text", help="被替换的文本")
    p_replace.add_argument("new_text", help="替换后的文本")

    # rethink
    p_rethink = subparsers.add_parser("rethink", help="LLM 整理压缩记忆块")
    p_rethink.add_argument("name", help="记忆块名称")

    # search
    p_search = subparsers.add_parser("search", help="搜索记忆块")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("-n", "--limit", type=int, default=5, help="返回结果数 (默认: 5)")

    # create
    p_create = subparsers.add_parser("create", help="创建新记忆块")
    p_create.add_argument("name", help="记忆块名称")
    p_create.add_argument("label", help="显示标签")
    p_create.add_argument("limit", type=int, default=1000, help="字符数上限 (默认: 1000)")
    p_create.add_argument("--archive", action="store_true", help="设为归档块 (不注入 context)")

    # compact
    subparsers.add_parser("compact", help="压缩所有超限的记忆块")

    # inject
    subparsers.add_parser("inject", help="输出所有常驻记忆块 (供 system prompt 使用)")

    # history
    p_history = subparsers.add_parser("history", help="查看记忆块版本历史")
    p_history.add_argument("name", help="记忆块名称")
    p_history.add_argument("-n", "--limit", type=int, default=10, help="返回版本数 (默认: 10)")

    # rollback
    p_rollback = subparsers.add_parser("rollback", help="回滚记忆块到历史版本")
    p_rollback.add_argument("name", help="记忆块名称")
    p_rollback.add_argument("version_id", nargs="?", type=int, help="目标版本 ID (默认: 上一版本)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # ── 执行命令 ──

    if args.command == "list":
        blocks = list_blocks()
        if not blocks:
            print("暂无记忆块。使用 'create' 命令创建。")
        else:
            for blk in blocks:
                print(_format_block(blk))
                print()

    elif args.command == "show":
        blk = get_block(args.name)
        if not blk:
            print(f"错误: 记忆块 '{args.name}' 不存在")
            sys.exit(1)
        print(_format_block(blk, verbose=args.verbose))

    elif args.command == "insert":
        result = memory_block_insert(args.name, args.text)
        if result["ok"]:
            print(f"✅ 已追加到 '{args.name}'")
            print(f"   当前: {result['char_count']}/{result['limit']} 字符 ({result['usage_pct']}%)")
            if result.get("truncated"):
                print(f"   ⚠️ 内容超出限制，已截断至 {result['limit']} 字符")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "replace":
        result = memory_block_replace(args.name, args.old_text, args.new_text)
        if result["ok"]:
            print(f"✅ 已替换 '{args.name}' 中的内容")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "rethink":
        print(f"🔄 正在整理 '{args.name}' ...")
        result = memory_block_rethink(args.name)
        if result["ok"]:
            if result.get("compressed"):
                print(f"✅ 已压缩 '{args.name}': {result['old_len']} → {result['new_len']} 字符")
            else:
                print(f"✅ 已整理 '{args.name}': {result['old_len']} → {result['new_len']} 字符")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "search":
        result = memory_block_search(args.query, limit=args.limit)
        if result["ok"]:
            if result["results"]:
                print(_format_results(result["results"]))
                print(f"(共 {result['total_matches']} 个匹配)")
            else:
                print(f"未找到与 '{args.query}' 相关的记忆块")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "create":
        in_context = not args.archive
        result = memory_block_create(
            name=args.name,
            label=args.label,
            limit=args.limit,
            in_context=in_context,
        )
        if result["ok"]:
            ctx_type = "常驻" if in_context else "归档"
            print(f"✅ 已创建记忆块 '{args.name}' ({ctx_type})")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "compact":
        print("🔄 正在压缩所有超限记忆块...")
        result = memory_block_compact()
        if result["ok"]:
            if result["compacted"]:
                print(f"✅ 已压缩 {result['total_compacted']} 个块: {', '.join(result['compacted'])}")
            else:
                print("所有记忆块均在限制内，无需压缩")
            if result["errors"]:
                for err in result["errors"]:
                    print(f"⚠️ {err['name']}: {err['error']}")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)

    elif args.command == "inject":
        text = inject_context()
        if text:
            print(text)
        else:
            print("(无常驻记忆块内容)")

    elif args.command == "history":
        history = get_block_history(args.name, limit=args.limit)
        if not history:
            print(f"记忆块 '{args.name}' 无历史记录")
        else:
            print(f"📜 '{args.name}' 版本历史 ({len(history)} 条):\n")
            for i, h in enumerate(history, 1):
                preview = h["content"][:80].replace("\n", " ")
                if len(h["content"]) > 80:
                    preview += "…"
                print(f"  [{h['id']}] {h['action']:12s}  {h['archived_at']}")
                print(f"       长度: {len(h['content'])} 字符  |  {preview}")
                print()

    elif args.command == "rollback":
        result = rollback_block(args.name, args.version_id)
        if result["ok"]:
            print(f"✅ 已回滚 '{args.name}' 到版本 #{result['rolled_back_to_version']}")
            print(f"   存档时间: {result['archived_at']}")
        else:
            print(f"❌ {result['error']}")
            sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  程序化接口（供其他脚本/agent 直接 import 使用）
# ═══════════════════════════════════════════════════════════════

__all__ = [
    # 核心操作
    "memory_block_create",
    "memory_block_insert",
    "memory_block_replace",
    "memory_block_rethink",
    "memory_block_search",
    "memory_block_compact",
    # 查询
    "get_block",
    "list_blocks",
    "inject_context",
    # 版本管理
    "get_block_history",
    "rollback_block",
    # 数据库
    "get_db",
    "DB_PATH",
    "HERMES_HOME",
]

if __name__ == "__main__":
    main()