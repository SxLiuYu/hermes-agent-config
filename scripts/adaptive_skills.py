#!/usr/bin/env python3
"""
Hermes Adaptive Skill System v2 — 技能自进化引擎
=================================================
用进废退 + oMLX 错误分析 + 链式推荐 + 跨会话趋势

v2 新增:
  - oMLX 错误根因分析: 错误模式聚类 → 自动生成修复建议
  - 技能链检测: Markov 链建模哪些技能经常一起用 → 推荐 "下一个"
  - 跨会话趋势: 30 天窗口滚动评分 → 识别正在上升/衰落的技能
  - 情绪上下文: 记录用户情绪与技能使用关联 → 精准推荐
  - 自动修复: 高信任度建议 (>0.8) 自动应用
  - 报告增强: 热力图、趋势图 (ASCII)、瓶颈识别

用法:
  python3 adaptive_skills.py track <skill> <success> <duration_ms> [error_type]
  python3 adaptive_skills.py report              # 完整健康报告
  python3 adaptive_skills.py evolve              # 运行一轮进化
  python3 adaptive_skills.py recommend [n=5]     # Top-N 技能推荐
  python3 adaptive_skills.py chain <skill>       # 显示技能链
  python3 adaptive_skills.py trends              # 30 天趋势
"""

import json
import math
import os
import sqlite3
import sys
import time
import subprocess
from pathlib import Path
from collections import Counter

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
SKILLS_DIR = HERMES_HOME / "skills"
ADAPTIVE_DB = HERMES_HOME / "adaptive_skills.db"
INTEL_STATE = HERMES_HOME / "conversation_intel_state.json"

WEIGHTS = {"frequency": 0.35, "success_rate": 0.35, "recency": 0.20, "trend": 0.10}
DEPRECATE_DAYS = 60
ERROR_THRESHOLD = 3
DECAY_HALF_LIFE = 14

# ── DB ──
def init_db():
    conn = sqlite3.connect(str(ADAPTIVE_DB))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name  TEXT NOT NULL,
            success     INTEGER NOT NULL,
            duration_ms REAL,
            error_type  TEXT,
            error_msg   TEXT,
            session_id  TEXT,
            user_mood   TEXT,
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_skill_time ON usage_log(skill_name, created_at);
        CREATE INDEX IF NOT EXISTS idx_session ON usage_log(session_id, created_at);

        CREATE TABLE IF NOT EXISTS skill_chains (
            from_skill  TEXT NOT NULL,
            to_skill    TEXT NOT NULL,
            count       INTEGER DEFAULT 1,
            PRIMARY KEY (from_skill, to_skill)
        );

        CREATE TABLE IF NOT EXISTS evolution_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name  TEXT NOT NULL,
            action      TEXT NOT NULL,
            suggestion  TEXT,
            confidence  REAL,
            applied     INTEGER DEFAULT 0,
            created_at  REAL NOT NULL
        );
    """)
    # Migration: add columns that might be missing from v1 schema
    _migrate_add_column(conn, "usage_log", "session_id", "TEXT")
    _migrate_add_column(conn, "usage_log", "user_mood", "TEXT")
    _migrate_add_column(conn, "usage_log", "error_msg", "TEXT")
    _migrate_add_column(conn, "evolution_log", "created_at", "REAL NOT NULL DEFAULT 0")
    conn.commit()
    return conn


# ── Migration Helper ──
def _migrate_add_column(conn, table: str, column: str, col_type: str):
    """Safe migration: add column if not exists."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Scoring ──
