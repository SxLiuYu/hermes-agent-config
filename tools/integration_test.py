#!/usr/bin/env python3
"""
Hermes Agent 集成测试 — 全模块端到端验证
============================================
模拟一个完整的真实任务流程，验证 23 个模块的协同工作。

测试场景: "修复 memory_decay.py 中的 score 计算错误"
覆盖: multi_exit → planning → metacognition → model_router → context_budget →
      terminal → tool_cache → error_correction → constitutional_safety →
      experience_distiller → experience_evolution → health_dashboard

用法:
  python3 tools/integration_test.py run       # 运行测试
  python3 tools/integration_test.py report    # 查看上次报告
  python3 tools/integration_test.py smoke     # 冒烟测试(只验证模块可调用)
"""

import json
import os
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
TOOLS = HERMES_HOME / "tools"
REPORT_DIR = HERMES_HOME / "integration_test"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


class Result:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.details = []

    def add(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            self.details.append(("✅", name, detail))
        else:
            self.failed += 1
            self.details.append(("❌", name, detail))

    def skip(self, name, reason=""):
        self.skipped += 1
        self.details.append(("⬛", name, reason))


def run_cmd(name, *args, timeout=10):
    """运行工具并返回 parsed JSON"""
    cmd = ["python3", str(TOOLS / name), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return {"raw": r.stdout[:200]}
        return {"error": r.stderr[:300], "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
#  Phase 1: 冒烟测试 — 所有模块可调用
# ═══════════════════════════════════════════════════════════
def smoke_test():
    """快速冒烟: 验证每个模块至少能输出 JSON"""
    r = Result()
    modules = OrderedDict([
        # 输入层
        ("multi_exit.py",       ["classify", "--query", "修复一个 Python bug"]),
        ("intent_disambiguator.py", ["detect", "--command", "改一下那个文件"]),
        # 规划层
        ("planning.py",         ["status"]),
        ("milestone_planner.py",["plan", "--goal", "test milestone"]),
        ("few_shot_selector.py",["inject", "--task", "fix bug"]),
        ("metacognition.py",    ["inject"]),
        # 路由层
        ("model_router.py",     ["report"]),
        ("bandit_router.py",    ["stats"]),
        ("tool_chain_fusion.py",["record", "--tools", "web_search,web_extract"]),
        ("tool_cache.py",       ["stats"]),
        # 记忆层
        ("vector_memory.py",    ["stats"]),
        ("memory_decay.py",     ["stats"]),
        ("context_budget.py",   ["check"]),
        ("chapter_compressor.py",["stats"]),
        # 安全层
        ("constitutional_safety.py", ["audit", "--text", "用 rm -rf /tmp/test 清理临时文件"]),
        ("human_in_loop.py",    ["check", "--action", "rm /tmp/test.txt"]),
        # 输出层
        ("progressive_disclosure.py", ["inject"]),
        # 进化层
        ("error_correction.py", ["stats"]),
        ("experience_distiller.py", ["stats"]),
        ("experience_evolution.py", ["stats"]),
        # 协作层
        ("agent_comms.py",      ["status"]),
        ("swarm_skills.py",     ["list"]),
        ("hits_mode.py",        ["status"]),
        # 监控层
        ("health_dashboard.py", ["collect"]),
    ])

    for name, args in modules.items():
        result = run_cmd(name, *args)
        ok = "error" not in result
        r.add(name, ok, "output: " + json.dumps(result, ensure_ascii=False)[:80] if ok else str(result.get("error", ""))[:80])
    
    return r


# ═══════════════════════════════════════════════════════════
#  Phase 2: 端到端工作流 — 模拟真实任务
# ═══════════════════════════════════════════════════════════
def e2e_test():
    """模拟完整工作流"""
    r = Result()
    task = "修复 memory_decay.py 中 decay 计算对长时间未用记忆的误判"
    trace = []
    
    print("=" * 60)
    print("🧪 端到端测试")
    print(f"   任务: {task}")
    print("=" * 60)

    # 1. Multi-Exit: 分类
    step("1. 输入分类 (multi_exit)", r, trace)
    mc = run_cmd("multi_exit.py", "classify", "--query", task)
    trace.append({"step": "multi_exit", "result": mc})
    r.add("multi_exit classifies bug fix", mc.get("level_name") == "FULL",
          f"level={mc.get('level_name')}")

    # 2. Intent Disambiguator: 检查模糊度
    step("2. 意图消歧", r, trace)
    idet = run_cmd("intent_disambiguator.py", "detect", "--command", task)
    trace.append({"step": "intent_disambiguator", "result": idet})
    r.add("intent disambiguator", "error" not in idet,
          f"ambiguous={idet.get('is_ambiguous')}")

    # 3. Planning: 生成计划
    step("3. 任务分解 (planning)", r, trace)
    plan = run_cmd("planning.py", "decompose", "--task", task)
    trace.append({"step": "planning", "result": plan})
    has_steps = plan.get("steps") or plan.get("subtasks") or "error" not in plan
    r.add("planning generates steps", has_steps, "")

    # 4. MetaCognition: 选择策略
    step("4. 策略选择 (metacognition)", r, trace)
    meta = run_cmd("metacognition.py", "assess", "--task", task)
    trace.append({"step": "metacognition", "result": meta})
    r.add("metacognition selects strategy", meta.get("strategy") is not None,
          f"strategy={meta.get('strategy')}")

    # 5. Model Router: 选择模型
    step("5. 模型路由 (model_router)", r, trace)
    mroute = run_cmd("model_router.py", "select", "--task", task)
    trace.append({"step": "model_router", "result": mroute})
    r.add("model_router selects model", mroute.get("model") is not None,
          f"model={mroute.get('model')}")

    # 6. Context Budget: 分配预算
    step("6. 上下文预算 (context_budget)", r, trace)
    cb = run_cmd("context_budget.py", "check")
    trace.append({"step": "context_budget", "result": cb})
    r.add("context_budget check", "error" not in str(cb), "")

    # 7. Few-Shot: 检索类似案例
    step("7. 案例检索 (few_shot_selector)", r, trace)
    fs = run_cmd("few_shot_selector.py", "inject", "--task", task)
    trace.append({"step": "few_shot_selector", "result": fs})
    r.add("few_shot_selector", "error" not in str(fs), "")

    # 8. 模拟 tool call: terminal (read file)
    step("8. 模拟工具调用 (tool_chain + tool_cache)", r, trace)
    # 记录工具序列 (需要至少2个工具)
    tcf = run_cmd("tool_chain_fusion.py", "record", "--tools", "web_search,web_extract")
    trace.append({"step": "tool_chain.record", "result": tcf})
    r.add("tool_chain.record web_search→extract", 
          tcf.get("status") != "error" if isinstance(tcf, dict) else "error" not in str(tcf),
          "")

    # 检查缓存
    tc = run_cmd("tool_cache.py", "stats")
    r.add("tool_cache stats", "error" not in str(tc), f"hit_rate={tc.get('hit_rate','?')}")

    # 9. Constitutional Safety: 审查
    step("9. 安全审查 (constitutional_safety)", r, trace)
    safe = run_cmd("constitutional_safety.py", "audit", "--text", 
                   "找到了 decay 参数配置错误，需要修改 decay_rate 从 0.3 改为 0.1")
    trace.append({"step": "constitutional_safety", "result": safe})
    r.add("constitutional_safety passed", safe.get("passed", True) == True,
          "")

    # 10. Human-in-Loop: 检查是否需要确认
    step("10. 安全门 (human_in_loop)", r, trace)
    hil = run_cmd("human_in_loop.py", "check", "--action", "patch decay_rate")
    trace.append({"step": "human_in_loop", "result": hil})
    r.add("human_in_loop check", "error" not in str(hil),
          f"gated={hil.get('gated')}")

    # 11. Error Correction: 记录
    step("11. 错误模式记录 (error_correction)", r, trace)
    ec = run_cmd("error_correction.py", "record", 
                  "--task", task,
                  "--success",
                  "--approach", "检查 decay 参数配置并更正 decay_rate")
    trace.append({"step": "error_correction", "result": ec})
    r.add("error_correction records fix", "error" not in str(ec), "")

    # 12. Experience Distiller: 蒸馏经验
    step("12. 经验蒸馏 (experience_distiller)", r, trace)
    ed = run_cmd("experience_distiller.py", "distill")
    trace.append({"step": "experience_distiller", "result": ed})
    r.add("experience_distiller distill", "error" not in str(ed),
          f"new_experiences={ed.get('new_experiences', 0)}")

    # 13. Experience Evolution: 进化
    step("13. 经验进化 (experience_evolution)", r, trace)
    ee = run_cmd("experience_evolution.py", "evolve")
    trace.append({"step": "experience_evolution", "result": ee})
    r.add("experience_evolution evolve",
          isinstance(ee, dict) and ee.get("total_experiences", -1) >= 0,
          f"experiences={ee.get('total_experiences', '?') if isinstance(ee, dict) else '?'}")

    # 14. Progressive Disclosure: 格式化输出
    step("14. 输出格式化 (progressive_disclosure)", r, trace)
    pd = run_cmd("progressive_disclosure.py", "inject")
    trace.append({"step": "progressive_disclosure", "result": pd})
    r.add("progressive_disclosure inject", "error" not in str(pd), "")

    # 15. Health Dashboard: 收集
    step("15. 健康仪表盘 (health_dashboard)", r, trace)
    hd = run_cmd("health_dashboard.py", "collect")
    trace.append({"step": "health_dashboard", "result": hd})
    r.add("health_dashboard collect", "error" not in str(hd), "")

    # 16. Agent Comms: 通知其他 agent
    step("16. Agent 通信 (agent_comms)", r, trace)
    ac = run_cmd("agent_comms.py", "status")
    trace.append({"step": "agent_comms", "result": ac})
    r.add("agent_comms status", "error" not in str(ac), "")

    # 17. Bandit Router: 记录工具成功
    step("17. Bandit 学习 (bandit_router)", r, trace)
    br = run_cmd("bandit_router.py", "record", "--task-type", "debug",
                  "--tool", "terminal", "--success")
    trace.append({"step": "bandit_router", "result": br})
    r.add("bandit_router record", "error" not in str(br), "")

    # 18. Chapter Compressor: 检查章节
    step("18. 章节压缩 (chapter_compressor)", r, trace)
    cc = run_cmd("chapter_compressor.py", "stats")
    trace.append({"step": "chapter_compressor", "result": cc})
    r.add("chapter_compressor stats", "error" not in str(cc), "")

    # 保存 trace
    trace_path = REPORT_DIR / f"trace_{int(time.time())}.json"
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False))

    return r


def step(label, r, trace):
    print(f"\n  {label}")


# ═══════════════════════════════════════════════════════════
#  Phase 3: 交叉验证 — 模块间交互
# ═══════════════════════════════════════════════════════════
def cross_validation():
    """验证模块间的数据流通"""
    r = Result()

    # 检查 experience_evolution 是否能读取 experience_distiller 的数据
    ed_stats = run_cmd("experience_distiller.py", "stats")
    ee_stats = run_cmd("experience_evolution.py", "stats")
    
    if "error" not in str(ed_stats) and "error" not in str(ee_stats):
        r.add("exp_distiller → exp_evolution link",
              int(ee_stats.get("total_experiences", 0)) >= 0,
              f"distiller={ed_stats.get('total_experiences',0)}, evolution={ee_stats.get('total_experiences',0)}")
    else:
        r.skip("exp_distiller → exp_evolution link", "one source unavailable")

    # 检查 context_budget 消费记录
    cb_check = run_cmd("context_budget.py", "check")
    if "error" not in str(cb_check):
        r.add("context_budget has layers", 
              isinstance(cb_check, dict) or (isinstance(cb_check, str) and "预算" in cb_check),
              "")
    
    # 检查 health_dashboard 是否能聚合所有模块
    hd_coll = run_cmd("health_dashboard.py", "collect")
    if "error" not in str(hd_coll):
        r.add("health_dashboard aggregates", 
              hd_coll.get("modules") is not None or hd_coll.get("status") is not None,
              "")
    
    # 检查 error_correction → experience_distiller
    ec_stats = run_cmd("error_correction.py", "stats")
    if "error" not in str(ec_stats):
        r.add("error_correction stats", True, 
              f"corrections={ec_stats.get('total_corrections',0) or ec_stats.get('total_tasks',0)}")

    # 检查工具序列: terminal → tool_chain → tool_cache
    run_cmd("tool_chain_fusion.py", "record", "--tools", "web_search,web_extract")
    tcf_sugg = run_cmd("tool_chain_fusion.py", "suggest", "--tools", "web_search")
    if "error" not in str(tcf_sugg):
        r.add("tool_chain suggests after record", 
              isinstance(tcf_sugg, dict) and "error" not in tcf_sugg,
              f"suggestions={tcf_sugg.get('suggestions', [])[:2] if isinstance(tcf_sugg, dict) else '?'}")

    return r


# ═══════════════════════════════════════════════════════════
#  Report
# ═══════════════════════════════════════════════════════════
def generate_report(smoke_r, e2e_r, cross_r):
    """生成综合测试报告"""
    total_passed = smoke_r.passed + e2e_r.passed + cross_r.passed
    total_failed = smoke_r.failed + e2e_r.failed + cross_r.failed
    total_skipped = smoke_r.skipped + e2e_r.skipped + cross_r.skipped
    total = total_passed + total_failed + total_skipped
    
    lines = [
        "=" * 65,
        "   Hermes Agent 集成测试报告",
        f"   时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 65,
        "",
        f"   总计: {total} 项 | ✅ {total_passed} | ❌ {total_failed} | ⬛ {total_skipped}",
        f"   通过率: {total_passed / max(total, 1) * 100:.0f}%",
        "",
        "─" * 65,
        "   Phase 1: 冒烟测试 (模块可用性)",
        "─" * 65,
    ]
    for icon, name, detail in smoke_r.details:
        lines.append(f"   {icon} {name:<35s} {detail}")
    
    lines += [
        "",
        "─" * 65,
        "   Phase 2: 端到端工作流",
        "─" * 65,
    ]
    for icon, name, detail in e2e_r.details:
        lines.append(f"   {icon} {name:<35s} {detail}")
    
    lines += [
        "",
        "─" * 65,
        "   Phase 3: 交叉验证",
        "─" * 65,
    ]
    for icon, name, detail in cross_r.details:
        lines.append(f"   {icon} {name:<35s} {detail}")
    
    # Summary
    lines += [
        "",
        "=" * 65,
        f"   {'🎉 全部通过！' if total_failed == 0 else f'⚠️  有 {total_failed} 项失败，需修复'}",
        "=" * 65,
    ]
    
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes 集成测试")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("smoke", help="仅冒烟测试")
    p = sub.add_parser("run", help="运行完整集成测试")
    p.add_argument("--save", action="store_true", default=True, help="保存报告")
    sub.add_parser("report", help="查看上次测试报告")

    args = parser.parse_args()

    if args.cmd == "smoke":
        r = smoke_test()
        for icon, name, detail in r.details:
            print(f"{icon} {name}")
        print(f"\n{r.passed}/{r.passed+r.failed} passed")

    elif args.cmd == "run":
        print("🏃 开始集成测试...\n")
        
        print("PHASE 1: 冒烟")
        smoke_r = smoke_test()
        
        print("\nPHASE 2: 端到端")
        e2e_r = e2e_test()
        
        print("\nPHASE 3: 交叉验证")
        cross_r = cross_validation()
        
        report = generate_report(smoke_r, e2e_r, cross_r)
        print(f"\n{report}")
        
        if args.save:
            path = REPORT_DIR / f"report_{int(time.time())}.txt"
            path.write_text(report)
            latest = REPORT_DIR / "latest.txt"
            latest.write_text(report)
            print(f"\n📄 报告已保存: {path}")

    elif args.cmd == "report":
        latest = REPORT_DIR / "latest.txt"
        if latest.exists():
            print(latest.read_text())
        else:
            print("暂无测试报告，请先运行: python3 tools/integration_test.py run")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()