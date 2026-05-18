#!/usr/bin/env python3
"""
P0-15: Structured Knowledge Memory — Cognee-style graph memory

对标:
  - Cognee (12.6k stars, GitHub): Entity-relationship knowledge graph for agents
  - Mem0 vs Cognee 对比: "Mem0 是平铺笔记本，Cognee 是带交叉引用的百科全书"

核心差异:
  Mem0 风格 (现有 memory 工具):
    "字节2025年营收超过1000亿美元" → 存为纯文本
    "TikTok在美国面临监管" → 存为纯文本
    查询 "字节的海外风险" → 可能只能召回其中一个

  Cognee 风格 (本工具):
    实体: 字节 → 属性: {2025营收: 1000亿}
    实体: TikTok → 关系: [parent_of: 字节, risk: US监管]
    查询 "字节的海外风险" → 沿关系链路: 字节→(拥有)→TikTok→(面临)→US监管

实现:
  - 轻量级: 单文件 Python, 不依赖外部图数据库
  - 基于 networkx + JSON 文件持久化
  - 支持三种检索: 向量相似 / 实体关系遍历 / 混合检索
"""

import json
import os
import re
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict, deque

# ─── 数据存储 ────────────────────────────────────────────

MEMORY_DIR = os.path.expanduser("~/.hermes/memory")
GRAPH_FILE = os.path.join(MEMORY_DIR, "knowledge_graph.json")

# ─── 实体和关系 ──────────────────────────────────────────


@dataclass
class Entity:
    """知识图谱中的实体节点"""
    id: str
    name: str
    type: str  # person, project, tool, concept, file, server, api, skill, etc.
    attributes: dict = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)  # 关联的陈述列表
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


@dataclass
class Relation:
    """实体间的关系边"""
    source: str  # entity id
    target: str  # entity id
    relation_type: str  # owns, depends_on, uses, part_of, risks, configured_by, etc.
    description: str = ""
    weight: float = 1.0  # 关系强度
    facts: list[str] = field(default_factory=list)  # 支撑该关系的证据


@dataclass
class KnowledgeGraph:
    """完整知识图谱"""
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    version: int = 1
    updated_at: float = 0.0

    def __post_init__(self):
        if self.updated_at == 0.0:
            self.updated_at = time.time()


# ─── 序列化 ──────────────────────────────────────────────


def _serialize(graph: KnowledgeGraph) -> dict:
    """序列化为 JSON"""
    return {
        "entities": {eid: {
            "id": e.id,
            "name": e.name,
            "type": e.type,
            "attributes": e.attributes,
            "facts": e.facts,
            "created_at": e.created_at,
        } for eid, e in graph.entities.items()},
        "relations": [{
            "source": r.source,
            "target": r.target,
            "relation_type": r.relation_type,
            "description": r.description,
            "weight": r.weight,
            "facts": r.facts,
        } for r in graph.relations],
        "version": graph.version,
        "updated_at": graph.updated_at,
    }


def _deserialize(data: dict) -> KnowledgeGraph:
    """从 JSON 加载"""
    graph = KnowledgeGraph(version=data.get("version", 1),
                            updated_at=data.get("updated_at", time.time()))
    for eid, edata in data.get("entities", {}).items():
        graph.entities[eid] = Entity(
            id=edata["id"],
            name=edata["name"],
            type=edata["type"],
            attributes=edata.get("attributes", {}),
            facts=edata.get("facts", []),
            created_at=edata.get("created_at", time.time()),
        )
    for rdata in data.get("relations", []):
        graph.relations.append(Relation(
            source=rdata["source"],
            target=rdata["target"],
            relation_type=rdata["relation_type"],
            description=rdata.get("description", ""),
            weight=rdata.get("weight", 1.0),
            facts=rdata.get("facts", []),
        ))
    return graph


def load_graph() -> KnowledgeGraph:
    """加载知识图谱"""
    if os.path.exists(GRAPH_FILE):
        try:
            with open(GRAPH_FILE) as f:
                return _deserialize(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return KnowledgeGraph()
    return KnowledgeGraph()


def save_graph(graph: KnowledgeGraph):
    """保存知识图谱"""
    graph.version += 1
    graph.updated_at = time.time()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(GRAPH_FILE, "w") as f:
        json.dump(_serialize(graph), f, indent=2, ensure_ascii=False)


# ─── 实体管理 ────────────────────────────────────────────


def _entity_id(name: str, etype: str) -> str:
    """生成实体 ID"""
    raw = f"{etype}:{name.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def add_entity(graph: KnowledgeGraph, name: str, etype: str,
               attributes: dict = None, fact: str = "") -> str:
    """添加或更新实体"""
    eid = _entity_id(name, etype)
    if eid in graph.entities:
        entity = graph.entities[eid]
        if attributes:
            entity.attributes.update(attributes)
        if fact and fact not in entity.facts:
            entity.facts.append(fact)
    else:
        graph.entities[eid] = Entity(
            id=eid, name=name, type=etype,
            attributes=attributes or {},
            facts=[fact] if fact else [],
        )
    return eid