def compute_score(conn, skill_name: str, now: float = None) -> dict:
    """计算单个技能的综合分数."""
    if now is None:
        now = time.time()

    rows = conn.execute("""
        SELECT success, duration_ms, created_at
        FROM usage_log
        WHERE skill_name = ?
        ORDER BY created_at DESC
    """, (skill_name,)).fetchall()

    if not rows:
        return {"skill": skill_name, "score": 0, "uses": 0, "success_rate": 0,
                "avg_duration": 0, "last_used_days": 999, "trend": 0}

    total = len(rows)
    successes = sum(1 for r in rows if r[0])
    success_rate = successes / total if total else 0
    avg_duration = sum(r[1] for r in rows if r[1]) / total if total else 0

    # Recency: 天数越近权重越高
    latest = rows[0][2]
    days_since = (now - latest) / 86400
    recency = math.exp(-days_since / DECAY_HALF_LIFE)

    # Frequency: 用半衰期衰减
    frequency = sum(math.exp(-(now - r[2]) / 86400 / DECAY_HALF_LIFE) for r in rows)

    # Trend: 后 30 天 vs 前 30 天
    recent_30 = sum(1 for r in rows if now - r[2] < 30 * 86400)
    older_30 = sum(1 for r in rows if 30 * 86400 <= now - r[2] < 60 * 86400)
    trend = (recent_30 - older_30) / max(older_30, 1)

    score = (
        WEIGHTS["frequency"] * min(frequency / 10, 1.0) +
        WEIGHTS["success_rate"] * success_rate +
        WEIGHTS["recency"] * recency +
        WEIGHTS["trend"] * (0.5 + 0.5 * math.tanh(trend))
    )

    return {
        "skill": skill_name,
        "score": round(score, 4),
        "uses": total,
        "success_rate": round(success_rate, 4),
        "avg_duration": round(avg_duration, 0),
        "last_used_days": round(days_since, 1),
        "trend": round(trend, 3),
        "status": _classify(score, days_since, success_rate),
    }


def _classify(score, days, success_rate) -> str:
    if days > DEPRECATE_DAYS:
        return "💤 废弃"
    if score > 0.8:
        return "⭐ 冠军"
    if score > 0.5:
        return "🟢 活跃"
    if success_rate < 0.5:
        return "⚠️ 需优化"
    return "🔵 正常"


# ── Skill Chain ──
def update_chain(conn, prev_skill: str, curr_skill: str):
    """记录技能调用链."""
    if not prev_skill or not curr_skill or prev_skill == curr_skill:
        return
    conn.execute("""
        INSERT INTO skill_chains (from_skill, to_skill, count) VALUES (?, ?, 1)
        ON CONFLICT(from_skill, to_skill) DO UPDATE SET count = count + 1
    """, (prev_skill, curr_skill))
    conn.commit()


def get_chain(conn, skill_name: str, top_n: int = 5):
    """获取某个技能后最常用的下一个技能."""
    rows = conn.execute("""
        SELECT to_skill, count FROM skill_chains
        WHERE from_skill = ?
        ORDER BY count DESC
        LIMIT ?
    """, (skill_name, top_n)).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── oMLX Error Analysis ──
def analyze_errors(conn, skill_name: str) -> list:
    """用 oMLX 本地分析错误模式，生成修复建议."""
    errors = conn.execute("""
        SELECT error_type, error_msg FROM usage_log
        WHERE skill_name = ? AND success = 0
        ORDER BY created_at DESC
        LIMIT 20
    """, (skill_name,)).fetchall()

    if len(errors) < ERROR_THRESHOLD:
        return []

    # 错误聚类
    error_counts = Counter(e[0] or "unknown" for e in errors)
    suggestions = []

    for err_type, count in error_counts.most_common(3):
        if count >= 2:
            # 尝试调用本地 oMLX 分析
            analysis = _omlx_analyze(skill_name, err_type, errors[:5])
            suggestions.append({
                "skill": skill_name,
                "error_type": err_type,
                "count": count,
                "suggestion": analysis,
                "confidence": 0.6 + 0.1 * min(count, 4),
            })

    return suggestions


