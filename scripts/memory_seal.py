#!/usr/bin/env python3
"""
Hermes Memory Seal Pipeline v2
==============================
canonicalize -> chunk -> score -> link -> seal -> compress -> prune

升级:
  - 优先级评分: 融合 conversation_intel 话题重要性
  - 自动链接: 检测同主题/相邻时间的块关系
  - 过期清理: 7天+低分块自动删除
  - 记忆压缩: 合并相关 sealed 块为摘要

用法:
  python3 memory_seal.py ingest        # 摄入+评分
  python3 memory_seal.py seal          # 封印高分块
  python3 memory_seal.py compress      # 压缩关联记忆
  python3 memory_seal.py prune         # 清理过期低分块
  python3 memory_seal.py status        # 管线状态
  python3 memory_seal.py query <kw>    # 搜索
"""

import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB_PATH = HERMES_HOME / "memory_seal.db"
INTEL_DB = HERMES_HOME / "conversation_intel.db"
OBSIDIAN_CHUNKS = Path.home() / "obsidian-vault" / "Chunks"
OBSIDIAN_SEALED = Path.home() / "obsidian-vault" / "Sealed"

SOURCE_WEIGHTS = {
    "feishu": 1.0, "weixin": 1.0, "auto-fetch": 0.7,
    "trigger": 0.8, "manual": 0.9, "system": 0.5,
}
DECAY_HALF_LIFE = 72
MAX_CHUNK_TOKENS = 2500
PRUNE_AGE_DAYS = 7
PRUNE_SCORE_THRESHOLD = 0.2
LINK_TIME_THRESHOLD = 3600  # 1h window for auto-linking


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY, source TEXT, topic TEXT, content TEXT, summary TEXT,
        tokens INTEGER DEFAULT 0, score REAL DEFAULT 0, freshness REAL DEFAULT 1.0,
        importance REAL DEFAULT 0.5, relevance REAL DEFAULT 0.5,
        state TEXT DEFAULT 'draft', created_at REAL, scored_at REAL, sealed_at REAL,
        parent_id TEXT, obsidian_path TEXT, compressed_at REAL
    )""")
    # Add column if missing from old schema
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN compressed_at REAL")
    except:
        pass
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN summary TEXT")
    except:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS chunk_links (
        chunk_a TEXT, chunk_b TEXT, relation TEXT, weight REAL DEFAULT 1.0,
        PRIMARY KEY (chunk_a, chunk_b)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS topics (
        name TEXT PRIMARY KEY, chunk_count INTEGER DEFAULT 0,
        avg_score REAL DEFAULT 0, last_updated REAL, importance REAL DEFAULT 0.5
    )""")
    try:
        conn.execute("ALTER TABLE topics ADD COLUMN importance REAL DEFAULT 0.5")
    except:
        pass
    conn.commit()
    return conn


def est_tokens(text):
    return max(1, math.ceil(len(text) / 4))


def canonicalize(text):
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = []
    for line in text.split('\n'):
        lines.append(line[:497] + '...' if len(line) > 500 else line)
    return '\n'.join(lines).strip()


def extract_topic(content, source):
    cl = content.lower()
    kw = {
        "ai": ["ai","llm","gpt","claude","模型","大模型"],
        "stocks": ["股票","a股","涨停","跌停","复盘","k线"],
        "code": ["代码","python","bug","部署","github","api"],
        "smart_home": ["智能家居","贾维斯","红外","ha","传感器"],
        "meeting": ["会议","meeting","腾讯会议","纪要"],
        "deploy": ["部署","服务器","阿里云","docker","nginx"],
        "trigger": ["触发","告警","alert","紧急"],
        "memory": ["记忆","memory","备忘","提醒"],
        "voice": ["语音","唤醒","stt","tts","whisper"],
    }
    scores = {}
    for t, words in kw.items():
        s = sum(1 for w in words if w in cl)
        if s > 0: scores[t] = s
    return max(scores, key=scores.get) if scores else source