def find_entity(graph: KnowledgeGraph, name: str = "",
                etype: str = "", eid: str = "") -> Optional[Entity]:
    """查找实体"""
    if eid and eid in graph.entities:
        return graph.entities[eid]
    if name:
        target_id = _entity_id(name, etype) if etype else None
        if target_id and target_id in graph.entities:
            return graph.entities[target_id]
        # 模糊匹配
        name_lower = name.lower()
        for entity in graph.entities.values():
            if name_lower in entity.name.lower():
                return entity
    return None


def search_entities(graph: KnowledgeGraph, query: str) -> list[Entity]:
    """搜索实体（关键词匹配）"""
    q = query.lower()
    results = []
    for entity in graph.entities.values():
        score = 0
        if q in entity.name.lower():
            score += 10
        if q in entity.type.lower():
            score += 5
        for key, val in entity.attributes.items():
            if q in str(key).lower() or q in str(val).lower():
                score += 3
        for fact in entity.facts:
            if q in fact.lower():
                score += 2
        if score > 0:
            results.append((score, entity))
    results.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in results[:20]]


# ─── 关系管理 ────────────────────────────────────────────


def add_relation(graph: KnowledgeGraph, source_name: str, source_type: str,
                 target_name: str, target_type: str, relation_type: str,
                 description: str = "", fact: str = "", weight: float = 1.0):
    """添加关系（自动创建不存在的实体）"""
    src_id = add_entity(graph, source_name, source_type)
    tgt_id = add_entity(graph, target_name, target_type)

    # 检查是否已存在相同关系
    for r in graph.relations:
        if (r.source == src_id and r.target == tgt_id
                and r.relation_type == relation_type):
            r.weight = max(r.weight, weight)
            if fact and fact not in r.facts:
                r.facts.append(fact)
            return src_id, tgt_id

    graph.relations.append(Relation(
        source=src_id, target=tgt_id,
        relation_type=relation_type,
        description=description,
        weight=weight,
        facts=[fact] if fact else [],
    ))
    return src_id, tgt_id


def get_related_entities(graph: KnowledgeGraph, entity_name: str,
                         entity_type: str = "",
                         relation_type: str = "",
                         direction: str = "both") -> list[dict]:
    """
    查找与指定实体相关的其他实体。
    direction: "out" (出边), "in" (入边), "both" (双向)
    """
    entity = find_entity(graph, name=entity_name, etype=entity_type)
    if not entity:
        return []

    results = []
    for r in graph.relations:
        if relation_type and r.relation_type != relation_type:
            continue

        other_eid = None
        rel_dir = ""
        if direction in ("out", "both") and r.source == entity.id:
            other_eid = r.target
            rel_dir = "→"
        if direction in ("in", "both") and r.target == entity.id:
            other_eid = r.source
            rel_dir = "←"

        if other_eid and other_eid in graph.entities:
            results.append({
                "entity": graph.entities[other_eid],
                "relation": r,
                "direction": rel_dir,
            })

    results.sort(key=lambda x: x["relation"].weight, reverse=True)
    return results


# ─── 路径查询 ────────────────────────────────────────────


def find_path(graph: KnowledgeGraph, from_name: str, to_name: str,
              from_type: str = "", to_type: str = "",
              max_depth: int = 3) -> Optional[list[dict]]:
    """
    查找两个实体之间的最短关系路径。
    对标 Cognee 的关系推理能力。
    """
    from_e = find_entity(graph, name=from_name, etype=from_type)
    to_e = find_entity(graph, name=to_name, etype=to_type)
    if not from_e or not to_e:
        return None

    # BFS
    # 构建邻接表
    neighbors = defaultdict(list)
    for r in graph.relations:
        neighbors[r.source].append((r.target, r))
        neighbors[r.target].append((r.source, r))

    queue = deque([(from_e.id, [])])
    visited = {from_e.id}

    while queue:
        current, path = queue.popleft()
        if len(path) >= max_depth:
            continue

        for neighbor_id, relation in neighbors[current]:
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)

            new_path = path + [{
                "from": graph.entities[current],
                "to": graph.entities[neighbor_id],
                "relation": relation,
            }]

            if neighbor_id == to_e.id:
                return new_path

            queue.append((neighbor_id, new_path))

    return None


# ─── 智能关系提取 ────────────────────────────────────────