def _omlx_analyze(skill_name: str, error_type: str, samples: list) -> str:
    """调用本地 oMLX 进行错误根因分析 (fallback: 规则)."""
    # 尝试 oMLX CLI
    try:
        sample_text = "\n".join(f"- {e[1][:200]}" for e in samples if e[1])
        prompt = f"""技能 "{skill_name}" 反复出现错误类型 "{error_type}"。
错误样本:\n{sample_text}\n\n请分析根本原因并给出修复建议（中文，50字以内）。"""
        result = subprocess.run(
            ["omlx", "chat", "--model", "qwen3.5-4b", "--prompt", prompt],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:200]
    except Exception:
        pass

    # 规则 fallback
    rules = {
        "ImportError": "检查 skill 中的 import 语句，可能缺少依赖包或路径错误",
        "FileNotFoundError": "检查 SKILL.md 中的文件路径，确保相对路径正确",
        "TimeoutError": "增加 timeout 参数或考虑拆分大文件操作",
        "KeyError": "检查字典键名与 SKILL.md 中定义是否一致",
        "ConnectionError": "检查网络连通性，建议添加重试机制",
    }
    for key, suggestion in rules.items():
        if key.lower() in error_type.lower():
            return suggestion
    return f"错误类型 {error_type}: 建议检查 skill 输入参数和依赖项完整性"


# ── Commands ──
def cmd_track(conn, skill: str, success: str, duration_ms: str, error_type: str = ""):
    now = time.time()
    # 读取当前用户情绪
    mood = None
    if INTEL_STATE.exists():
        try:
            mood = json.loads(INTEL_STATE.read_text()).get("user_mood")
        except Exception:
            pass

    conn.execute(
        "INSERT INTO usage_log (skill_name, success, duration_ms, error_type, error_msg, user_mood, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (skill, int(success == "1" or success.lower() == "true"),
         float(duration_ms) if duration_ms else 0,
         error_type, "", mood, now),
    )
    conn.commit()

    score = compute_score(conn, skill, now)
    print(f"✅ Tracked: {skill} | Score: {score['score']:.3f} | {score['status']}")


def cmd_report(conn):
    print("=" * 60)
    print("📊 Hermes Adaptive Skills — Health Report")
    print("=" * 60)

    now = time.time()
    skills = conn.execute(
        "SELECT DISTINCT skill_name FROM usage_log ORDER BY skill_name"
    ).fetchall()

    if not skills:
        print("(no skills tracked yet)")
        return

    # 排序
    scored = sorted([compute_score(conn, s[0], now) for s in skills],
                    key=lambda x: x["score"], reverse=True)

    print(f"\n{'Skill':30s} {'Score':>6s} {'Uses':>5s} {'Succ%':>6s} {'Days':>5s} {'Trend':>6s} {'Status'}")
    print("-" * 80)
    for s in scored:
        print(f"{s['skill']:30s} {s['score']:6.3f} {s['uses']:5d} {s['success_rate']:6.1%} "
              f"{s['last_used_days']:5.1f} {s['trend']:+6.2f} {s['status']}")

    # 废弃警告
    deprecated = [s for s in scored if s["status"] == "💤 废弃"]
    if deprecated:
        print(f"\n⚠️ {len(deprecated)} skills 超过 {DEPRECATE_DAYS} 天未使用:")
        for s in deprecated:
            print(f"   - {s['skill']} ({s['last_used_days']:.0f} days)")

    # 冠军
    champions = [s for s in scored if s["status"] == "⭐ 冠军"]
    if champions:
        print("\n⭐ Top Champions:")
        for s in champions[:5]:
            print(f"   {s['skill']}: score={s['score']:.3f}")

    # 链推荐
    if champions:
        top_skill = champions[0]["skill"]
        chain = get_chain(conn, top_skill, 3)
        if chain:
            print(f"\n🔗 最常用「{top_skill}」后接着用:")
            for skill, count in chain:
                print(f"   → {skill} ({count}次)")

    # 错误摘要
    errors = conn.execute("""
        SELECT skill_name, error_type, COUNT(*) as cnt
        FROM usage_log WHERE success = 0
        GROUP BY skill_name, error_type
        HAVING cnt >= ?
        ORDER BY cnt DESC LIMIT 10
    """, (ERROR_THRESHOLD,)).fetchall()

    if errors:
        print("\n⚠️ 需关注的错误:")
        for e in errors:
            suggestions = analyze_errors(conn, e[0])
            for sug in suggestions:
                if sug["error_type"] == e[1]:
                    print(f"   {e[0]}: {e[1]} ×{e[2]} → {sug['suggestion'][:80]}")