def get_topic_importance():
    """Get topic importance scores from conversation_intel.db."""
    if not INTEL_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(INTEL_DB))
        rows = conn.execute("""SELECT topic_a, topic_b, cooccurrence
            FROM topic_graph ORDER BY cooccurrence DESC LIMIT 50""").fetchall()
        conn.close()
        imp = defaultdict(float)
        for a, b, cooc in rows:
            imp[a] += cooc * 0.1
            imp[b] += cooc * 0.1
        # Normalize
        if imp:
            mx = max(imp.values())
            for k in imp:
                imp[k] = min(1.0, imp[k] / mx * 0.5 + 0.5)
        return dict(imp)
    except:
        return {}


def score_chunk(chunk_id, source, content, created_at):
    now = time.time()
    age_h = (now - created_at) / 3600
    freshness = math.exp(-age_h * math.log(2) / DECAY_HALF_LIFE)
    sw = SOURCE_WEIGHTS.get(source, 0.5)
    tokens = est_tokens(content)
    density = min(1.0, tokens / 500)
    importance = sw * (0.5 + 0.5 * density)
    has_urls = bool(re.search(r'https?://', content))
    has_code = bool(re.search(r'```', content))
    has_nums = bool(re.search(r'\d{2,}', content))
    relevance = 0.5 + (0.1 if has_urls else 0) + (0.15 if has_code else 0) + (0.1 if has_nums else 0)
    # Topic importance boost
    topic = extract_topic(content, source)
    topic_imp = get_topic_importance()
    topic_boost = topic_imp.get(topic, 0.5)
    relevance *= (0.8 + 0.2 * topic_boost)
    score = freshness * importance * relevance
    return {"freshness": round(freshness,4), "importance": round(importance,4),
            "relevance": round(relevance,4), "score": round(score,4), "tokens": tokens}


def chunk_text(content):
    if est_tokens(content) <= MAX_CHUNK_TOKENS:
        return [content]
    paragraphs = content.split('\n\n')
    chunks, current = [], ""
    for p in paragraphs:
        if est_tokens(current + '\n\n' + p) <= MAX_CHUNK_TOKENS:
            current = (current + '\n\n' + p).strip()
        else:
            if current: chunks.append(current)
            current = p
    if current: chunks.append(current)
    return chunks


def ingest_from_source(conn, source, data, timestamp=None):
    if timestamp is None: timestamp = time.time()
    canonical = canonicalize(data)
    if not canonical or len(canonical) < 20: return 0
    chunks_list = chunk_text(canonical)
    count = 0
    for i, chunk_content in enumerate(chunks_list):
        cid = f"{source}_{int(timestamp)}_{i}"
        topic = extract_topic(chunk_content, source)
        scored = score_chunk(cid, source, chunk_content, timestamp)
        conn.execute("""INSERT OR REPLACE INTO chunks
            (id,source,topic,content,tokens,score,freshness,importance,relevance,state,created_at,scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,'scored',?,?)""",
            (cid,source,topic,chunk_content,scored["tokens"],scored["score"],
             scored["freshness"],scored["importance"],scored["relevance"],timestamp,time.time()))
        conn.execute("""INSERT INTO topics (name,chunk_count,avg_score,last_updated,importance)
            VALUES (?,1,?,?,?) ON CONFLICT(name) DO UPDATE SET
            chunk_count=chunk_count+1, avg_score=(avg_score*chunk_count+?)/(chunk_count+1),
            last_updated=?, importance=\?""",
            (topic,scored["score"],time.time(),0.5,scored["score"],time.time(),0.5))
        count += 1
    conn.commit()
    return count


