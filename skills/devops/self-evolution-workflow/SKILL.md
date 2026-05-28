---
name: self-evolution-workflow
title: 自进化任务闭环 v2 完整工作流
description: mark→去重→Agent Bus→omp执行→collect评分→失败提取教训→自动重试(最多3次)→inject成功模式。全套异步架构最佳实践
category: devops
version: 1.0
depends_on: ["self_evolution.py", "agent_bus.py", "omp"]
---

# 自进化任务闭环 v2

> 异步任务执行 + 失败自学习 + 成功模式注入

## 触发条件
- "帮我自动执行一个复杂任务"、"用进化系统跑一下"
- "自进化"、"self evolution"、"mark一个任务"
- 排查进化系统问题时先查此文档

## 架构

```
mark 任务
  ↓ 去重检测（相同标题跳过）
Agent Bus (SQLite 任务表)
  ↓
executor (launchd, 30min间隔)
  ↓ omp run -p --no-session
结果文件
  ↓
collect (launchd, 5min间隔)
  ├── 孤儿检测 (>2h → 标记超时)
  ├── 评分 (FinnA 评分 0-10)
  ├── 失败→_extract_evo 提取教训
  ├── 自动重试 (最多3次)
  └── inject 输出成功模式+失败教训
      ↓
  session_context.md (下次任务注入)
```

## 关键脚本

| 脚本 | 路径 | 用途 |
|------|------|------|
| self_evolution.py | ~/.hermes/scripts/self_evolution.py | 主控脚本 |
| agent_bus.py | ~/.hermes/scripts/agent_bus.py | SQLite 任务表+任务锁 |
| outcomes_grader.py | ~/.hermes/scripts/outcomes_grader.py | FinnA 评分 |

## 命令速查

```bash
# 标记新任务
python3 ~/.hermes/scripts/self_evolution.py mark   --title "任务标题"   --context "详细任务描述"

# 执行（最多N个任务）
python3 ~/.hermes/scripts/self_evolution.py execute --max-tasks 1

# 收集结果 + 评分 + 自动重试
python3 ~/.hermes/scripts/self_evolution.py collect

# 查看状态
python3 ~/.hermes/scripts/self_evolution.py status

# 查看注入的进化上下文
python3 ~/.hermes/scripts/self_evolution.py inject
```

## 关键坑

### omp 不退出 / 挂起
**原因**：omp 默认交互模式，完成任务后等输入
**修复**：必须加 `-p --no-session`：`omp run -p --no-session "prompt"`
**验证**：`pgrep -fl omp` 确认没有僵尸进程

### 结果文件为空
**原因**：omp stdout 不 flush
**同上修复**

### sqlite3.Row 不支持 .get()
用 `row["key"]` 直接访问，不要用 `.get()`

## DB 表结构

表名: `evolution_tasks`
关键字段: id, status(pending/running/completed/failed), retry_count, max_retries(3), outcome_score, evolution_context, result_file

## launchd 配置

```bash
# executor: 每30分钟
~/.hermes/logs/evolution_executor.log

# collect: 每5分钟  
~/.hermes/logs/evolution_collect.log
```
