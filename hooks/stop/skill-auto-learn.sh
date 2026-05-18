#!/bin/bash
# Skill Auto-Learner — 对标 Letta Sleep-time Compute
# 每次会话结束自动分析并生成 skill 草稿
python3 "$HOME/.hermes/scripts/skill_auto_learn.py" draft 2>&1 | tee -a "$HOME/.hermes/logs/skill_learning.log"