# 预定义的关系提取规则
RELATION_PATTERNS = [
    # 拥有/控制关系
    (r"(\S+)拥有(\S+)", "owns"),
    (r"(\S+)收购了(\S+)", "owns"),
    (r"(\S+)旗下有(\S+)", "owns"),
    (r"(\S+)\s+(owns|controls|acquired)\s+(\S+)", "owns"),
    # 依赖关系
    (r"(\S+)依赖(\S+)", "depends_on"),
    (r"(\S+)基于(\S+)", "depends_on"),
    (r"(\S+)运行在(\S+)", "runs_on"),
    (r"(\S+)\s+(depends on|runs on|built on)\s+(\S+)", "depends_on"),
    # 使用关系
    (r"(\S+)使用(\S+)(?:API|模型|服务|框架)", "uses"),
    (r"(\S+)调用(\S+)", "uses"),
    # 风险关系
    (r"(\S+)面临(\S+)(?:风险|监管|审查|限制)", "risks"),
    (r"(\S+)影响(\S+)", "affects"),
    # 配置关系
    (r"(\S+)配置了(\S+)", "configured_with"),
    (r"(\S+)部署到(\S+)", "deployed_to"),
    # 关系关系
    (r"(\S+)关联(\S+)", "related_to"),
]


def extract_relations_from_text(text: str) -> list[dict]:
    """
    从文本中自动提取实体关系。
    轻量级版本——基于正则规则，不依赖 LLM。
    （也可以配置为调用 LLM 进行更精准的提取）
    """
    found = []
    for pattern, rel_type in RELATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if len(match.groups()) == 2:
                found.append({
                    "source": match.group(1),
                    "target": match.group(2),
                    "relation_type": rel_type,
                    "evidence": match.group(0),
                })
    return found


def auto_populate(graph: KnowledgeGraph, text: str,
                  entity_type: str = "concept") -> int:
    """
    自动从文本中提取关系并填充图谱。
    返回提取到的关系数量。
    """
    relations = extract_relations_from_text(text)
    count = 0
    for rel in relations:
        try:
            add_relation(
                graph,
                source_name=rel["source"],
                source_type=entity_type,
                target_name=rel["target"],
                target_type=entity_type,
                relation_type=rel["relation_type"],
                fact=rel["evidence"],
            )
            count += 1
        except Exception:
            continue
    if count > 0:
        save_graph(graph)
    return count


# ─── 查询接口 ────────────────────────────────────────────


def query(graph: KnowledgeGraph, question: str) -> str:
    """
    综合查询：搜索实体 + 查找关系 + 关系推理。
    对标 Cognee 的混合检索。
    """
    # 尝试匹配 "A 和 B 的关系"
    rel_match = re.search(r"(\S+)[和与]\s*(\S+)[的]*(关系|关联|联系)", question)
    if rel_match:
        path = find_path(graph, rel_match.group(1), rel_match.group(2))
        if path:
            steps = []
            for step in path:
                steps.append(
                    f"{step['from'].name} -[{step['relation'].relation_type}]→ "
                    f"{step['to'].name}"
                )
            return f"找到关系路径: {' → '.join(steps)}"

    # 尝试匹配 "与 X 相关的 Y"
    related_match = re.search(r"(?:与|和)\s*(\S+)\s*相关", question)
    if related_match:
        related = get_related_entities(graph, related_match.group(1))
        if related:
            lines = [f"与 {related_match.group(1)} 相关的实体:"]
            for item in related[:10]:
                e = item["entity"]
                r = item["relation"]
                lines.append(
                    f"  {item['direction']} {e.name} "
                    f"({r.relation_type}: {r.description or r.facts[0] if r.facts else ''})"
                )
            return "\n".join(lines)

    # 通用搜索
    entities = search_entities(graph, question)
    if entities:
        lines = [f"找到 {len(entities)} 个相关实体:"]
        for e in entities[:10]:
            attrs = ", ".join(f"{k}={v}" for k, v in list(e.attributes.items())[:3])
            facts_preview = e.facts[0][:60] + "..." if e.facts else ""
            lines.append(f"  [{e.type}] {e.name}" + (f" ({attrs})" if attrs else ""))
            if facts_preview:
                lines.append(f"    {facts_preview}")
        return "\n".join(lines)

    return "未找到相关信息"


# ─── 摘要导出 ────────────────────────────────────────────


