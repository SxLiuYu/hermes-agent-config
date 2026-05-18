#!/usr/bin/env python3
"""
Hermes Recipe Engine — 对标 Goose Recipes

预配置的可复用 agent 工作流模板。YAML 定义，支持参数插值、条件分支、
子 Agent 编排、步骤重试。

Recipe 结构:
  name: 代码审查 + 自动修复
  description: |
    对 PR 执行代码审查，自动修复 lint 错误，生成审查报告
  args:
    pr_number:
      type: int
      required: true
    auto_fix:
      type: bool
      default: true
  steps:
    - name: 拉取 PR
      run: gh pr checkout {{pr_number}}
    - name: 运行 lint
      run: ruff check --output-format=json .
      capture: lint_output
    - name: 自动修复
      run: ruff check --fix .
      when: "{{auto_fix}} == true and lint_output != '[]'"
    - name: 生成报告
      subagent: |
        审查 PR #{{pr_number}}，总结修改内容和 lint 结果:
        {{lint_output}}
      timeout: 120

用法:
  hermes recipe list                    # 列出可用 recipe
  hermes recipe run <name> [args]      # 运行 recipe
  hermes recipe show <name>            # 查看 recipe 详情
  hermes recipe create <name>          # 交互式创建 recipe
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

HERMES_HOME = Path.home() / ".hermes"
RECIPES_DIR = HERMES_HOME / "recipes"
RECIPE_RUNS_DIR = HERMES_HOME / "logs" / "recipe_runs"


def _resolve_vars(text: str, context: dict) -> str:
    """解析 {{var}} 模板变量，支持 . 路径访问"""
    def replacer(match):
        expr = match.group(1).strip()
        parts = expr.split(".")
        val = context
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p, match.group(0))
            else:
                return match.group(0)
        return str(val) if val is not None else match.group(0)
    return re.sub(r"\{\{(.+?)\}\}", replacer, text)


def _eval_condition(condition: str, context: dict) -> bool:
    """安全地评估条件表达式，只支持简单比较"""
    resolved = _resolve_vars(condition, context)
    # 简单布尔判断
    if resolved.strip() in ("true", "True", "1", "yes"):
        return True
    if resolved.strip() in ("false", "False", "0", "no", "[]", "{}", "null", "None"):
        return False
    # == 比较
    eq_match = re.match(r"(.+?)\s*==\s*(.+)", resolved)
    if eq_match:
        left, right = eq_match.groups()
        return left.strip().strip("'\"").lower() == right.strip().strip("'\"").lower()
    # != 比较
    neq_match = re.match(r"(.+?)\s*!=\s*(.+)", resolved)
    if neq_match:
        left, right = neq_match.groups()
        return left.strip().strip("'\"").lower() != right.strip().strip("'\"").lower()
    # and / or
    return bool(resolved.strip())


class RecipeRunner:
    """执行 recipe 的运行时引擎"""

    def __init__(self, recipe_path: Path):
        self.recipe = yaml.safe_load(recipe_path.read_text())
        self.context: dict = {}
        self.step_results: list = []
        self.start_time = time.time()

    def validate(self) -> list:
        """验证 recipe 结构"""
        errors = []
        if not self.recipe.get("name"):
            errors.append("缺少 name")
        if not self.recipe.get("steps"):
            errors.append("缺少 steps")

        for i, step in enumerate(self.recipe.get("steps", [])):
            if not step.get("name"):
                errors.append(f"Step {i}: 缺少 name")
            if "run" not in step and "subagent" not in step and "recipe" not in step:
                errors.append(f"Step '{step.get('name', i)}': 缺少 run/subagent/recipe")

        return errors

    def _run_step(self, step: dict, step_idx: int) -> dict:
        """执行单个步骤"""
        name = step.get("name", f"step-{step_idx}")
        result = {"name": name, "status": "skipped", "output": "", "duration": 0}

        # 条件检查
        condition = step.get("when")
        if condition:
            if not _eval_condition(condition, self.context):
                result["status"] = "skipped"
                result["output"] = f"条件不满足: {condition}"
                return result

        t0 = time.time()

        try:
            if "run" in step:
                result = self._run_shell(step, result)
            elif "subagent" in step:
                result = self._run_subagent(step, result)
            elif "recipe" in step:
                result = self._run_nested_recipe(step, result)

            result["status"] = "ok"
        except Exception as e:
            result["status"] = "error"
            result["output"] = str(e)

            # 重试
            retries = step.get("retry", 0)
            if retries > 0 and not step.get("_retried"):
                for attempt in range(retries):
                    print(f"  🔄 重试 {name} ({attempt + 1}/{retries})...")
                    time.sleep(step.get("retry_delay", 2))
                    try:
                        step["_retried"] = True
                        if "run" in step:
                            result = self._run_shell(step, result)
                        result["status"] = "ok"
                        break
                    except Exception:
                        continue

            # 失败处理
            on_fail = step.get("on_fail", "stop")
            if on_fail == "continue":
                result["status"] = "error_continued"
            else:
                raise  # 传播到外层

        result["duration"] = time.time() - t0

        # capture 输出到 context
        capture_key = step.get("capture")
        if capture_key:
            self.context[capture_key] = result["output"]

        return result

    def _run_shell(self, step: dict, result: dict) -> dict:
        """执行 shell 命令"""
        cmd = _resolve_vars(step["run"], self.context)
        timeout = step.get("timeout", 300)
        cwd = step.get("cwd")

        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )

        output = proc.stdout.strip()
        if proc.returncode != 0:
            error = proc.stderr.strip()
            result["output"] = output + ("\n" + error if error else "")
            result["exit_code"] = proc.returncode
            if not step.get("ignore_errors"):
                raise RuntimeError(f"命令失败 (exit={proc.returncode}): {error or output[:200]}")
        else:
            result["output"] = output
            result["exit_code"] = 0

        return result

    def _run_subagent(self, step: dict, result: dict) -> dict:
        """派生子 Agent 执行任务"""
        prompt = _resolve_vars(step["subagent"], self.context)
        timeout = step.get("timeout", 120)

        # 通过 delegate_task 机制——这里用简化的本地调用
        # 实际环境中应由 Hermes 的 delegate_task 处理
        env = os.environ.copy()
        env["HERMES_SUBAGENT_PROMPT"] = prompt
        env["HERMES_SUBAGENT_TIMEOUT"] = str(timeout)

        # 尝试通过 agent_bus 投递任务
        bus_file = HERMES_HOME / "scripts" / "agent_bus.py"
        if bus_file.exists():
            proc = subprocess.run(
                ["python3", str(bus_file), "submit",
                 "--task", prompt,
                 "--timeout", str(timeout)],
                capture_output=True, text=True, timeout=30,
            )
            result["output"] = proc.stdout.strip() or proc.stderr.strip()
        else:
            result["output"] = f"[subagent] {prompt[:100]}..."
            result["status"] = "skipped"
            result["output"] = "agent_bus 不可用，无法派生子 Agent"

        return result

    def _run_nested_recipe(self, step: dict, result: dict) -> dict:
        """嵌套执行另一个 recipe"""
        recipe_name = step["recipe"]
        recipe_file = RECIPES_DIR / f"{recipe_name}.yaml"
        if not recipe_file.exists():
            recipe_file = RECIPES_DIR / f"{recipe_name}.yml"

        if not recipe_file.exists():
            result["output"] = f"找不到 recipe: {recipe_name}"
            raise RuntimeError(f"找不到 recipe: {recipe_name}")

        # 合并参数
        nested_args = dict(self.context)
        nested_args.update(step.get("args", {}))

        runner = RecipeRunner(recipe_file)
        run_result = runner.run(**nested_args)

        result["output"] = run_result["summary"]
        result["nested"] = run_result
        return result

    def run(self, **kwargs) -> dict:
        """执行整个 recipe"""
        # 初始化上下文
        self.context = {
            "args": kwargs,
            "env": dict(os.environ),
            "pwd": os.getcwd(),
        }
        # 也把 args 展平到 context
        self.context.update(kwargs)

        # 验证参数
        args_spec = self.recipe.get("args", {})
        for arg_name, spec in args_spec.items():
            if spec.get("required") and arg_name not in kwargs:
                default = spec.get("default")
                if default is not None:
                    self.context[arg_name] = default
                else:
                    raise ValueError(f"缺少必需参数: {arg_name}")
            elif arg_name not in kwargs and "default" in spec:
                self.context[arg_name] = spec["default"]

        recipe_name = self.recipe["name"]
        steps = self.recipe.get("steps", [])

        print(f"📋 Recipe: {recipe_name}")
        print(f"   步骤数: {len(steps)}")
        print(f"   参数:   {', '.join(f'{k}={v}' for k, v in kwargs.items()) if kwargs else '无'}")
        print()

        results = {"name": recipe_name, "steps": [], "ok": 0, "error": 0, "skipped": 0}

        for i, step in enumerate(steps):
            name = step.get("name", f"step-{i}")
            print(f"  [{i+1}/{len(steps)}] {name}...", end=" ", flush=True)

            try:
                step_result = self._run_step(step, i)
                results["steps"].append(step_result)

                if step_result["status"] == "ok":
                    print(f"✅ ({step_result['duration']:.1f}s)")
                    results["ok"] += 1
                elif step_result["status"] == "skipped":
                    print("⏭️  跳过")
                    results["skipped"] += 1
                elif step_result["status"].startswith("error"):
                    print(f"❌ ({step_result['duration']:.1f}s)")
                    results["error"] += 1
                    on_fail = step.get("on_fail", "stop")
                    if on_fail == "stop":
                        print(f"\n⛔ Recipe 中断于步骤 {i+1}: {name}")
                        break

            except Exception as e:
                results["steps"].append({"name": name, "status": "error", "output": str(e), "duration": 0})
                print(f"❌ {str(e)[:80]}")
                results["error"] += 1
                if step.get("on_fail", "stop") == "stop":
                    print(f"\n⛔ Recipe 中断于步骤 {i+1}: {name}")
                    break

        total = results["ok"] + results["error"] + results["skipped"]
        elapsed = time.time() - self.start_time
        results["summary"] = f"{recipe_name}: {results['ok']}✅ {results['error']}❌ {results['skipped']}⏭️ (共 {total} 步, {elapsed:.1f}s)"
        results["elapsed"] = elapsed

        # 保存运行记录
        self._save_run(results)

        print(f"\n📊 {results['summary']}")
        return results

    def _save_run(self, results: dict):
        """保存运行记录"""
        RECIPE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        run_file = RECIPE_RUNS_DIR / f"{results['name']}-{ts}.json"
        run_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────


def list_recipes():
    """列出所有 recipe"""
    if not RECIPES_DIR.exists():
        print("📋 暂无 recipe\n💡 创建: hermes recipe create <name>")
        return

    recipes = []
    for f in sorted(RECIPES_DIR.glob("*.yaml")) + sorted(RECIPES_DIR.glob("*.yml")):
        try:
            data = yaml.safe_load(f.read_text())
            recipes.append({
                "name": data.get("name", f.stem),
                "file": f.name,
                "description": data.get("description", ""),
                "steps": len(data.get("steps", [])),
                "args": list(data.get("args", {}).keys()),
            })
        except Exception:
            recipes.append({"name": f.stem, "file": f.name, "description": "⚠️ 解析失败", "steps": 0, "args": []})

    if not recipes:
        print("📋 暂无有效 recipe")
        return

    print(f"📋 可用 Recipe ({len(recipes)} 个):\n")
    for r in recipes:
        args_str = ", ".join(r["args"]) if r["args"] else "无参数"
        print(f"  {r['name']:<25} {r['steps']} 步  [{args_str}]")
        if r["description"]:
            print(f"    {r['description'][:80]}")


def show_recipe(name: str):
    """查看 recipe 详情"""
    recipe_file = RECIPES_DIR / f"{name}.yaml"
    if not recipe_file.exists():
        recipe_file = RECIPES_DIR / f"{name}.yml"

    if not recipe_file.exists():
        print(f"❌ 找不到 recipe: {name}")
        return

    data = yaml.safe_load(recipe_file.read_text())
    print(f"🔖 Recipe: {data.get('name', name)}")
    print(f"   描述: {data.get('description', '无')}")
    print("   参数:")
    for arg_name, spec in data.get("args", {}).items():
        req = "必需" if spec.get("required") else "可选"
        default = f" (默认: {spec['default']})" if "default" in spec else ""
        print(f"     {arg_name}: {spec.get('type', 'any')} [{req}]{default}")
    print(f"\n   步骤 ({len(data.get('steps', []))} 个):")
    for i, step in enumerate(data.get("steps", [])):
        name = step.get("name", f"step-{i}")
        action = step.get("run", step.get("subagent", step.get("recipe", "?")))[:60]
        cond = f" [条件: {step['when']}]" if step.get("when") else ""
        print(f"     {i+1}. {name}{cond}")
        print(f"        {action}")


def create_recipe(name: str):
    """创建新的 recipe 模板"""
    recipe_file = RECIPES_DIR / f"{name}.yaml"
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)

    if recipe_file.exists():
        print(f"⚠️  recipe 已存在: {name}")
        return

    template = f"""name: {name}