def ingest_all(conn):
    print("Memory Seal v2 - Ingest\n")
    if OBSIDIAN_CHUNKS.exists():
        for f in sorted(OBSIDIAN_CHUNKS.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
            if f.stat().st_mtime > time.time() - 7200:
                content = f.read_text()
                source = f.stem.split('_')[0] if '_' in f.stem else "chunk"
                n = ingest_from_source(conn, source, content, f.stat().st_mtime)
                if n > 0: print(f"  {f.name} -> {n} chunk(s)")
    total = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='scored'").fetchone()[0]
    print(f"\n  Total scored: {total}")
    return total


def auto_link_chunks(conn):
    """Auto-link chunks with same topic and close timestamps."""
    rows = conn.execute("""SELECT id, topic, created_at FROM chunks
        WHERE state IN ('scored','sealed') ORDER BY topic, created_at""").fetchall()
    if len(rows) < 2: return 0
    links = 0
    groups = defaultdict(list)
    for cid, topic, ts in rows:
        groups[topic].append((cid, ts))
    for topic, items in groups.items():
        items.sort(key=lambda x: x[1])
        for i in range(len(items)-1):
            if abs(items[i+1][1] - items[i][1]) < LINK_TIME_THRESHOLD:
                conn.execute("""INSERT OR IGNORE INTO chunk_links (chunk_a, chunk_b, relation)
                    VALUES (?,?,'temporal')""", (min(items[i][0],items[i+1][0]), max(items[i][0],items[i+1][0])))
                links += 1
    conn.commit()
    return links


def seal_chunks(conn, min_score=0.5, max_age_h=24):
    cutoff = time.time() - (max_age_h * 3600)
    rows = conn.execute("""SELECT id,source,topic,content,score FROM chunks
        WHERE state='scored' AND score>=? AND created_at<=? ORDER BY score DESC LIMIT 50""",
        (min_score, cutoff)).fetchall()
    sealed = 0
    for cid, source, topic, content, score in rows:
        conn.execute("UPDATE chunks SET state='sealed',sealed_at=? WHERE id=?", (time.time(), cid))
        OBSIDIAN_SEALED.mkdir(parents=True, exist_ok=True)
        ts = datetime.fromtimestamp(time.time()).strftime("%Y%m%d_%H%M%S")
        sf = OBSIDIAN_SEALED / f"{ts}_{source}_{cid[:8]}.md"
        sf.write_text(f"---\nchunk_id: {cid}\nsource: {source}\ntopic: {topic}\nscore: {score:.4f}\nsealed_at: {datetime.now().isoformat()}\n---\n\n# Sealed Memory - {topic}\n\n{content}\n")
        sealed += 1
    conn.commit()
    print(f"Sealed {sealed} chunks (score >={min_score})")
    return sealed


def compress_memories(conn, min_chunks=3):
    """Compress linked sealed chunks into a summary chunk."""
    # Find groups of linked sealed chunks
    links = conn.execute("""SELECT chunk_a, chunk_b FROM chunk_links""").fetchall()
    if not links:
        print("No links found for compression.")
        return 0

    # Build connected components
    adj = defaultdict(set)
    for a, b in links:
        adj[a].add(b); adj[b].add(a)

    visited = set()
    compressed = 0
    for node in list(adj.keys()):
        if node in visited: continue
        # BFS
        component = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n in visited: continue
            visited.add(n)
            component.append(n)
            stack.extend(adj[n])

        if len(component) >= min_chunks:
            # Get content from all chunks in component
            cids = ",".join(f"'{c}'" for c in component)
            rows = conn.execute(f"""SELECT id, source, topic, content, score FROM chunks
                WHERE id IN ({cids}) AND state='sealed'""").fetchall()
            if len(rows) < min_chunks: continue

            source = rows[0][1]
            topic = rows[0][2]
            avg_score = sum(r[4] for r in rows) / len(rows)

            # Create summary chunk
            summary = f"# Compressed Memory: {topic}\n\n"
            summary += f"*{len(rows)} related chunks merged*\n\n"
            for r in rows:
                summary += f"## Entry\n{r[3][:500]}\n\n---\n\n"

            cid = f"compressed_{topic}_{int(time.time())}"
            conn.execute("""INSERT OR REPLACE INTO chunks
                (id,source,topic,content,summary,tokens,score,state,created_at,compressed_at)
                VALUES (?,?,?,\?\,'compressed',\?,\?,'compressed',\?,\?)""",
                (cid, source, topic, "", summary, est_tokens(summary), avg_score, time.time(), time.time()))

            # Mark originals as compressed
            for r in rows:
                conn.execute("UPDATE chunks SET state='compressed',compressed_at=? WHERE id=?",
                    (time.time(), r[0]))
            compressed += 1

    conn.commit()
    print(f"Compressed {compressed} memory groups")
    return compressed


def prune_expired(conn):
    """Delete chunks older than PRUNE_AGE_DAYS with score < threshold."""
    cutoff = time.time() - (PRUNE_AGE_DAYS * 86400)
    rows = conn.execute("""SELECT id, score, created_at FROM chunks
        WHERE created_at < ? AND score < ? AND state != 'compressed'""",
        (cutoff, PRUNE_SCORE_THRESHOLD)).fetchall()
    for cid, score, ts in rows:
        conn.execute("DELETE FROM chunks WHERE id=?", (cid,))
        conn.execute("DELETE FROM chunk_links WHERE chunk_a=? OR chunk_b=?", (cid, cid))
    conn.commit()
    pruned = len(rows)
    print(f"Pruned {pruned} expired low-score chunks")
    return pruned


def query_chunks(conn, keyword, limit=10):
    rows = conn.execute("""SELECT id,source,topic,score,state,content,created_at FROM chunks
        WHERE content LIKE ? ORDER BY score DESC LIMIT ?""", (f'%{keyword}%', limit)).fetchall()
    print(f"\nSearch: \"{keyword}\"\n")
    for cid, source, topic, score, state, content, ts in rows:
        t = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        print(f"  [{state:10s}] s={score:.2f} | {topic:12s} | {source:12s} | {t}")
        print(f"           {content[:120].replace(chr(10),' ')}...\n")


def show_status(conn):
    draft = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='draft'").fetchone()[0]
    scored = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='scored'").fetchone()[0]
    sealed = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='sealed'").fetchone()[0]
    compressed = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='compressed' or compressed_at IS NOT NULL").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM chunk_links").fetchone()[0]
    print("\nMemory Seal Pipeline v2\n")
    print(f"  Draft:      {draft:>6}")
    print(f"  Scored:     {scored:>6}")
    print(f"  Sealed:     {sealed:>6}")
    print(f"  Compressed: {compressed:>6}")
    print(f"  Links:      {links:>6}")
    print(f"  Total:      {draft+scored+sealed+compressed:>6}")
    topics = conn.execute("""SELECT name,chunk_count,avg_score,importance FROM topics ORDER BY avg_score DESC LIMIT 8""").fetchall()
    if topics:
        print("\n  Top Topics:")
        for name, cnt, avg, imp in topics:
            print(f"    {name:15s} {cnt:>4} chunks  score={avg:.2f}  imp={imp:.2f}")


def main():
    conn = init_db()
    if len(sys.argv) < 2:
        print("Usage: memory_seal.py <ingest|seal|compress|prune|status|query>")
        return
    cmd = sys.argv[1]
    if cmd == "ingest":
        ingest_all(conn)
        links = auto_link_chunks(conn)
        print(f"  Auto-linked: {links} pair(s)")
    elif cmd == "seal":
        min_score = float(sys.argv[2]) if len(sys.argv)>2 else 0.5
        seal_chunks(conn, min_score)
    elif cmd == "compress":
        compress_memories(conn)
    elif cmd == "prune":
        prune_expired(conn)
    elif cmd == "status":
        show_status(conn)
    elif cmd == "query":
        kw = sys.argv[2] if len(sys.argv)>2 else ""
        if kw: query_chunks(conn, kw)
        else: print("Provide a keyword.")
    conn.close()


if __name__ == "__main__":
    main()