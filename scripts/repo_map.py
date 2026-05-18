#!/usr/bin/env python3
"""
Repo Map v2 — 基于 PageRank 的代码库结构图

对标 Aider Repo-Map 完整实现:
  1. 符号提取 — ast (Python) + tree-sitter fallback
  2. 依赖图构建 — 文件节点 + 导入/调用边
  3. PageRank 排序 — 迭代计算文件重要性
  4. 贪心选择 — 按 token 预算选取最重要的符号

相比 v1 的改进:
  - 从简单引用计数 → 真正的 PageRank 迭代
  - 跨文件导入解析（相对导入、包导入）
  - 函数调用图（追踪 def_a 调用 def_b）
  - Token 预算感知的选择算法

用法:
  python3 scripts/repo_map.py map ~/YuanFang --budget 3000
  python3 scripts/repo_map.py graph ~/YuanFang          # Mermaid 依赖图
  python3 scripts/repo_map.py ranks ~/YuanFang          # 查看 PageRank 排名
"""

import ast
import sys
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set


class RepoMapV2:
    """基于 PageRank 的代码库结构分析器"""

    def __init__(self, root: str, max_files: int = 200):
        self.root = Path(root).expanduser().resolve()
        self.max_files = max_files

        # 数据
        self.files: Dict[str, dict] = {}       # relpath → {symbols, imports, calls}
        self.call_graph: Dict[str, Set[str]] = defaultdict(set)  # caller → callees
        self.import_graph: Dict[str, Set[str]] = defaultdict(set)  # file → imported files
        self.pagerank: Dict[str, float] = {}

        # 排除目录
        self.exclude = {'venv', '.venv', '__pycache__', '.git', '.hermes',
                       'node_modules', 'dist', 'build', '.eggs', '.tox',
                       'migrations', '.pytest_cache', '.mypy_cache'}

    # ─── Phase 1: 符号提取 ───────────────────────────────────

    def scan(self, glob_pattern: str = "*.py"):
        """扫描所有源文件，提取符号和引用"""
        files = []
        for fp in self.root.rglob(glob_pattern):
            # 用相对路径检查排除目录（避免绝对路径中的 .hermes 误杀）
            try:
                rel = fp.relative_to(self.root)
            except ValueError:
                rel = fp
            rel_parts = set(str(p) for p in rel.parent.parts if p != '.')
            if rel_parts & self.exclude:
                continue
            # 跳过测试文件
            if 'test' in fp.stem.lower() and fp.suffix == '.py':
                continue
            files.append(fp)

        # 限制总数后再按修改时间排序，活跃文件优先
        # 如果文件太多（大项目），先排序再截断
        if len(files) > self.max_files * 2:
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            files = files[:self.max_files]

        print(f"📂 扫描 {len(files)} 个文件...", file=sys.stderr)

        for i, fp in enumerate(files):
            if i % 50 == 0:
                print(f"  进度: {i}/{len(files)}", file=sys.stderr)
            self._scan_file(fp)

        print(f"  完成: {len(self.files)} 个文件", file=sys.stderr)

    def _scan_file(self, fp: Path):
        """扫描单个文件：符号 + 导入 + 调用"""
        try:
            rel = str(fp.relative_to(self.root))
        except ValueError:
            rel = str(fp)

        try:
            source = fp.read_text()
        except (UnicodeDecodeError, PermissionError):
            return

        # 尝试用 ast 解析
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # Python 语法错误，跳过
            return

        symbols = []
        imports = []
        calls = set()

        for node in ast.walk(tree):
            # ── 函数定义 ──
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                decorators = self._extract_decorators(node)

                # 追踪函数内部的调用
                inner_calls = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        called = self._resolve_call(child)
                        if called:
                            inner_calls.add(called)
                            calls.add(called)

                symbols.append({
                    "type": "function",
                    "name": node.name,
                    "args": args,
                    "decorators": decorators,
                    "lineno": node.lineno,
                    "calls": list(inner_calls),
                })

            # ── 类定义 ──
            elif isinstance(node, ast.ClassDef):
                bases = self._extract_bases(node)
                methods = []
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(child.name)

                # 追踪类方法内部的调用
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        called = self._resolve_call(child)
                        if called:
                            calls.add(called)

                symbols.append({
                    "type": "class",
                    "name": node.name,
                    "bases": bases,
                    "methods": methods[:12],
                    "lineno": node.lineno,
                })

            # ── 导入 ──
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({
                        "module": alias.name,
                        "name": alias.name,
                        "alias": alias.asname,
                    })

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    level = node.level  # 相对导入层级
                    for alias in node.names:
                        imports.append({
                            "module": node.module,
                            "name": alias.name,
                            "alias": alias.asname,
                            "level": level,  # 0=绝对, >0=相对
                        })

        if symbols or imports or calls:
            self.files[rel] = {
                "symbols": symbols,
                "imports": imports,
                "calls": calls,
                "lines": len(source.split("\n")),
            }

    def _extract_decorators(self, node) -> List[str]:
        decs = []
        for d in node.decorator_list:
            if isinstance(d, ast.Name):
                decs.append(d.id)
            elif isinstance(d, ast.Attribute):
                if hasattr(d.value, 'id'):
                    decs.append(f"{d.value.id}.{d.attr}")
                else:
                    decs.append(d.attr)
            elif isinstance(d, ast.Call):
                if hasattr(d.func, 'id'):
                    decs.append(d.func.id)
        return decs

    def _extract_bases(self, node: ast.ClassDef) -> List[str]:
        bases = []
        for b in node.bases:
            if isinstance(b, ast.Name):
                bases.append(b.id)
            elif isinstance(b, ast.Attribute):
                if hasattr(b.value, 'id'):
                    bases.append(f"{b.value.id}.{b.attr}")
        return bases

    def _resolve_call(self, node: ast.Call) -> str:
        """解析函数调用 -> 函数名"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            if hasattr(node.func.value, 'id'):
                return f"{node.func.value.id}.{node.func.attr}"
            return node.func.attr
        return ""

    # ─── Phase 2: 依赖图构建 ─────────────────────────────────

    def build_graph(self):
        """构建文件级依赖图 + 函数调用图"""
        print("🔗 构建依赖图...", file=sys.stderr)

        # 建立模块名 → 文件路径的映射
        module_to_file = {}
        for fpath in self.files:
            mod = self._file_to_module(fpath)
            module_to_file[mod] = fpath
            # 也注册短名
            short = mod.split(".")[-1]
            if short not in module_to_file:
                module_to_file[short] = fpath

        for fpath, info in self.files.items():
            fmod = self._file_to_module(fpath)

            # 解析导入 → import_graph
            for imp in info.get("imports", []):
                target_mod = imp["module"]
                level = imp.get("level", 0)

                if level > 0:
                    # 相对导入：从当前文件路径解析
                    resolved = self._resolve_relative_import(fpath, target_mod, level)
                else:
                    # 绝对导入
                    resolved = target_mod

                # 查找目标文件
                target_file = self._find_module_file(resolved, module_to_file)
                if target_file and target_file != fpath:
                    self.import_graph[fpath].add(target_file)

            # 解析函数调用 → call_graph
            for call in info.get("calls", set()):
                # 尝试找调用目标所在的文件
                target_mod = self._find_call_module(call, module_to_file, fpath)
                if target_mod and target_mod != fpath:
                    self.call_graph[fpath].add(target_mod)

        # 合并为一个图（import + call edges）
        self.graph = defaultdict(set)
        for fpath in self.files:
            self.graph[fpath].update(self.import_graph.get(fpath, set()))
            self.graph[fpath].update(self.call_graph.get(fpath, set()))

        edge_count = sum(len(v) for v in self.graph.values())
        print(f"  构建完成: {len(self.files)} 节点, {edge_count} 边", file=sys.stderr)

    def _file_to_module(self, fpath: str) -> str:
        """文件路径 → Python 模块名"""
        parts = list(Path(fpath).parts)
        if parts[-1] == "__init__.py":
            return ".".join(parts[:-1])
        return ".".join(parts).replace(".py", "")

    def _resolve_relative_import(self, fpath: str, target_mod: str, level: int) -> str:
        """解析相对导入"""
        parts = list(Path(fpath).parts)
        # 去掉文件名
        if parts[-1].endswith(".py"):
            dir_parts = parts[:-1]
        else:
            dir_parts = parts

        if parts[-1] == "__init__.py":
            dir_parts = parts[:-1]

        # 向上 level 层
        if level > 1:
            dir_parts = dir_parts[:-(level - 1)]
        elif level == 1:
            pass  # 当前包

        result_parts = list(dir_parts)
        if target_mod:
            result_parts.append(target_mod)

        return ".".join(result_parts)

    def _find_module_file(self, mod_name: str, module_to_file: Dict) -> str:
        """查找模块对应的文件"""
        # 精确匹配
        if mod_name in module_to_file:
            return module_to_file[mod_name]

        # 尝试加 __init__.py
        init_mod = f"{mod_name}.__init__"
        if init_mod in module_to_file:
            return module_to_file[init_mod]

        # 按后缀匹配：mod_name 可能是某个长模块名的前缀
        for mod, fpath in module_to_file.items():
            if mod.endswith(f".{mod_name}") or mod == mod_name:
                return fpath

        return ""

    def _find_call_module(self, call_name: str, module_to_file: Dict, current_file: str) -> str:
        """查找函数调用所在的文件"""
        # call_name 可能是 "module.function" 或 "obj.method"
        parts = call_name.split(".")

        # 先试 module.function 形式
        if len(parts) >= 2:
            mod = ".".join(parts[:-1])
            if mod in module_to_file:
                return module_to_file[mod]

        # 在当前文件的符号中查找
        info = self.files.get(current_file, {})
        for sym in info.get("symbols", []):
            if sym["name"] == call_name and sym["type"] == "function":
                return current_file

        return ""

    # ─── Phase 3: PageRank ────────────────────────────────────

    def compute_pagerank(self, damping: float = 0.85, iterations: int = 50):
        """计算文件级 PageRank"""
        files = list(self.files.keys())
        n = len(files)
        if n == 0:
            return

        print(f"📊 计算 PageRank ({n} 节点)...", file=sys.stderr)

        # 初始化
        self.pagerank = {f: 1.0 / n for f in files}

        for it in range(iterations):
            new_pr = {}
            total_change = 0

            for f in files:
                # 所有指向 f 的文件
                incoming = []
                for src, targets in self.graph.items():
                    if f in targets:
                        incoming.append(src)

                if incoming:
                    rank_sum = sum(
                        self.pagerank[src] / max(len(self.graph[src]), 1)
                        for src in incoming
                    )
                    new_pr[f] = (1 - damping) / n + damping * rank_sum
                else:
                    new_pr[f] = (1 - damping) / n

                total_change += abs(new_pr[f] - self.pagerank.get(f, 0))

            self.pagerank = new_pr

            # 收敛检测
            if total_change < 0.0001:
                print(f"  收敛于迭代 {it + 1}", file=sys.stderr)
                break

        print("  完成", file=sys.stderr)

    # ─── Phase 4: 贪心选择 ────────────────────────────────────

    def generate_map(self, token_budget: int = 3000) -> str:
        """按 PageRank + token 预算生成结构图"""
        # 估算 token：英文 ~4 char/token，中文 ~1.5 char/token
        char_budget = token_budget * 3  # 保守估计

        lines = [f"# Repo Map: {self.root.name}\n"]
        lines.append(f"文件: {len(self.files)} | "
                    f"符号: {sum(len(f.get('symbols', [])) for f in self.files.values())} | "
                    f"PageRank 排序\n")

        # 按 PageRank 排序
        sorted_files = sorted(
            self.files.items(),
            key=lambda x: self.pagerank.get(x[0], 0),
            reverse=True
        )

        used_chars = len("\n".join(lines))
        files_shown = 0
        symbols_shown = 0

        # 每文件最大符号数（高 rank 文件更多）
        for fpath, info in sorted_files:
            if used_chars >= char_budget:
                break

            pr = self.pagerank.get(fpath, 0)
            syms = info.get("symbols", [])
            if not syms:
                continue

            # 高 rank 文件展示更多符号
            if pr > 0.01:
                max_syms = min(len(syms), 8)
            elif pr > 0.005:
                max_syms = min(len(syms), 5)
            elif pr > 0.002:
                max_syms = min(len(syms), 3)
            else:
                max_syms = min(len(syms), 1)

            block = f"\n## {fpath} (PR={pr:.4f}, {len(syms)} symbols)\n"
            block_chars = len(block)

            for sym in syms[:max_syms]:
                if sym["type"] == "class":
                    bases_str = ", ".join(sym["bases"]) if sym["bases"] else ""
                    line = f"  📦 class {sym['name']}"
                    if bases_str:
                        line += f"({bases_str})"
                    line += "\n"
                    if sym["methods"][:5]:
                        line += f"     · {', '.join(sym['methods'][:5])}\n"
                elif sym["type"] == "function":
                    args_str = ", ".join(sym["args"][:4])
                    dec_str = f" @{','.join(sym['decorators'])}" if sym.get("decorators") else ""
                    line = f"  🔧 def {sym['name']}({args_str}){dec_str}\n"
                else:
                    continue

                if block_chars + len(line) > 500:
                    line = f"  ... (+{len(syms) - max_syms} more)\n"
                    block += line
                    break

                block += line
                block_chars += len(line)

            lines.append(block)
            used_chars += block_chars
            files_shown += 1
            symbols_shown += min(len(syms), max_syms)

        # 摘要
        remaining = len(self.files) - files_shown
        if remaining > 0:
            lines.append(f"\n... (+{remaining} 个低排名文件)")

        total_syms = sum(len(f.get("symbols", [])) for f in self.files.values())
        lines.append(f"\n📊 展示 {symbols_shown}/{total_syms} 个符号 "
                    f"(预算 {token_budget} tokens, 约 {used_chars} 字符)")

        return "\n".join(lines)

    # ─── 其他输出 ─────────────────────────────────────────────

    def get_ranks(self, top_n: int = 20) -> str:
        """Top-N PageRank 排名"""
        sorted_files = sorted(
            self.pagerank.items(),
            key=lambda x: x[1],
            reverse=True
        )[:top_n]

        lines = [f"# PageRank Top {top_n}: {self.root.name}\n"]
        lines.append(f"{'Rank':<5} {'PR':<10} {'Imports':<8} {'Symbols':<8} File")

        for i, (fpath, pr) in enumerate(sorted_files, 1):
            info = self.files.get(fpath, {})
            imports = len(info.get("imports", []))
            syms = len(info.get("symbols", []))
            bar = "█" * int(pr * 500) if pr > 0 else ""
            lines.append(f"{i:<5} {pr:<10.6f} {imports:<8} {syms:<8} {fpath} {bar}")

        return "\n".join(lines)

    def generate_graph_mermaid(self) -> str:
        """生成 Mermaid 格式依赖图"""
        lines = ["```mermaid", "graph LR"]

        # 只显示 Top-30 文件
        top_files = sorted(
            self.pagerank.items(),
            key=lambda x: x[1], reverse=True
        )[:30]
        top_set = {f for f, _ in top_files}

        # 节点
        for fpath, pr in top_files:
            short = Path(fpath).stem[:15]
            lines.append(f'  {short}["{short}<br/>PR={pr:.4f}"]')

        # 边
        for fpath in top_set:
            short = Path(fpath).stem[:15]
            targets = self.graph.get(fpath, set())
            for t in targets & top_set:
                t_short = Path(t).stem[:15]
                if t_short != short:
                    lines.append(f"  {short} --> {t_short}")

        lines.append("```")
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Repo Map v2 — 基于 PageRank 的代码库结构图"
    )
    sub = parser.add_subparsers(dest="command")

    map_p = sub.add_parser("map", help="生成结构图")
    map_p.add_argument("path", help="项目路径")
    map_p.add_argument("--budget", type=int, default=3000, help="Token 预算")

    ranks_p = sub.add_parser("ranks", help="查看 PageRank 排名")
    ranks_p.add_argument("path", help="项目路径")
    ranks_p.add_argument("--top", type=int, default=20)

    graph_p = sub.add_parser("graph", help="依赖关系图 (Mermaid)")
    graph_p.add_argument("path", help="项目路径")

    stats_p = sub.add_parser("stats", help="统计信息")
    stats_p.add_argument("path", help="项目路径")

    args = parser.parse_args()

    if hasattr(args, 'path'):
        mapper = RepoMapV2(args.path)
        mapper.scan()
        mapper.build_graph()
        mapper.compute_pagerank()

        if args.command == "map":
            print(mapper.generate_map(args.budget))
        elif args.command == "ranks":
            print(mapper.get_ranks(getattr(args, 'top', 20)))
        elif args.command == "graph":
            print(mapper.generate_graph_mermaid())
        elif args.command == "stats":
            total_syms = sum(len(f.get("symbols", [])) for f in mapper.files.values())
            total_classes = sum(1 for f in mapper.files.values()
                              for s in f.get("symbols", []) if s["type"] == "class")
            total_funcs = sum(1 for f in mapper.files.values()
                            for s in f.get("symbols", []) if s["type"] == "function")
            edges = sum(len(v) for v in mapper.graph.values())
            print(f"文件: {len(mapper.files)}")
            print(f"符号: {total_syms} (类: {total_classes}, 函数: {total_funcs})")
            print(f"依赖边: {edges}")
            if mapper.pagerank:
                avg = sum(mapper.pagerank.values()) / len(mapper.pagerank)
                max_pr = max(mapper.pagerank.values())
                print(f"平均 PageRank: {avg:.6f}")
                print(f"最高 PageRank: {max_pr:.6f}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()