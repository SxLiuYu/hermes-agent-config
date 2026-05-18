# Hermes Agent 优化 Round 10 — 记忆衰减 + Skill 蒸馏飞轮

**完成时间**: 2026-05-18
**新增代码**: 1064 行 Python + 3 个 Hook

## 对标来源

| 论文/框架 | 核心思路 | 落地组件 |
|-----------|----------|----------|
| **Mem0 Memory Decay (2026.5)** | access_recency 加权排序，打破语义相似度的时间盲 | `memory_decay.py` |
| **LangMem procedural memory** | 从情景记忆中蒸馏可执行技能 | `skill_distiller.py` |
| **EverOS Cases→Skills 管线** | 跨 session 批量蒸馏 + 版本管理 | `skill_distiller.py` batch mode |

---

## 一、Memory Decay — `tools/memory_decay.py` (452 行)

### 功能
- **touch**: 标记记忆被访问，更新 `last_accessed` + `access_count`
- **touch-query**: 根据查询关键词自动 touch 最相关记忆
- **rank**: 按 decay_factor × position_weight 重新排序
- **context**: 生成 recency 加权的上下文注入（session_start hook）
- **reap**: 归档极久未访问的记忆（不删除，移到 archive/）

### 衰减算法
```
< 1 小时访问 → 1.5x boost (🔥 热记忆)
1h ~ 3 天    → 1.5x → 1.0x 线性
3 天 ~ 14 天  → 1.0x → 0.5x 指数衰减
> 14 天      → 0.5x → 0.3x 渐进到底 (❄️ 冷记忆)
高频记忆 (50+ access) → +0.15 偏移
```

### Hook 集成
- **post_tool_use/memory-decay-touch.sh**: 每次 memory/session_search 工具使用 → 自动 touch
- **session_start/memory-decay-inject.sh**: 每次新会话 → 按 recency 重新排序注入

---

## 二、Skill 蒸馏飞轮 — `tools/skill_distiller.py` (518 行)

### 功能
- **batch**: 收集 N 个同类型 session → 跨 session 批量蒸馏 → 生成统一 skill
- **distill**: 手动指定 session 蒸馏
- **feedback**: 记录 skill 使用反馈（成功/失败/评分）
- **prune**: 低成功率 skill 自动剪除（归档到 _archived/）
- **stats**: 蒸馏飞轮统计（含成功率排名）

### 飞轮闭环
```
情景记忆(做了什么) → 收集 → 批量蒸馏 → 程序性记忆(skill) → 下次直接调用
                                              ↓
                                        执行反馈 → 评分 → 高分保留/低分回滚
```

### 版本管理
- 同一 task_type 的多次蒸馏 → `-v1`, `-v2`, `-v3` ...
- 注册表 `registry.json` 追踪所有版本和状态
- 高版本 promotion，低版本 deprecation

### Hook 集成
- **stop/skill-feedback.sh**: 会话结束 → 检测 skill 使用 → 记录成功/失败反馈
- 与现有 `skill-auto-learn.sh` (Stop hook) 配合：先蒸馏 → 再记录反馈

---

## 新增文件清单

| 文件 | 行数 | 描述 |
|------|------|------|
| `tools/memory_decay.py` | 452 | 记忆衰减引擎 |
| `tools/skill_distiller.py` | 518 | Skill 蒸馏飞轮 |
| `hooks/post_tool_use/memory-decay-touch.sh` | 29 | 自动 touch hook |
| `hooks/session_start/memory-decay-inject.sh` | 21 | 会话开始注入 hook |
| `hooks/stop/skill-feedback.sh` | 44 | Skill 反馈记录 hook |
| **总计** | **1064** | |

---

## 使用方法

### Memory Decay
```bash
# 手动 touch 一条记忆
python3 tools/memory_decay.py touch "FinnA API: Base URL=..."

# 搜索时自动 touch 最相关记忆
python3 tools/memory_decay.py touch-query "agent delegatetask"

# 查看衰减排名
python3 tools/memory_decay.py rank --limit 10

# 衰减统计
python3 tools/memory_decay.py stats

# 归档 >30 天未访问记忆
python3 tools/memory_decay.py reap --days 30 --no-dry-run
```

### Skill 蒸馏
```bash
# 检查并批量蒸馏
python3 tools/skill_distiller.py batch --all --min-sessions 3

# 记录 skill 使用反馈
python3 tools/skill_distiller.py feedback --skill auto-feature --success true --score 9.1

# 剪除低分 skill
python3 tools/skill_distiller.py prune --min-success-rate 0.5 --no-dry-run

# 飞轮统计
python3 tools/skill_distiller.py stats
```

---

**注意**: Hook 需要重启 Hermes gateway 生效