description: |
  描述这个 recipe 的用途

args:
  # 定义参数
  # target:
  #   type: str
  #   required: true
  #   description: 目标描述

steps:
  - name: 第一步
    run: echo "Hello from {{args.target or 'world'}}"
    capture: output

  - name: 条件执行
    run: echo "Conditional step"
    when: "output != ''"

  - name: 子 Agent 分析
    subagent: |
      分析以下输出并总结:
      {{output}}
    timeout: 60
    # retry: 2
    # on_fail: continue
"""

    recipe_file.write_text(template)
    print(f"✅ Recipe 模板已创建: {recipe_file}")
    print(f"   编辑: vim {recipe_file}")


def run_recipe(name: str, **kwargs):
    """运行 recipe"""
    recipe_file = RECIPES_DIR / f"{name}.yaml"
    if not recipe_file.exists():
        recipe_file = RECIPES_DIR / f"{name}.yml"

    if not recipe_file.exists():
        print(f"❌ 找不到 recipe: {name}")
        print("💡 可用 recipe: ")
        list_recipes()
        return

    runner = RecipeRunner(recipe_file)
    errors = runner.validate()
    if errors:
        print("⚠️  Recipe 验证失败:")
        for e in errors:
            print(f"  - {e}")
        return

    try:
        runner.run(**kwargs)
    except KeyboardInterrupt:
        print("\n⛔ Recipe 被中断")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Recipe Engine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出可用 recipe")

    show_p = sub.add_parser("show", help="查看 recipe 详情")
    show_p.add_argument("name")

    run_p = sub.add_parser("run", help="运行 recipe")
    run_p.add_argument("name")
    run_p.add_argument("args", nargs="*", help="参数: key=value")

    create_p = sub.add_parser("create", help="创建新 recipe")
    create_p.add_argument("name")

    args = parser.parse_args()

    if args.command == "list":
        list_recipes()
    elif args.command == "show":
        show_recipe(args.name)
    elif args.command == "create":
        create_recipe(args.name)
    elif args.command == "run":
        kwargs = {}
        if args.args:
            for a in args.args:
                if "=" in a:
                    k, v = a.split("=", 1)
                    kwargs[k] = v
        run_recipe(args.name, **kwargs)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()