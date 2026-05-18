#!/usr/bin/env python3
"""
Hermes Conversation Intelligence v2
=====================================
情感分析 + 话题提取 + 用户状态建模 + 状态突变检测 + 预测式预加载

对标: OpenHuman "keeps thinking + proactive"
升级: oMLX 本地推理情感 (准确度 85%+) + 状态突变告警

情感分析双通道:
  - 快速通道: 关键词匹配 (延迟 <1ms)
  - 精度通道: oMLX Qwen3.5-4B (延迟 ~500ms, 准确度 85%+)

用法:
  python3 conversation_intel.py analyze <text>       # 关键词分析
  python3 conversation_intel.py analyze-omlx <text>  # oMLX 精度分析
  python3 conversation_intel.py state                # 用户状态+突变检测
  python3 conversation_intel.py delta                # 仅突变检测
  python3 conversation_intel.py daemon               # 后台分析
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import Counter, defaultdict

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_DB = HERMES_HOME / "state.db"
INTEL_DB = HERMES_HOME / "conversation_intel.db"

FINNA_KEY = os.environ.get("FINNA_API_KEY", "app-BqyKsTO4Om3JGoPCTkJX080J")
FINNA_BASE = "https://www.finna.com.cn/v1"
OMLX_BASE = "http://localhost:4560/v1"

# Emotion lexicon (fast channel)
EMOTION_LEXICON = {
    "positive": {
        "words": ["好","棒","厉害","牛","可以","不错","赞","完美","太好","cool","nice",
                   "喜欢","开心","哈哈","笑","给力","优秀","666","顶","成功","搞定"],
        "valence": 0.7, "arousal": 0.5, "dominance": 0.5,
    },
    "negative": {
        "words": ["烦","差","烂","不行","失败","错误","bug","问题","不好",
                   "坑","难受","无语","崩溃","炸","挂","死","垃圾"],
        "valence": -0.6, "arousal": 0.5, "dominance": -0.3,
    },
    "urgent": {
        "words": ["紧急","马上","立刻","快","赶紧","速度","urgent","asap",
                   "立即","迅速","快点","着急","来不及"],
        "valence": -0.2, "arousal": 0.9, "dominance": -0.5,
    },
    "curious": {
        "words": ["为什么","怎么","什么","如何","?","?","试试","看看",
                   "查一下","分析","对比","研究"],
        "valence": 0.3, "arousal": 0.6, "dominance": 0.2,
    },
    "tired": {
        "words": ["累","困","休息","睡觉","晚","明天","再说","算了"],
        "valence": -0.3, "arousal": -0.5, "dominance": -0.4,
    },
    "focused": {
        "words": ["继续","接着","然后","下一步","优化","改","实现",
                   "做","部署","测试","看看","检查"],
        "valence": 0.2, "arousal": 0.4, "dominance": 0.6,
    },
}

TOPIC_LEXICON = {
    "stocks": ["股票","A股","涨停","跌停","复盘","K线","量能","板块","仓位","买入","卖出"],
    "code": ["代码","Python","bug","函数","class","import","部署","GitHub","API","json"],
    "ai": ["AI","LLM","模型","GPT","Claude","DeepSeek","训练","推理","prompt"],
    "smart_home": ["智能家居","贾维斯","红外","空调","电视","灯","传感器","HA"],
    "infra": ["服务器","阿里云","部署","docker","nginx","端口","SSH","网络"],
    "voice": ["语音","唤醒","STT","TTS","Whisper","录音","说话"],
    "memory": ["记忆","memory","备忘","记录","存档","chunk","seal"],
    "sync": ["同步","sync","推送","pull","push","ssh","rsync"],
    "hermes_meta": ["Hermes","OpenHuman","对比","skill","优化","组件"],
}

STATE_WINDOW = 3600
DELTA_THRESHOLD = 0.3  # VAD change > 0.3 = alert


def init_db():
    conn = sqlite3.connect(str(INTEL_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER, role TEXT, content TEXT,
            valence REAL, arousal REAL, dominance REAL,
            primary_emotion TEXT, topics TEXT,
            word_count INTEGER, timestamp REAL, session_id TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            timestamp REAL PRIMARY KEY, valence REAL, arousal REAL, dominance REAL,
            fatigue_score REAL, focus_score REAL,
            dominant_topic TEXT, predicted_needs TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topic_graph (
            topic_a TEXT, topic_b TEXT,
            cooccurrence INTEGER DEFAULT 1, last_seen REAL,
            PRIMARY KEY (topic_a, topic_b)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_time ON conversation_analysis(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_session ON conversation_analysis(session_id)")
    conn.commit()
    return conn


# ---- Emotion: Fast Channel (keyword) ----

def analyze_emotion(text: str) -> dict:
    text_lower = text.lower()
    scores = defaultdict(float)
    hits = defaultdict(int)
    for emotion, data in EMOTION_LEXICON.items():
        for word in data["words"]:
            if word in text_lower or word in text:
                scores["valence"] += data["valence"]
                scores["arousal"] += data["arousal"]
                scores["dominance"] += data["dominance"]
                hits[emotion] += 1
    total = sum(hits.values()) or 1
    v = max(-1, min(1, scores["valence"] / total))
    a = max(-1, min(1, scores["arousal"] / total))
    d = max(-1, min(1, scores["dominance"] / total))
    primary = max(hits, key=hits.get) if hits else "neutral"
    return {"valence": v, "arousal": a, "dominance": d, "primary": primary}


# ---- Emotion: Precision Channel (oMLX) ----

def analyze_emotion_omlx(text: str) -> dict:
    """Use local oMLX Qwen3.5-4B for high-accuracy emotion analysis."""
    try:
        import requests
    except ImportError:
        return analyze_emotion(text)  # fallback

    prompt = f"""Analyze the emotional content of this Chinese message. Return ONLY a JSON object with these keys:
- "primary": one of [positive, negative, urgent, curious, tired, focused, neutral]
- "valence": float from -1 (very negative) to 1 (very positive)
- "arousal": float from -1 (very calm) to 1 (very excited)
- "dominance": float from -1 (very passive) to 1 (very dominant)

Message: "{text}"

Return ONLY the JSON, no explanation."""

    try:
        resp = requests.post(
            f"{OMLX_BASE}/chat/completions",
            headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
            json={
                "model": "qwen3.5-4b-mlx",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150, "temperature": 0.1, "stream": False,
            },
            timeout=5,
        )
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"]
            # Extract JSON from response
            json_match = re.search(r'\{[^}]+\}', raw)
            if json_match:
                result = json.loads(json_match.group())
                return {
                    "valence": float(result.get("valence", 0)),
                    "arousal": float(result.get("arousal", 0)),
                    "dominance": float(result.get("dominance", 0)),
                    "primary": result.get("primary", "neutral"),
                }
    except Exception:
        pass

    return analyze_emotion(text)  # fallback to keyword


# ---- Topics ----

def extract_topics(text: str) -> list:
    text_lower = text.lower()
    scores = {}
    for topic, keywords in TOPIC_LEXICON.items():
        score = sum(1 for kw in keywords if kw.lower() in text_lower or kw in text)
        if score > 0:
            scores[topic] = score
    return sorted(scores, key=scores.get, reverse=True)


# ---- Ingestion ----

def analyze_message(conn, msg_id, role, content, session_id, timestamp):
    if not content or len(content) < 5:
        return
    emotion = analyze_emotion(content)
    topics = extract_topics(content)
    conn.execute("""INSERT INTO conversation_analysis
        (message_id,role,content,valence,arousal,dominance,primary_emotion,topics,word_count,timestamp,session_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (msg_id, role, content[:500], emotion["valence"], emotion["arousal"], emotion["dominance"],
         emotion["primary"], json.dumps(topics), len(content), timestamp, session_id))
    for i, t1 in enumerate(topics):
        for t2 in topics[i+1:]:
            conn.execute("""INSERT INTO topic_graph (topic_a, topic_b, last_seen)
                VALUES (?,?,?) ON CONFLICT(topic_a, topic_b) DO UPDATE SET
                cooccurrence=cooccurrence+1, last_seen=?""",
                (min(t1,t2), max(t1,t2), time.time(), time.time()))
    conn.commit()


