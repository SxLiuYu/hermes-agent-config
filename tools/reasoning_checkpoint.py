#!/usr/bin/env python3
"""
P0-20: Long-chain Reasoning Checkpointer — Mythos-style 129-round resilience

对标:
  - Claude Mythos Preview: 129 轮 LLM 调用 + 154 次工具调用不掉链子
  - GraphWalks BFS 256K-1M: Mythos 在百万 token 上下文中做 BFS 不丢失信息
  - ExploitBench Case 1: 一年未解悬案，15 次尝试中持续学习

核心机制:
  1. 里程碑检查点 — 每 N 轮或关键操作后自动保存推理快照
  2. 推理健康度追踪 — 监控输出一致性、目标偏离度、重复循环检测
  3. 降级恢复 — 检测到质量下降时自动回滚到最近健康检查点
  4. 状态摘要注入 — 在长链中定期注入"已知状态摘要"防止上下文漂移

与现有组件的关系:
  - P0-13 context_health: 检测整体上下文健康（token/轮次/规则存活率）
  - P0-20 reasoning_checkpoint: 检测推理质量（一致性/目标对齐/循环检测）
  两者互补：健康监控关注"容器"，检查点关注"内容"
"""

import json
import os
import re
import time
import hashlib
from dataclasses import dataclass, field
from collections import defaultdict, deque
from typing import Optional

# ─── 数据存储 ────────────────────────────────────────────

CHECKPOINT_DIR = os.path.expanduser("~/.hermes/checkpoints")
os.makedirs(CHECKPOINT_DIR, exist_ok=True)


@dataclass
class Checkpoint:
    """推理状态快照"""
    id: str
    timestamp: float
    round_num: int
    goal: str                         # 当前目标
    subgoals_completed: list[str]     # 已完成的子目标
    subgoals_remaining: list[str]     # 剩余子目标
    known_facts: list[str]            # 已确认的事实
    open_questions: list[str]         # 待解决的疑问
    hypotheses_tested: list[dict]     # 已测试的假设 [{hypothesis, result, confidence}]
    errors_encountered: list[dict]    # 遇到的错误 [{error, fix, learned}]
    files_modified: list[str]         # 已修改的文件
    tools_used: list[str]             # 本轮使用的工具
    context_snapshot: str = ""        # 上下文摘要（最多 500 字符）
    health_score: float = 1.0         # 推理健康度 0-1


# ─── 健康度评估 ──────────────────────────────────────────


class ReasoningHealthMonitor:
    """监控推理质量，对标 Mythos 的 long-chain coherence"""

    def __init__(self, window_size: int = 10):
        self.window_size = window_size
        self.recent_outputs: deque = deque(maxlen=window_size)
        self.recent_actions: deque = deque(maxlen=window_size)
        self.goal: str = ""
        self.subgoals_history: list = []

    def set_goal(self, goal: str):
        self.goal = goal

    def observe(self, round_num: int, output: str, action: str,
                tools_used: list[str]):
        """每轮观察 Agent 输出"""
        self.recent_outputs.append({
            "round": round_num,
            "output": output[:200],
            "action": action[:100],
        })
        self.recent_actions.append({
            "round": round_num,
            "tools": tools_used,
        })

    def check_health(self) -> dict:
        """
        评估当前推理健康度。

        检测项:
        - 重复循环: 是否在重复相同的操作
        - 目标偏离: 是否偏离了初始目标
        - 输出一致性: 输出质量是否在下降
        - 停滞: 是否卡住没有任何进展
        """
        alerts = []
        score = 1.0

        if len(self.recent_actions) < 3:
            return {"score": score, "alerts": alerts, "status": "healthy"}

        # 1. 重复循环检测
        recent_tool_sets = [
            tuple(sorted(a["tools"])) for a in self.recent_actions
        ]
        # 检查最近 3 轮是否使用完全相同的工具组合
        if len(set(recent_tool_sets[-3:])) == 1 and len(recent_tool_sets) >= 3:
            score -= 0.3
            alerts.append({
                "type": "repeat_loop",
                "severity": "warning",
                "message": "最近 3 轮使用了完全相同的工具组合，可能陷入循环",
            })

        # 检查更严重的 5 轮循环
        if len(recent_tool_sets) >= 5:
            unique_last5 = len(set(recent_tool_sets[-5:]))
            if unique_last5 <= 2:
                score -= 0.2
                alerts.append({
                    "type": "severe_loop",
                    "severity": "critical",
                    "message": f"最近 5 轮只有 {unique_last5} 种工具组合，严重循环",
                })

        # 2. 输出长度衰减检测（可能表示模型"累了"）
        if len(self.recent_outputs) >= 5:
            output_lens = [len(o["output"]) for o in list(self.recent_outputs)[-5:]]
            first_half = sum(output_lens[:3]) / 3
            second_half = sum(output_lens[3:]) / 2
            if first_half > 0 and second_half / first_half < 0.3:
                score -= 0.2
                alerts.append({
                    "type": "output_decay",
                    "severity": "warning",
                    "message": f"输出长度衰减 {((1-second_half/first_half)*100):.0f}%，推理质量可能下降",
                })

        # 3. 目标相关性检测（简单关键词匹配）
        if self.goal and len(self.recent_outputs) >= 3:
            goal_keywords = set(re.findall(r"[\u4e00-\u9fff]{2,}", self.goal[:100]))
            recent_text = " ".join(o["output"] for o in list(self.recent_outputs)[-3:])
            recent_keywords = set(re.findall(r"[\u4e00-\u9fff]{2,}", recent_text))
            if goal_keywords:
                overlap = len(goal_keywords & recent_keywords) / len(goal_keywords)
                if overlap < 0.2:
                    score -= 0.3
                    alerts.append({
                        "type": "goal_drift",
                        "severity": "warning",
                        "message": f"目标关键词覆盖率仅 {overlap:.0%}，可能偏离目标",
                    })

        score = max(0, score)
        status = "healthy" if score > 0.7 else ("degrading" if score > 0.4 else "critical")

        return {"score": score, "alerts": alerts, "status": status}