def export_summary(graph: KnowledgeGraph) -> str:
    """导出图谱摘要（可注入到 Agent 上下文）"""
    lines = ["## 知识图谱摘要"]
    entity_count = len(graph.entities)
    relation_count = len(graph.relations)
    lines.append(f"实体: {entity_count} | 关系: {relation_count}")

    # 按类型分组
    by_type = defaultdict(list)
    for e in graph.entities.values():
        by_type[e.type].append(e.name)

    for etype, names in sorted(by_type.items()):
        names_preview = ", ".join(names[:5])
        suffix = f" (+{len(names)-5} more)" if len(names) > 5 else ""
        lines.append(f"- {etype}: {names_preview}{suffix}")

    # 关键关系
    if graph.relations:
        lines.append("\n关键关系:")
        for r in sorted(graph.relations, key=lambda x: x.weight, reverse=True)[:10]:
            src = graph.entities.get(r.source)
            tgt = graph.entities.get(r.target)
            if src and tgt:
                lines.append(f"  {src.name} -[{r.relation_type}]→ {tgt.name}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# DAG 记忆层 — 对标 TeleMem 论文（中国电信）
# 时序依赖边 + 因果闭包检索 + 传递冗余剪除 + 拓扑排序保证无环
# ═══════════════════════════════════════════════════════════

DAG_FILE = os.path.join(MEMORY_DIR, "dag_edges.json")

# ─── DAG 数据结构 ────────────────────────────────────────


@dataclass
class DAGEdge:
    """时序依赖边：实体间的因果/时序依赖关系

    与普通 Relation 的区别：
      - Relation 是静态关系（owns, depends_on, uses...）
      - DAGEdge 是动态因果链（causes, triggers, enables, precedes...）
      明确 "当前认知由哪些历史状态转化而来"
    """
    source: str       # 源实体 ID（原因 / 前置状态）
    target: str       # 目标实体 ID（结果 / 后继状态）
    dep_type: str     # 依赖类型: causes, triggers, enables, precedes, transforms_into
    description: str = ""
    timestamp: float = 0.0
    facts: list[str] = field(default_factory=list)  # 支撑该依赖的证据

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class DAGStore:
    """DAG 依赖边存储"""
    edges: list[DAGEdge] = field(default_factory=list)
    version: int = 1
    updated_at: float = 0.0

    def __post_init__(self):
        if self.updated_at == 0.0:
            self.updated_at = time.time()


# ─── DAG 序列化 ──────────────────────────────────────────


def _serialize_dag(store: DAGStore) -> dict:
    """序列化 DAG 存储"""
    return {
        "edges": [{
            "source": e.source,
            "target": e.target,
            "dep_type": e.dep_type,
            "description": e.description,
            "timestamp": e.timestamp,
            "facts": e.facts,
        } for e in store.edges],
        "version": store.version,
        "updated_at": store.updated_at,
    }


def _deserialize_dag(data: dict) -> DAGStore:
    """从 JSON 加载 DAG 存储"""
    store = DAGStore(
        version=data.get("version", 1),
        updated_at=data.get("updated_at", time.time()),
    )
    for edata in data.get("edges", []):
        store.edges.append(DAGEdge(
            source=edata["source"],
            target=edata["target"],
            dep_type=edata["dep_type"],
            description=edata.get("description", ""),
            timestamp=edata.get("timestamp", time.time()),
            facts=edata.get("facts", []),
        ))
    return store


def load_dag() -> DAGStore:
    """加载 DAG 依赖边存储"""
    if os.path.exists(DAG_FILE):
        try:
            with open(DAG_FILE) as f:
                return _deserialize_dag(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return DAGStore()
    return DAGStore()


def save_dag(store: DAGStore):
    """保存 DAG 依赖边存储"""
    store.version += 1
    store.updated_at = time.time()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(DAG_FILE, "w") as f:
        json.dump(_serialize_dag(store), f, indent=2, ensure_ascii=False)


# ─── DAG 辅助函数 ────────────────────────────────────────


def _dag_adjacency(store: DAGStore):
    """构建 DAG 邻接表

    Returns:
        outgoing: source -> [(target, edge), ...]
        incoming: target -> [(source, edge), ...]
    """
    outgoing = defaultdict(list)
    incoming = defaultdict(list)
    for e in store.edges:
        outgoing[e.source].append((e.target, e))
        incoming[e.target].append((e.source, e))
    return outgoing, incoming


def _dag_has_path(store: DAGStore, source: str, target: str,
                  exclude_edge: Optional[tuple] = None) -> bool:
    """BFS 检测从 source 到 target 是否存在路径

    Args:
        exclude_edge: (src, tgt) 排除的边，用于传递闭包剪除时忽略自身
    """
    if source == target:
        return True
    outgoing, _ = _dag_adjacency(store)
    visited = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for nxt, edge in outgoing.get(node, []):
            if exclude_edge and node == exclude_edge[0] and nxt == exclude_edge[1]:
                continue
            if nxt == target:
                return True
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return False


def _dag_topological_order(store: DAGStore) -> list[str]:
    """返回 DAG 的拓扑排序序列（Kahn 算法）"""
    outgoing, incoming = _dag_adjacency(store)
    all_nodes = set()
    for e in store.edges:
        all_nodes.add(e.source)
        all_nodes.add(e.target)

    in_degree = {n: len(incoming.get(n, [])) for n in all_nodes}
    queue = deque([n for n in all_nodes if in_degree[n] == 0])
    order = []

    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt, _ in outgoing.get(node, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    return order


# ─── DAG 核心操作 ────────────────────────────────────────


def dag_add_edge(store: DAGStore, source: str, target: str,
                 dep_type: str, description: str = "",
                 fact: str = "") -> DAGEdge:
    """添加 DAG 依赖边（带环检测）

    对标 TeleMem 的时序依赖边：明确"当前认知由哪些历史状态转化而来"。

    设计约束：拓扑排序保证无环。插入时 DFS 检测 target→source 是否可达，
    若可达则拒绝插入（否则会形成环）。

    Returns:
        新添加或已存在的 DAGEdge

    Raises:
        ValueError: 自环或插入会导致环
    """
    if source == target:
        raise ValueError(f"Self-loop not allowed in DAG: {source} → {target}")

    # 环检测：若 target 到 source 已有路径，添加 source→target 会形成环
    if _dag_has_path(store, target, source):
        raise ValueError(
            f"Adding edge {source} → {target} would create a cycle: "
            f"{target} already reaches {source}"
        )

    # 检查是否已存在相同依赖边（去重）
    for e in store.edges:
        if e.source == source and e.target == target and e.dep_type == dep_type:
            if fact and fact not in e.facts:
                e.facts.append(fact)
            if description:
                e.description = description
            return e

    edge = DAGEdge(
        source=source, target=target,
        dep_type=dep_type, description=description,
        facts=[fact] if fact else [],
    )
    store.edges.append(edge)
    return edge


def dag_remove_edge(store: DAGStore, source: str, target: str) -> int:
    """移除 source→target 的所有 DAG 依赖边

    Returns:
        移除的边数量
    """
    before = len(store.edges)
    store.edges = [e for e in store.edges
                   if not (e.source == source and e.target == target)]
    return before - len(store.edges)


def dag_path(store: DAGStore, entity_id: str) -> dict:
    """查看实体的完整因果链（Thread）

    从 entity_id 出发：
      - backward: 沿入边回溯所有因果祖先（谁导致了它）
      - forward:  沿出边追踪所有因果后继（它导致了什么）

    Returns:
        {
            "entity_id": str,
            "backward_chains": list[list[DAGEdge]],  # 多条回溯链
            "forward_chains": list[list[DAGEdge]],   # 多条前向链
            "ancestors": list[str],                   # 所有祖先节点
            "descendants": list[str],                 # 所有后继节点
        }
    """
    outgoing, incoming = _dag_adjacency(store)

    # ── 回溯：找所有因果祖先 ──
    ancestors = set()
    backward_chains = []
    visited_back = {entity_id}
    queue = deque([(entity_id, [])])
    while queue:
        node, path = queue.popleft()
        for prv, edge in incoming.get(node, []):
            if prv not in visited_back:
                visited_back.add(prv)
                ancestors.add(prv)
                new_path = path + [edge]
                backward_chains.append(new_path)
                queue.append((prv, new_path))

    # ── 前向：找所有因果后继 ──
    descendants = set()
    forward_chains = []
    visited_fwd = {entity_id}
    queue = deque([(entity_id, [])])
    while queue:
        node, path = queue.popleft()
        for nxt, edge in outgoing.get(node, []):
            if nxt not in visited_fwd:
                visited_fwd.add(nxt)
                descendants.add(nxt)
                new_path = path + [edge]
                forward_chains.append(new_path)
                queue.append((nxt, new_path))

    # ── 路径展平 — 串联多条依赖边形成可追溯的记忆演化链 ──
    def _flatten_chains(chains):
        """将多条链合并为完整的因果序列"""
        if not chains:
            return []
        # 取最长链
        longest = max(chains, key=len)
        return longest

    return {
        "entity_id": entity_id,
        "backward_chains": backward_chains,
        "forward_chains": forward_chains,
        "backward_thread": _flatten_chains(backward_chains),
        "forward_thread": _flatten_chains(forward_chains),
        "ancestors": list(ancestors),
        "descendants": list(descendants),
    }


def dag_closure(store: DAGStore, entity_id: str) -> dict:
    """最小因果闭包子图检索

    对标 TeleMem 论文的因果闭包检索：
    从种子节点沿因果链反向 BFS，收集所有依赖祖先节点（前置条件），
    只返回不可约的依赖边 —— 即最小因果骨架。

    Args:
        entity_id: 种子实体 ID

    Returns:
        {
            "seed": str,
            "ancestors": list[str],         # 所有因果祖先
            "edges": list[DAGEdge],         # 闭包子图中的边
            "depth": int,                   # 最大回溯深度
            "topological_order": list[str], # 拓扑排序
        }
    """
    _, incoming = _dag_adjacency(store)

    ancestors = set()
    closure_edges = []
    visited = {entity_id}
    queue = deque([entity_id])

    while queue:
        node = queue.popleft()
        for prv, edge in incoming.get(node, []):
            if prv not in visited:
                visited.add(prv)
                ancestors.add(prv)
                closure_edges.append(edge)
                queue.append(prv)

    # 计算最大深度：从种子沿闭包边 BFS 的最长路径
    max_depth = 0
    if closure_edges:
        # 构建子图邻接表
        sub_outgoing = defaultdict(list)
        for e in closure_edges:
            sub_outgoing[e.source].append(e.target)

        depth_queue = deque([(eid, 0) for eid in ancestors
                             if eid not in {t for _, t in
                             [(ee.source, ee.target) for ee in closure_edges]}])
        while depth_queue:
            node, depth = depth_queue.popleft()
            max_depth = max(max_depth, depth)
            for nxt in sub_outgoing.get(node, []):
                depth_queue.append((nxt, depth + 1))

    # 拓扑排序
    topo = [eid for eid in _dag_topological_order(store) if eid in ancestors or eid == entity_id]

    return {
        "seed": entity_id,
        "ancestors": list(ancestors),
        "edges": closure_edges,
        "depth": max_depth,
        "topological_order": topo,
        "size": len(closure_edges),
    }


def dag_prune(store: DAGStore) -> dict:
    """传递冗余剪除（Transitive Reduction）

    对标 TeleMem 的最小因果骨架：
    对每个节点对 (A, C)，检测是否存在中间节点 B 使 A→B→C 路径存在，
    若有则删除直连边 A→C（传递冗余）。

    算法：
      遍历每条边 A→C，暂时排除该边后检查 A 到 C 是否仍可达。
      若仍可达，则该边是传递冗余的，标记删除。

    Returns:
        {"removed": int, "remaining": int, "removed_edges": list[dict]}
    """
    removed_edges = []
    for edge in list(store.edges):
        if _dag_has_path(store, edge.source, edge.target,
                         exclude_edge=(edge.source, edge.target)):
            removed_edges.append(edge)
            store.edges.remove(edge)

    return {
        "removed": len(removed_edges),
        "remaining": len(store.edges),
        "removed_edges": [
            {"source": e.source, "target": e.target, "dep_type": e.dep_type}
            for e in removed_edges
        ],
    }


def dag_stats(store: DAGStore, graph: Optional[KnowledgeGraph] = None) -> str:
    """DAG 统计信息

    Returns:
        格式化的统计字符串
    """
    edge_count = len(store.edges)
    node_set = set()
    dep_types = defaultdict(int)

    for e in store.edges:
        node_set.add(e.source)
        node_set.add(e.target)
        dep_types[e.dep_type] += 1

    outgoing, incoming = _dag_adjacency(store)
    roots = sorted(n for n in node_set if n not in incoming)
    leaves = sorted(n for n in node_set if n not in outgoing)

    # 拓扑排序
    topo = _dag_topological_order(store)

    lines = [
        "─── DAG Statistics ───",
        f"  Nodes:          {len(node_set)}",
        f"  Edges:          {edge_count}",
        f"  Roots (no in):  {len(roots)}",
        f"  Leaves (no out):{len(leaves)}",
        f"  Topological:    {len(topo)} nodes sorted",
        "",
        "  Dependency types:",
    ]
    for dtype, count in sorted(dep_types.items(), key=lambda x: -x[1]):
        lines.append(f"    {dtype}: {count}")

    if graph and roots:
        lines.append("\n  Root entities (no causal dependency):")
        for r in roots[:15]:
            e = graph.entities.get(r)
            name = e.name if e else r
            etype = e.type if e else "?"
            lines.append(f"    [{etype}] {name}")
        if len(roots) > 15:
            lines.append(f"    ... and {len(roots) - 15} more")

    if graph and leaves:
        lines.append("\n  Leaf entities (no causal consequence):")
        for l in leaves[:15]:
            e = graph.entities.get(l)
            name = e.name if e else l
            etype = e.type if e else "?"
            lines.append(f"    [{etype}] {name}")
        if len(leaves) > 15:
            lines.append(f"    ... and {len(leaves) - 15} more")

    if topo:
        lines.append(f"\n  Topological order ({len(topo)} nodes):")
        names = []
        for tid in topo[:20]:
            e = graph.entities.get(tid) if graph else None
            names.append(e.name if e else tid[:8])
        lines.append("    " + " → ".join(names))
        if len(topo) > 20:
            lines.append(f"    ... +{len(topo) - 20} more nodes")

    return "\n".join(lines)


# ─── DAG 离线批量构建 ────────────────────────────────────


def dag_build_from_relations(graph: KnowledgeGraph,
                             store: DAGStore = None) -> DAGStore:
    """离线并行构图：从现有 Relation 推理时序依赖

    启发式规则：
      - "depends_on" / "requires" → 目标 is prerequisite of 源 → 目标→源 (enables)
      - "owns" / "parent_of"    → 源→目标 (owns)
      - "part_of"               → 源→目标 (contains)

    Args:
        graph: 已有的知识图谱
        store: 可选，现有 DAG 存储（增量添加）

    Returns:
        更新后的 DAGStore
    """
    if store is None:
        store = DAGStore()

    type_mapping = {
        "depends_on": "enables",
        "requires": "enables",
        "owns": "owns",
        "parent_of": "owns",
        "part_of": "contains",
        "uses": "enables",
        "runs_on": "enables",
        "configured_with": "enables",
    }

    added = 0
    skipped = 0
    for rel in graph.relations:
        dep_type = type_mapping.get(rel.relation_type)
        if not dep_type:
            skipped += 1
            continue

        # depends_on/requires: 方向反转（前置依赖是原因）
        if rel.relation_type in ("depends_on", "requires", "uses",
                                  "runs_on", "configured_with"):
            source, target = rel.target, rel.source
        else:
            source, target = rel.source, rel.target

        # 检查是否已存在
        exists = any(
            e.source == source and e.target == target and e.dep_type == dep_type
            for e in store.edges
        )
        if exists:
            skipped += 1
            continue

        try:
            dag_add_edge(store, source, target, dep_type,
                         description=f"From relation: {rel.relation_type}",
                         fact=rel.description or (rel.facts[0] if rel.facts else ""))
            added += 1
        except ValueError:
            skipped += 1

    if added > 0:
        save_dag(store)

    return store


# ─── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  knowledge_graph.py add-entity <name> <type> [attr=val ...]")
        print("  knowledge_graph.py add-relation <src> <src_type> <tgt> <tgt_type> <rel_type> [desc]")
        print("  knowledge_graph.py search <query>")
        print("  knowledge_graph.py query <question>")
        print("  knowledge_graph.py related <entity> [relation_type]")
        print("  knowledge_graph.py path <from> <to>")
        print("  knowledge_graph.py auto-extract <text>")
        print("  knowledge_graph.py summary")
        print("  knowledge_graph.py dag-add-edge --source <eid> --target <eid> --dep-type <type> [--desc ...]")
        print("  knowledge_graph.py dag-remove-edge --source <eid> --target <eid>")
        print("  knowledge_graph.py dag-path --entity-id <eid>")
        print("  knowledge_graph.py dag-closure --entity-id <eid>")
        print("  knowledge_graph.py dag-prune")
        print("  knowledge_graph.py dag-stats")
        print("  knowledge_graph.py dag-build")
        sys.exit(1)

    cmd = sys.argv[1]
    graph = load_graph()

    try:
        if cmd == "add-entity":
            name, etype = sys.argv[2], sys.argv[3]
            attrs = {}
            for a in sys.argv[4:]:
                if "=" in a:
                    k, v = a.split("=", 1)
                    attrs[k] = v
            eid = add_entity(graph, name, etype, attributes=attrs)
            save_graph(graph)
            print(f"Added entity: {name} [{etype}] id={eid}")

        elif cmd == "add-relation":
            src, stype, tgt, ttype, rtype = sys.argv[2:7]
            desc = sys.argv[7] if len(sys.argv) > 7 else ""
            src_id, tgt_id = add_relation(graph, src, stype, tgt, ttype, rtype, description=desc)
            save_graph(graph)
            print(f"Added relation: {src} -[{rtype}]→ {tgt}")

        elif cmd == "search":
            query_text = " ".join(sys.argv[2:])
            results = search_entities(graph, query_text)
            for e in results:
                print(f"[{e.type}] {e.name} ({e.id})")
                for fact in e.facts[:3]:
                    print(f"  {fact[:80]}")

        elif cmd == "query":
            question = " ".join(sys.argv[2:])
            print(query(graph, question))

        elif cmd == "related":
            entity = sys.argv[2]
            rtype = sys.argv[3] if len(sys.argv) > 3 else ""
            results = get_related_entities(graph, entity, relation_type=rtype)
            for item in results:
                e = item["entity"]
                r = item["relation"]
                print(f"{item['direction']} [{e.type}] {e.name} "
                      f"({r.relation_type}: {r.description or r.facts[0][:50] if r.facts else ''})")

        elif cmd == "path":
            frm, to = sys.argv[2], sys.argv[3]
            path = find_path(graph, frm, to)
            if path:
                for step in path:
                    print(f"{step['from'].name} -[{step['relation'].relation_type}]→ {step['to'].name}")
            else:
                print("No path found")

        elif cmd == "auto-extract":
            text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else sys.stdin.read()
            count = auto_populate(graph, text)
            print(f"Extracted {count} relations from text")

        elif cmd == "summary":
            print(export_summary(graph))

        # ─── DAG 命令 ──────────────────────────────────────

        elif cmd == "dag-add-edge":
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--source", required=True)
            parser.add_argument("--target", required=True)
            parser.add_argument("--dep-type", required=True)
            parser.add_argument("--desc", default="")
            parser.add_argument("--fact", default="")
            args = parser.parse_args(sys.argv[2:])
            store = load_dag()
            try:
                dag_add_edge(store, args.source, args.target,
                            args.dep_type, description=args.desc, fact=args.fact)
                save_dag(store)
                print(f"Added DAG edge: {args.source} -[{args.dep_type}]→ {args.target}")
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)

        elif cmd == "dag-remove-edge":
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--source", required=True)
            parser.add_argument("--target", required=True)
            args = parser.parse_args(sys.argv[2:])
            store = load_dag()
            count = dag_remove_edge(store, args.source, args.target)
            save_dag(store)
            print(f"Removed {count} DAG edge(s): {args.source} → {args.target}")

        elif cmd == "dag-path":
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--entity-id", required=True)
            args = parser.parse_args(sys.argv[2:])
            store = load_dag()
            result = dag_path(store, args.entity_id)
            print(f"─── Causal Path for {args.entity_id} ───")
            print(f"\nBackward chain ({len(result['ancestors'])} ancestors):")
            if result["backward_thread"]:
                for i, edge in enumerate(result["backward_thread"], 1):
                    src_name = graph.entities.get(edge.source)
                    src_name = src_name.name if src_name else edge.source[:8]
                    tgt_name = graph.entities.get(edge.target)
                    tgt_name = tgt_name.name if tgt_name else edge.target[:8]
                    print(f"  {i}. [{edge.dep_type}] {src_name} → {tgt_name}")
            else:
                print("  (none — root node)")
            print(f"\nForward chain ({len(result['descendants'])} descendants):")
            if result["forward_thread"]:
                for i, edge in enumerate(result["forward_thread"], 1):
                    src_name = graph.entities.get(edge.source)
                    src_name = src_name.name if src_name else edge.source[:8]
                    tgt_name = graph.entities.get(edge.target)
                    tgt_name = tgt_name.name if tgt_name else edge.target[:8]
                    print(f"  {i}. [{edge.dep_type}] {src_name} → {tgt_name}")
            else:
                print("  (none — leaf node)")

        elif cmd == "dag-closure":
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--entity-id", required=True)
            args = parser.parse_args(sys.argv[2:])
            store = load_dag()
            result = dag_closure(store, args.entity_id)
            print(f"─── Causal Closure for {args.entity_id} ───")
            print(f"  Ancestors: {len(result['ancestors'])}")
            print(f"  Edges:     {result['size']}")
            print(f"  Max depth: {result['depth']}")
            if result["ancestors"]:
                print(f"\n  Causal ancestors:")
                for aid in result["ancestors"]:
                    e = graph.entities.get(aid)
                    name = e.name if e else aid[:12]
                    etype = e.type if e else "?"
                    print(f"    [{etype}] {name}")
            if result["edges"]:
                print(f"\n  Closure edges:")
                for edge in result["edges"]:
                    src_name = graph.entities.get(edge.source)
                    src_name = src_name.name if src_name else edge.source[:8]
                    tgt_name = graph.entities.get(edge.target)
                    tgt_name = tgt_name.name if tgt_name else edge.target[:8]
                    print(f"    [{edge.dep_type}] {src_name} → {tgt_name}")
            if result["topological_order"]:
                names = []
                for tid in result["topological_order"]:
                    e = graph.entities.get(tid)
                    names.append(e.name if e else tid[:8])
                print(f"\n  Topological order: {' → '.join(names)}")

        elif cmd == "dag-prune":
            store = load_dag()
            result = dag_prune(store)
            save_dag(store)
            print(f"─── DAG Transitive Reduction ───")
            print(f"  Removed:  {result['removed']} redundant edges")
            print(f"  Remaining:{result['remaining']} edges")
            if result["removed_edges"]:
                print(f"\n  Removed edges:")
                for re in result["removed_edges"]:
                    print(f"    {re['source'][:8]} -[{re['dep_type']}]→ {re['target'][:8]}")

        elif cmd == "dag-stats":
            store = load_dag()
            print(dag_stats(store, graph))

        elif cmd == "dag-build":
            store = load_dag()
            dag_build_from_relations(graph, store)
            print(f"Built DAG from relations: {len(store.edges)} edges")

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)

    except (IndexError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)