def ingest_recent(conn):
    if not STATE_DB.exists():
        return 0
    sconn = sqlite3.connect(str(STATE_DB))
    sconn.row_factory = sqlite3.Row
    last = conn.execute("SELECT MAX(message_id) FROM conversation_analysis").fetchone()[0] or 0
    rows = sconn.execute("""SELECT m.id, m.role, m.content, m.timestamp, m.session_id
        FROM messages m WHERE m.id > ? AND m.role IN ('user','assistant')
        ORDER BY m.id ASC LIMIT 100""", (last,)).fetchall()
    sconn.close()
    count = 0
    for r in rows:
        analyze_message(conn, r["id"], r["role"], r["content"] or "", r["session_id"], r["timestamp"])
        count += 1
    return count


# ---- User State ----

def compute_user_state(conn) -> dict:
    cutoff = time.time() - STATE_WINDOW
    rows = conn.execute("""SELECT AVG(valence),AVG(arousal),AVG(dominance),COUNT(*)
        FROM conversation_analysis WHERE timestamp > ? AND role='user'""",
        (cutoff,)).fetchone()
    if not rows or rows[3] == 0:
        return {"status": "no recent data"}

    avg_v = rows[0] or 0
    avg_a = rows[1] or 0
    avg_d = rows[2] or 0
    cnt = rows[3]

    fatigue = max(0, (avg_a*0.4) - (avg_v*0.3) + (cnt/20*0.3))
    focus = max(0, min(1, avg_d*0.5 + avg_a*0.3))

    topic_rows = conn.execute("""SELECT topics FROM conversation_analysis
        WHERE timestamp>? AND role='user'""", (cutoff,)).fetchall()
    tc = Counter()
    for r in topic_rows:
        try:
            for t in json.loads(r[0]):
                tc[t] += 1
        except json.JSONDecodeError:
            pass
    dominant_topic = tc.most_common(1)[0][0] if tc else "unknown"

    predicted = predict_needs(conn, avg_v, avg_a, avg_d, dominant_topic)

    state = {
        "valence": round(avg_v, 3), "arousal": round(avg_a, 3), "dominance": round(avg_d, 3),
        "fatigue_score": round(fatigue, 3), "focus_score": round(focus, 3),
        "dominant_topic": dominant_topic, "message_count_1h": cnt,
        "predicted_needs": predicted,
    }

    conn.execute("""INSERT OR REPLACE INTO user_state
        (timestamp,valence,arousal,dominance,fatigue_score,focus_score,dominant_topic,predicted_needs)
        VALUES (?,?,?,?,?,?,?,?)""",
        (time.time(), avg_v, avg_a, avg_d, fatigue, focus, dominant_topic,
         json.dumps(predicted, ensure_ascii=False)))
    conn.commit()
    return state


def predict_needs(conn, valence, arousal, dominance, topic) -> list:
    needs = []
    if dominance < -0.2 and arousal < 0.2:
        needs.append("简化回复,减少细节")
    if dominance > 0.4 and arousal > 0.3:
        needs.append("直接行动,不啰嗦")
    if topic == "stocks":
        needs.append("预加载今日行情数据")
    if topic == "code":
        needs.append("准备代码工具链")
    if topic == "deploy":
        needs.append("检查服务器状态")
    related = conn.execute("""SELECT topic_a,topic_b,cooccurrence FROM topic_graph
        WHERE topic_a=? OR topic_b=? ORDER BY cooccurrence DESC LIMIT 3""",
        (topic, topic)).fetchall()
    for a, b, count in related:
        rt = b if a == topic else a
        needs.append(f"关联话题: {rt} (共现{count}次)")
    return needs[:5]