# ─── 检查点管理 ───────────────────────────────────────────


class CheckpointManager:
    """管理推理检查点，对标 Mythos 的 long-chain state persistence"""

    def __init__(self, session_id: str = ""):
        self.session_id = session_id or hashlib.md5(
            str(time.time()).encode()
        ).hexdigest()[:8]
        self.checkpoints: list[Checkpoint] = []
        self.current_round = 0
        self.health_monitor = ReasoningHealthMonitor()
        self._file = os.path.join(
            CHECKPOINT_DIR, f"session_{self.session_id}.jsonl"
        )

    def set_goal(self, goal: str):
        self.health_monitor.set_goal(goal)

    def observe_round(self, output: str, action: str, tools_used: list[str]):
        self.current_round += 1
        self.health_monitor.observe(
            self.current_round, output, action, tools_used
        )

    def should_checkpoint(self, round_num: int = None,
                          force: bool = False) -> bool:
        """判断是否需要创建检查点"""
        if force:
            return True

        rn = round_num or self.current_round

        # 每 5 轮自动检查
        if rn % 5 == 0:
            return True

        # 健康度下降时立即检查
        health = self.health_monitor.check_health()
        if health["status"] in ("degrading", "critical"):
            return True

        return False

    def create_checkpoint(self, goal: str = "",
                          completed: list[str] = None,
                          remaining: list[str] = None,
                          facts: list[str] = None,
                          questions: list[str] = None,
                          hypotheses: list[dict] = None,
                          errors: list[dict] = None,
                          files_modified: list[str] = None,
                          tools_used: list[str] = None,
                          context_summary: str = "") -> Checkpoint:
        """创建推理快照"""

        health = self.health_monitor.check_health()

        cp = Checkpoint(
            id=f"cp_{self.current_round:04d}",
            timestamp=time.time(),
            round_num=self.current_round,
            goal=goal or self.health_monitor.goal,
            subgoals_completed=completed or [],
            subgoals_remaining=remaining or [],
            known_facts=facts or [],
            open_questions=questions or [],
            hypotheses_tested=hypotheses or [],
            errors_encountered=errors or [],
            files_modified=files_modified or [],
            tools_used=tools_used or [],
            context_snapshot=context_summary[:500],
            health_score=health["score"],
        )

        self.checkpoints.append(cp)

        # 持久化
        self._save(cp)

        return cp

    def get_latest_checkpoint(self) -> Optional[Checkpoint]:
        if self.checkpoints:
            return self.checkpoints[-1]
        return self._load_latest()

    def get_healthiest_checkpoint(self) -> Optional[Checkpoint]:
        """找到健康度最高的检查点（用于回滚）"""
        all_cps = self._load_all()
        if not all_cps:
            all_cps = self.checkpoints
        if not all_cps:
            return None
        return max(all_cps, key=lambda c: c.health_score)

    def generate_rollback_context(self) -> str:
        """
        从最近健康检查点生成回滚上下文。
        对标 Mythos: 在失败后带着累积知识重试。

        Returns: 可注入 Agent 上下文的回滚摘要
        """
        best = self.get_healthiest_checkpoint()
        latest = self.get_latest_checkpoint()

        if not best:
            return ""

        lines = [
            "## 推理状态恢复",
            f"从检查点 {best.id} (第 {best.round_num} 轮) 恢复",
            "",
        ]

        if best.goal:
            lines.append(f"**目标**: {best.goal}")

        if best.subgoals_completed:
            lines.append(f"**已完成**: {', '.join(best.subgoals_completed[:8])}")

        if best.subgoals_remaining:
            lines.append(f"**待完成**: {', '.join(best.subgoals_remaining[:5])}")

        if best.known_facts:
            lines.append(f"**已知事实**: {', '.join(best.known_facts[:5])}")

        if best.errors_encountered:
            lines.append("\n**已尝试的方法及结果**:")
            for err in best.errors_encountered[-5:]:
                lines.append(
                    f"- ❌ {err.get('error', '')[:80]}: "
                    f"{err.get('learned', '')[:80]}"
                )

        if best.hypotheses_tested:
            lines.append("\n**已测试的假设**:")
            for h in best.hypotheses_tested[-5:]:
                confidence = h.get("confidence", 0)
                icon = "✅" if confidence > 0.7 else ("⚠️" if confidence > 0.3 else "❌")
                lines.append(
                    f"- {icon} {h.get('hypothesis', '')[:80]} "
                    f"(结果: {h.get('result', '')[:60]})"
                )

        # 如果从最新检查点之后有尝试，说明失败历史
        if latest and best.round_num < latest.round_num:
            lines.append(
                f"\n⚠️ 检查点 {best.id} 之后进行了 "
                f"{latest.round_num - best.round_num} 轮尝试但未成功。"
                f"请基于以上已知信息尝试新方法，不要重复已失败的路径。"
            )

        return "\n".join(lines)

    def generate_state_summary(self, max_tokens: int = 500) -> str:
        """
        生成长链状态摘要——对标 Mythos 的上下文注入机制。
        在长对话中定期注入，防止上下文漂移。
        """
        latest = self.get_latest_checkpoint()
        if not latest:
            return ""

        # 紧凑格式
        parts = [f"[状态 R{latest.round_num}]"]
        if latest.subgoals_completed:
            done = len(latest.subgoals_completed)
            total = done + len(latest.subgoals_remaining)
            parts.append(f"进度:{done}/{total}")
        if latest.known_facts:
            parts.append(f"已知:{'; '.join(latest.known_facts[:3])}")
        if latest.errors_encountered:
            parts.append(f"已排除:{len(latest.errors_encountered)}种方案")
        if latest.health_score < 0.7:
            parts.append(f"⚠️质量:{latest.health_score:.0%}")

        return " | ".join(parts)

    # ─── 持久化 ───────────────────────────────────────────

    def _save(self, cp: Checkpoint):
        data = {
            "id": cp.id,
            "timestamp": cp.timestamp,
            "round_num": cp.round_num,
            "goal": cp.goal,
            "subgoals_completed": cp.subgoals_completed,
            "subgoals_remaining": cp.subgoals_remaining,
            "known_facts": cp.known_facts,
            "open_questions": cp.open_questions,
            "hypotheses_tested": cp.hypotheses_tested,
            "errors_encountered": cp.errors_encountered,
            "files_modified": cp.files_modified,
            "tools_used": cp.tools_used,
            "context_snapshot": cp.context_snapshot,
            "health_score": cp.health_score,
        }
        with open(self._file, "a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _load_all(self) -> list[Checkpoint]:
        if not os.path.exists(self._file):
            return []
        result = []
        with open(self._file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    result.append(Checkpoint(
                        id=d["id"],
                        timestamp=d["timestamp"],
                        round_num=d["round_num"],
                        goal=d.get("goal", ""),
                        subgoals_completed=d.get("subgoals_completed", []),
                        subgoals_remaining=d.get("subgoals_remaining", []),
                        known_facts=d.get("known_facts", []),
                        open_questions=d.get("open_questions", []),
                        hypotheses_tested=d.get("hypotheses_tested", []),
                        errors_encountered=d.get("errors_encountered", []),
                        files_modified=d.get("files_modified", []),
                        tools_used=d.get("tools_used", []),
                        context_snapshot=d.get("context_snapshot", ""),
                        health_score=d.get("health_score", 1.0),
                    ))
                except (json.JSONDecodeError, KeyError):
                    continue
        return result

    def _load_latest(self) -> Optional[Checkpoint]:
        all_cps = self._load_all()
        return all_cps[-1] if all_cps else None


# ─── 分析工具 ─────────────────────────────────────────────


def analyze_session(session_id: str) -> dict:
    """分析一个会话的推理轨迹"""
    mgr = CheckpointManager(session_id)
    cps = mgr._load_all()

    if not cps:
        return {"error": "No checkpoints found"}

    health_scores = [cp.health_score for cp in cps]
    rounds = [cp.round_num for cp in cps]

    # 检测健康度趋势
    trend = "stable"
    if len(health_scores) >= 3:
        first_half = sum(health_scores[: len(health_scores) // 2]) / max(
            len(health_scores) // 2, 1
        )
        second_half = sum(health_scores[len(health_scores) // 2 :]) / max(
            len(health_scores) - len(health_scores) // 2, 1
        )
        if second_half - first_half > 0.1:
            trend = "improving"
        elif first_half - second_half > 0.1:
            trend = "degrading"

    # 统计错误
    all_errors = []
    for cp in cps:
        all_errors.extend(cp.errors_encountered)

    return {
        "session_id": session_id,
        "total_checkpoints": len(cps),
        "total_rounds": rounds[-1] if rounds else 0,
        "health_trend": trend,
        "avg_health": round(sum(health_scores) / max(len(health_scores), 1), 3),
        "min_health": round(min(health_scores), 3) if health_scores else 0,
        "total_errors": len(all_errors),
        "subgoals_completed": sum(len(cp.subgoals_completed) for cp in cps),
        "degradation_events": sum(
            1 for cp in cps if cp.health_score < 0.5
        ),
    }


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  reasoning_checkpoint.py create <session_id> <round> <goal> [completed] [remaining]")
        print("  reasoning_checkpoint.py health <session_id>")
        print("  reasoning_checkpoint.py rollback <session_id>")
        print("  reasoning_checkpoint.py summary <session_id>")
        print("  reasoning_checkpoint.py analyze <session_id>")
        print("  reasoning_checkpoint.py latest <session_id>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "create":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        rn = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        goal = sys.argv[4] if len(sys.argv) > 4 else ""
        completed = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else []
        remaining = sys.argv[6].split(",") if len(sys.argv) > 6 and sys.argv[6] else []

        mgr = CheckpointManager(sid)
        mgr.current_round = rn
        cp = mgr.create_checkpoint(
            goal=goal,
            completed=completed,
            remaining=remaining,
        )
        print(json.dumps({"id": cp.id, "round": cp.round_num,
                          "health": cp.health_score}, ensure_ascii=False))

    elif cmd == "health":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        mgr = CheckpointManager(sid)
        health = mgr.health_monitor.check_health()
        print(json.dumps(health, indent=2, ensure_ascii=False))

    elif cmd == "rollback":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        mgr = CheckpointManager(sid)
        context = mgr.generate_rollback_context()
        print(context)

    elif cmd == "summary":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        mgr = CheckpointManager(sid)
        print(mgr.generate_state_summary())

    elif cmd == "analyze":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        result = analyze_session(sid)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "latest":
        sid = sys.argv[2] if len(sys.argv) > 2 else "default"
        mgr = CheckpointManager(sid)
        cp = mgr.get_latest_checkpoint()
        if cp:
            print(f"Checkpoint {cp.id}: round={cp.round_num}, "
                  f"health={cp.health_score:.2f}")
            if cp.subgoals_completed:
                print(f"  Completed: {', '.join(cp.subgoals_completed)}")
            if cp.errors_encountered:
                print(f"  Errors: {len(cp.errors_encountered)}")
        else:
            print("No checkpoints found")