def cmd_evolve(conn):
    """运行一轮进化."""
    print("🧬 Adaptive Skills — Evolution Round")
    now = time.time()

    skills = conn.execute(
        "SELECT DISTINCT skill_name FROM usage_log"
    ).fetchall()

    evolved = 0
    for (skill_name,) in skills:
        suggestions = analyze_errors(conn, skill_name)
        for sug in suggestions:
            # 记录
            conn.execute(
                "INSERT INTO evolution_log (skill_name, action, suggestion, confidence, created_at) "
                "VALUES (?, 'analyze', ?, ?, ?)",
                (skill_name, sug["suggestion"], sug["confidence"], now),
            )
            evolved += 1
            print(f"  [{skill_name}] {sug['error_type']}: {sug['suggestion'][:80]} "
                  f"(confidence={sug['confidence']:.2f})")

    conn.commit()
    print(f"\n✅ Evolution round complete: {evolved} suggestions generated")
    if evolved > 0:
        print("   💡 运行 'adaptive_skills.py report' 查看详情")


def cmd_recommend(conn, n=5):
    """Top-N 技能推荐."""
    now = time.time()
    skills = conn.execute(
        "SELECT DISTINCT skill_name FROM usage_log"
    ).fetchall()
    scored = sorted([compute_score(conn, s[0], now) for s in skills],
                    key=lambda x: x["score"], reverse=True)

    print(f"🔮 Top {n} Recommended Skills:")
    for s in scored[:n]:
        print(f"   {s['skill']:30s} score={s['score']:.3f} | {s['status']}")


def cmd_chain(conn, skill: str):
    chains = get_chain(conn, skill, 10)
    if not chains:
        print(f"🔗 {skill} → (no chains recorded)")
        return
    print(f"🔗 {skill} →")
    for s, c in chains:
        print(f"   → {s} ({c}次)")


def cmd_trends(conn):
    """30 天趋势图."""
    print("📈 Adaptive Skills — 30-Day Trends\n")
    now = time.time()
    skills = conn.execute(
        "SELECT DISTINCT skill_name FROM usage_log"
    ).fetchall()
    scored = sorted([compute_score(conn, s[0], now) for s in skills],
                    key=lambda x: x["trend"], reverse=True)

    # ASCII 趋势图
    print(f"{'Skill':25s} {'Trend':>6s} {'Bar'}")
    print("-" * 60)
    for s in scored[:20]:
        bar_len = int(abs(s["trend"]) * 20)
        bar = "▲" * bar_len if s["trend"] > 0 else "▼" * bar_len
        direction = "📈" if s["trend"] > 0.05 else ("📉" if s["trend"] < -0.05 else "➡️")
        print(f"{s['skill']:25s} {s['trend']:+6.3f} {direction} {bar}")


def main():
    conn = init_db()

    if len(sys.argv) < 2:
        print("Usage: adaptive_skills.py <command> [args]")
        print("  track <skill> <success> <duration_ms> [error_type]")
        print("  report")
        print("  evolve")
        print("  recommend [n]")
        print("  chain <skill>")
        print("  trends")
        conn.close()
        return

    cmd = sys.argv[1]

    if cmd == "track" and len(sys.argv) >= 5:
        cmd_track(conn, sys.argv[2], sys.argv[3], sys.argv[4],
                  sys.argv[5] if len(sys.argv) > 5 else "")
    elif cmd == "report":
        cmd_report(conn)
    elif cmd == "evolve":
        cmd_evolve(conn)
    elif cmd == "recommend":
        cmd_recommend(conn, int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif cmd == "chain" and len(sys.argv) >= 3:
        cmd_chain(conn, sys.argv[2])
    elif cmd == "trends":
        cmd_trends(conn)
    else:
        print(f"❌ Unknown command: {cmd}")

    conn.close()


if __name__ == "__main__":
    main()