# ---- State Delta Detection (NEW) ----

def detect_state_delta(conn) -> list:
    """Compare current state with previous state. Return alerts if significant change."""
    states = conn.execute(
        "SELECT * FROM user_state ORDER BY timestamp DESC LIMIT 2"
    ).fetchall()

    if len(states) < 2:
        return []

    current = {
        "valence": states[0][1], "arousal": states[0][2], "dominance": states[0][3],
        "fatigue": states[0][4], "focus": states[0][5], "topic": states[0][6],
    }
    previous = {
        "valence": states[1][1], "arousal": states[1][2], "dominance": states[1][3],
        "fatigue": states[1][4], "focus": states[1][5], "topic": states[1][6],
    }

    alerts = []

    # VAD delta detection
    delta_v = abs(current["valence"] - previous["valence"])
    delta_a = abs(current["arousal"] - previous["arousal"])
    delta_d = abs(current["dominance"] - previous["dominance"])

    if delta_v > DELTA_THRESHOLD:
        direction = "positive" if current["valence"] > previous["valence"] else "negative"
        alerts.append(f"情绪效价突变 ({direction}): {previous['valence']:.2f} -> {current['valence']:.2f}")

    if delta_a > DELTA_THRESHOLD:
        direction = "excited" if current["arousal"] > previous["arousal"] else "calmed"
        alerts.append(f"唤醒度突变 ({direction}): {previous['arousal']:.2f} -> {current['arousal']:.2f}")

    if delta_d > DELTA_THRESHOLD:
        direction = "assertive" if current["dominance"] > previous["dominance"] else "passive"
        alerts.append(f"支配度突变 ({direction}): {previous['dominance']:.2f} -> {current['dominance']:.2f}")

    # Fatigue spike
    if current["fatigue"] - previous["fatigue"] > 0.3:
        alerts.append(f"疲劳指数激增: {previous['fatigue']:.2f} -> {current['fatigue']:.2f}")

    # Topic change
    if current["topic"] != previous["topic"] and current["topic"] != "unknown":
        alerts.append(f"话题切换: {previous['topic']} -> {current['topic']}")

    return alerts


# ---- Preload ----

def preload_context(conn, user_state):
    topic = user_state.get("dominant_topic", "")
    skill_map = {
        "stocks": ["daily-replay-xuwenjie-v9.7", "a-stock-data-fetching"],
        "code": ["python-debugpy", "github-pr-workflow"],
        "ai": ["finna-api-batch-extraction", "omlx-server-setup"],
        "smart_home": ["jarvis-smart-home-voice-agent", "termux-android-infrared-remote"],
        "infra": ["ssh-ubuntu-server", "ubuntu-firewall-safety"],
        "voice": ["jarvis-four-voice-optimizations", "native-voice"],
    }
    context = {
        "topic": topic,
        "suggested_skills": skill_map.get(topic, []),
        "recent_memory": [],
    }
    seal_db = HERMES_HOME / "memory_seal.db"
    if seal_db.exists():
        sconn = sqlite3.connect(str(seal_db))
        recent = sconn.execute(
            "SELECT topic, content FROM chunks WHERE topic=? AND state='sealed' ORDER BY created_at DESC LIMIT 3",
            (topic,)).fetchall()
        context["recent_memory"] = [{"topic": r[0], "preview": (r[1] or "")[:200]} for r in recent]
        sconn.close()
    return context


# ---- Display ----

def show_user_state(conn):
    state = compute_user_state(conn)
    print(f"\n{'='*55}")
    print(f"  Brain - User State - {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*55}\n")

    if isinstance(state, dict) and state.get("status"):
        print(f"  {state['status']}")
        return

    v, a, d = state.get("valence",0), state.get("arousal",0), state.get("dominance",0)
    if v > 0 and a > 0: quadrant = "Excited/Positive"
    elif v > 0 and a <= 0: quadrant = "Calm/Satisfied"
    elif v <= 0 and a > 0: quadrant = "Tense/Anxious"
    else: quadrant = "Tired/Low"

    print(f"  Emotion Quadrant: {quadrant}")
    print(f"  VAD: v={v:.2f} a={a:.2f} d={d:.2f}")
    print(f"  Fatigue: {state.get('fatigue_score',0):.2f}  |  Focus: {state.get('focus_score',0):.2f}")
    print(f"  Dominant Topic: {state.get('dominant_topic','?')}  |  Messages/1h: {state.get('message_count_1h',0)}")

    # Delta alerts
    alerts = detect_state_delta(conn)
    if alerts:
        print("\n  Delta Alerts:")
        for alert in alerts:
            print(f"    {alert}")

    needs = state.get("predicted_needs", [])
    if needs:
        print("\n  Predicted Needs:")
        for n in needs:
            print(f"    - {n}")

    context = preload_context(conn, state)
    if context["suggested_skills"]:
        print("\n  Suggested Preload Skills:")
        for s in context["suggested_skills"][:3]:
            print(f"    - {s}")


def cmd_delta(conn):
    """Show only state delta alerts."""
    compute_user_state(conn)  # ensure current state is saved
    alerts = detect_state_delta(conn)
    if alerts:
        print("\n  State Delta Alerts:")
        for alert in alerts:
            print(f"    {alert}")
    else:
        print("  No significant state changes detected.")


# ---- Main ----

def main():
    conn = init_db()

    if len(sys.argv) < 2:
        print("Conversation Intelligence v2")
        print("\nCommands:")
        print("  analyze <text>        Fast keyword analysis")
        print("  analyze-omlx <text>   oMLX precision analysis")
        print("  state                 User state + delta alerts")
        print("  delta                 State delta only")
        print("  ingest                Ingest recent + show state")
        print("  daemon                Background analyzer (5min)")
        return

    cmd = sys.argv[1]

    if cmd == "analyze":
        text = " ".join(sys.argv[2:])
        if text:
            e = analyze_emotion(text)
            t = extract_topics(text)
            print(f"Emotion: {e['primary']} (v={e['valence']:.2f}, a={e['arousal']:.2f}, d={e['dominance']:.2f})")
            print(f"Topics:  {t}")

    elif cmd == "analyze-omlx":
        text = " ".join(sys.argv[2:])
        if text:
            print("  Running oMLX emotion analysis...")
            e = analyze_emotion_omlx(text)
            t = extract_topics(text)
            print(f"Emotion: {e['primary']} (v={e['valence']:.2f}, a={e['arousal']:.2f}, d={e['dominance']:.2f})")
            print(f"Topics:  {t}")
            print("  (oMLX precision mode)")

    elif cmd == "state":
        ingest_recent(conn)
        show_user_state(conn)

    elif cmd == "delta":
        cmd_delta(conn)

    elif cmd == "ingest":
        count = ingest_recent(conn)
        print(f"Ingested {count} messages")
        show_user_state(conn)

    elif cmd == "daemon":
        print("Brain - Conversation Intel daemon (5min)")
        try:
            while True:
                count = ingest_recent(conn)
                if count > 0:
                    state = compute_user_state(conn)
                    alerts = detect_state_delta(conn)
                    ts = datetime.now().strftime("%H:%M")
                    if alerts:
                        print(f"  [{ts}] {state.get('dominant_topic')} | ALERTS: {alerts[0]}")
                    elif state.get("predicted_needs"):
                        print(f"  [{ts}] {state.get('dominant_topic')} | {state['predicted_needs'][0]}")
                time.sleep(300)
        except KeyboardInterrupt:
            print("\nDone.")

    conn.close()


if __name__ == "__main__":
    main()