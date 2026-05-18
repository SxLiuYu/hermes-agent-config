#!/usr/bin/env python3
"""
Hermes Model Cascade — intelligent model routing based on task complexity.
Inspired by OpenClaw's model cascading: route simple tasks to cheap/fast models,
complex tasks to powerful models, with automatic fallback.

Architecture:
  LOCAL (free, fast)      → oMLX Qwen3.5-4B (simple queries)
  BUDGET (cheap)          → FinnA DeepSeek-V3.1 (moderate tasks)
  STANDARD (balanced)     → FinnA DeepSeek-V3.1 / GLM-4.6 (coding, analysis)
  PREMIUM (best quality)  → DeepSeek-V4-Pro / Kimi K2 (complex reasoning)

The cascade automatically:
  1. Classifies task complexity
  2. Routes to appropriate tier
  3. Falls back to next tier on failure
  4. Tracks per-model costs
"""

import json
import re
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from enum import Enum


# ─── Types ─────────────────────────────────────────────────────────────────

class TaskComplexity(Enum):
    TRIVIAL = 1     # "what time is it", "hello"
    SIMPLE = 2      # "read file X", "search for Y"
    MODERATE = 3    # "analyze this code", "explain concept Z"
    COMPLEX = 4     # "debug this bug", "design architecture"
    HEAVY = 5       # "refactor entire module", "deep research"


class ModelTier(Enum):
    LOCAL = 1       # Local models (free, oMLX)
    BUDGET = 2      # Cheap cloud models
    STANDARD = 3    # Default cloud models
    PREMIUM = 4     # Best quality models


@dataclass
class ModelConfig:
    """Configuration for a single model."""
    name: str
    provider: str  # omlx, finna, custom
    model_id: str
    tier: ModelTier
    cost_per_1k_input: float = 0
    cost_per_1k_output: float = 0
    avg_latency_ms: int = 500
    context_window: int = 8192
    supports_tools: bool = True
    supports_thinking: bool = False
    tags: List[str] = field(default_factory=list)  # ["coding", "creative", "analysis"]


@dataclass
class CascadeResult:
    """Result from a cascaded model call."""
    model_used: str
    tier: ModelTier
    content: str
    tokens_input: int = 0
    tokens_output: int = 0
    cost_estimate: float = 0
    latency_ms: float = 0
    fallback_attempts: int = 0
    fallback_chain: List[str] = field(default_factory=list)


# ─── Model Registry ────────────────────────────────────────────────────────

DEFAULT_MODELS = [
    # LOCAL tier
    ModelConfig(
        name="omlx-qwen35",
        provider="omlx",
        model_id="qwen3.5-4b",
        tier=ModelTier.LOCAL,
        cost_per_1k_input=0, cost_per_1k_output=0,
        avg_latency_ms=200,
        context_window=32768,
        supports_thinking=False,
        tags=["simple", "local", "quick"],
    ),
    # BUDGET tier
    ModelConfig(
        name="finna-deepseek-flash",
        provider="finna",
        model_id="deepseek-v4-flash",
        tier=ModelTier.BUDGET,
        cost_per_1k_input=0.001, cost_per_1k_output=0.002,
        avg_latency_ms=800,
        context_window=65536,
        supports_thinking=False,
        tags=["budget", "general"],
    ),
    # STANDARD tier
    ModelConfig(
        name="finna-deepseek-v3",
        provider="finna",
        model_id="deepseek-v3.1",
        tier=ModelTier.STANDARD,
        cost_per_1k_input=0.003, cost_per_1k_output=0.006,
        avg_latency_ms=1200,
        context_window=65536,
        supports_thinking=True,
        tags=["coding", "analysis", "general", "reasoning"],
    ),
    ModelConfig(
        name="finna-glm4",
        provider="finna",
        model_id="glm-4.6",
        tier=ModelTier.STANDARD,
        cost_per_1k_input=0.003, cost_per_1k_output=0.006,
        avg_latency_ms=1000,
        context_window=131072,
        supports_thinking=False,
        tags=["coding", "analysis", "long-context"],
    ),
    ModelConfig(
        name="finna-kimi-k2",
        provider="finna",
        model_id="kimi-k2",
        tier=ModelTier.STANDARD,
        cost_per_1k_input=0.005, cost_per_1k_output=0.01,
        avg_latency_ms=1500,
        context_window=131072,
        supports_thinking=True,
        tags=["coding", "analysis", "creative", "reasoning"],
    ),
    # PREMIUM tier
    ModelConfig(
        name="deepseek-v4-pro",
        provider="custom",
        model_id="deepseek-v4-pro",
        tier=ModelTier.PREMIUM,
        cost_per_1k_input=0.01, cost_per_1k_output=0.02,
        avg_latency_ms=3000,
        context_window=131072,
        supports_thinking=True,
        tags=["coding", "analysis", "creative", "reasoning", "complex"],
    ),
]


# ─── Task Classifier ───────────────────────────────────────────────────────

class TaskClassifier:
    """Classify task complexity to route to appropriate model tier."""

    # Complexity indicators
    TRIVIAL_PATTERNS = [
        r"^(hi|hello|hey|what'?s up|good morning|good evening)\b",
        r"\b(what time|what date|what day)\b",
        r"\b(thank|thanks|thx|ok|okay|got it|understood)\b",
        r"^(yes|no|yep|nope|sure)\b",
    ]

    SIMPLE_INDICATORS = [
        r"\b(read|show|list|display|cat|print)\s+(file|content|dir)",
        r"\b(search|find|look\s*up|google)\b",
        r"\b(what\s+is|define|explain\s+briefly)\b",
        r"\b(status|check|ping|health)\b",
        r"^\s*(how|what|when|where|who)\s+\w+\s*[?]?$",
    ]

    MODERATE_INDICATORS = [
        r"\b(analyze|review|inspect|examine|evaluate)\b",
        r"\b(explain|describe|summarize)\s+(in\s+detail|thoroughly)\b",
        r"\b(code|function|class|method|module)\b.+(review|check|fix)",
        r"\b(refactor|optimize|improve|enhance)\b",
        r"\b(compare|contrast|difference|vs)\b",
    ]

    COMPLEX_INDICATORS = [
        r"\b(debug|fix|resolve|troubleshoot)\s+(bug|error|issue|problem)\b",
        r"\b(design|architect|plan|blueprint)\b",
        r"\b(multi-step|complex|involved|sophisticated)\b",
        r"\b(implement|build|create)\s+.+\s+(system|pipeline|framework)\b",
        r"\b(refactor)\s+.+\s+(entire|whole|complete)\b",
    ]

    HEAVY_INDICATORS = [
        r"\b(research|investigate)\s+(deep|comprehensive|thorough)\b",
        r"\b(write|generate|create)\s+.+\s+(full|complete|entire|whole)\s+(project|app|system)\b",
        r"\b(migrate|rewrite|overhaul)\b",
        r"\b(论文|research\s*paper|academic|scientific)\b",
        r"\b(全部|所有|整个|完整)\s*(重构|重写|迁移|项目|系统)\b",
    ]

    # Domain-specific routing
    CODING_KEYWORDS = [
        r"\b(code|python|javascript|typescript|rust|go|java|c\+\+|bash|sql)\b",
        r"\b(debug|test|unit test|integration test|CI|CD|pipeline)\b",
        r"\b(function|class|module|package|import|export|async|await)\b",
        r"\b(git|commit|push|pull\s*request|merge|rebase)\b",
        r"\b(API|endpoint|route|handler|controller|middleware)\b",
    ]

    CREATIVE_KEYWORDS = [
        r"\b(write|compose|create|generate)\s+(story|poem|song|article|blog|post|content)\b",
        r"\b(brainstorm|ideate|come\s*up\s*with|creative)\b",
        r"\b(文案|创作|写|故事|诗|歌|文章)\b",
    ]

    ANALYSIS_KEYWORDS = [
        r"\b(analyze|analysis|statistics|data|trend|pattern)\b",
        r"\b(compare|contrast|evaluate|assess|review)\b",
        r"\b(backtest|back-test|strategy|indicator|signal)\b",
    ]

    def classify(self, task: str, history_tokens: int = 0) -> Tuple[TaskComplexity, str]:
        """
        Classify task complexity and domain.
        Returns (complexity, domain_tag).
        """
        task_lower = task.lower()
        task_len = len(task)

        # Factor 1: Pattern matching
        if any(re.search(p, task_lower) for p in self.TRIVIAL_PATTERNS):
            return TaskComplexity.TRIVIAL, "general"
        if any(re.search(p, task_lower) for p in self.HEAVY_INDICATORS):
            domain = self._get_domain(task_lower)
            return TaskComplexity.HEAVY, domain
        if any(re.search(p, task_lower) for p in self.COMPLEX_INDICATORS):
            domain = self._get_domain(task_lower)
            return TaskComplexity.COMPLEX, domain
        if any(re.search(p, task_lower) for p in self.MODERATE_INDICATORS):
            domain = self._get_domain(task_lower)
            return TaskComplexity.MODERATE, domain
        if any(re.search(p, task_lower) for p in self.SIMPLE_INDICATORS):
            return TaskComplexity.SIMPLE, "general"

        # Factor 2: Length heuristic
        if task_len < 20:
            return TaskComplexity.TRIVIAL, "general"
        if task_len > 2000:
            domain = self._get_domain(task_lower)
            return TaskComplexity.COMPLEX, domain
        if task_len > 500:
            domain = self._get_domain(task_lower)
            return TaskComplexity.MODERATE, domain

        # Factor 3: History context
        if history_tokens > 30000:
            domain = self._get_domain(task_lower)
            return TaskComplexity.COMPLEX, domain
        if history_tokens > 10000:
            domain = self._get_domain(task_lower)
            return TaskComplexity.MODERATE, domain

        # Default
        domain = self._get_domain(task_lower)
        return TaskComplexity.SIMPLE, domain

    def _get_domain(self, text: str) -> str:
        """Classify task domain: coding, creative, analysis, or general."""
        if any(re.search(p, text) for p in self.CODING_KEYWORDS):
            return "coding"
        if any(re.search(p, text) for p in self.CREATIVE_KEYWORDS):
            return "creative"
        if any(re.search(p, text) for p in self.ANALYSIS_KEYWORDS):
            return "analysis"
        return "general"


# ─── Cascade Router ────────────────────────────────────────────────────────

class CascadeRouter:
    """Routes tasks to models based on complexity, cost, and domain."""

    # Complexity → minimum tier mapping
    TIER_MAP = {
        TaskComplexity.TRIVIAL: ModelTier.LOCAL,
        TaskComplexity.SIMPLE: ModelTier.BUDGET,
        TaskComplexity.MODERATE: ModelTier.STANDARD,
        TaskComplexity.COMPLEX: ModelTier.STANDARD,
        TaskComplexity.HEAVY: ModelTier.PREMIUM,
    }

    # Domain → preferred model tags
    DOMAIN_TAGS = {
        "coding": ["coding", "reasoning"],
        "creative": ["creative", "reasoning"],
        "analysis": ["analysis", "reasoning"],
        "general": ["general"],
    }

    def __init__(self, models: Optional[List[ModelConfig]] = None):
        self.models = models or DEFAULT_MODELS
        self.classifier = TaskClassifier()
        self._fallback_counters: Dict[str, int] = {}
        self._cost_log: List[Dict] = []

    def route(self, task: str, history_tokens: int = 0,
              prefer_local: bool = False,
              prefer_cheap: bool = False) -> Tuple[ModelConfig, TaskComplexity, str]:
        """
        Route a task to the best model.
        Returns (model, complexity, domain).
        """
        complexity, domain = self.classifier.classify(task, history_tokens)

        # Determine minimum tier
        min_tier = self.TIER_MAP[complexity]

        # Override for cost-sensitive mode
        if prefer_cheap:
            min_tier = ModelTier(min(min_tier.value, ModelTier.BUDGET.value))
        if prefer_local:
            min_tier = ModelTier.LOCAL

        # Find matching models at appropriate tier
        preferred_tags = self.DOMAIN_TAGS.get(domain, ["general"])
        candidates = self._find_models(min_tier, preferred_tags)

        if not candidates:
            # Fallback: any model at this tier or above
            candidates = self._find_models(min_tier, ["general"])
        if not candidates:
            # Absolute fallback: any model
            candidates = self.models

        # Pick best: prefer domain match, then lowest cost
        best = candidates[0]
        return best, complexity, domain

    def get_fallback_chain(self, primary: ModelConfig,
                           complexity: TaskComplexity) -> List[ModelConfig]:
        """Get ordered fallback models if primary fails."""
        # Fallback: same tier different model, then next tier up
        chain = []

        # Same tier, different model
        for m in self.models:
            if m.tier == primary.tier and m.name != primary.name:
                chain.append(m)

        # Next tier(s) up — at least one fallback to STANDARD if on LOCAL/BUDGET
        if primary.tier.value < ModelTier.STANDARD.value:
            for m in self.models:
                if m.tier == ModelTier.STANDARD and m.name not in [c.name for c in chain]:
                    chain.append(m)
                    break  # One STANDARD fallback is enough

        # Premium as last resort for COMPLEX/HEAVY
        if complexity.value >= TaskComplexity.COMPLEX.value:
            for m in self.models:
                if m.tier == ModelTier.PREMIUM:
                    chain.append(m)
                    break

        return chain

    def record_call(self, model: ModelConfig, tokens_in: int, tokens_out: int):
        """Record model call for cost tracking."""
        cost = (tokens_in * model.cost_per_1k_input +
                tokens_out * model.cost_per_1k_output) / 1000
        entry = {
            "model": model.name,
            "tier": model.tier.name,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": round(cost, 6),
            "timestamp": time.time(),
        }
        self._cost_log.append(entry)

    def cost_summary(self) -> Dict:
        """Return cost summary by model and tier."""
        by_model: Dict[str, float] = {}
        by_tier: Dict[str, float] = {}
        total = 0.0

        for entry in self._cost_log:
            m = entry["model"]
            t = entry["tier"]
            c = entry["cost"]
            by_model[m] = by_model.get(m, 0) + c
            by_tier[t] = by_tier.get(t, 0) + c
            total += c

        return {
            "total_cost": round(total, 4),
            "total_calls": len(self._cost_log),
            "by_model": {k: round(v, 4) for k, v in by_model.items()},
            "by_tier": {k: round(v, 4) for k, v in by_tier.items()},
        }

    def _find_models(self, min_tier: ModelTier, preferred_tags: List[str]) -> List[ModelConfig]:
        """Find models at or above min_tier, sorted by tag match + cost."""
        candidates = []

        for m in self.models:
            if m.tier.value < min_tier.value:
                continue

            # Score by tag match
            tag_score = sum(1 for t in preferred_tags if t in m.tags)
            if tag_score == 0 and "general" not in m.tags:
                continue  # Skip if no matching tags and model isn't general-purpose

            candidates.append((tag_score, m.cost_per_1k_input + m.cost_per_1k_output, m))

        # Sort: highest tag score first, then lowest cost
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return [m for _, _, m in candidates]


# ─── Estimation Utilities ─────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for Chinese."""
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 2 + other_chars / 4)


def estimate_cost(model: ModelConfig, input_text: str,
                  expected_output_tokens: int = 500) -> float:
    """Estimate cost for a call to a model."""
    tokens_in = estimate_tokens(input_text)
    return (tokens_in * model.cost_per_1k_input +
            expected_output_tokens * model.cost_per_1k_output) / 1000


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Hermes Model Cascade — intelligent model routing"
    )
    sub = parser.add_subparsers(dest="command")

    # classify command
    classify = sub.add_parser("classify", help="Classify task complexity")
    classify.add_argument("task", help="Task description")
    classify.add_argument("--history-tokens", type=int, default=0,
                          help="Tokens in conversation history")

    # route command
    route = sub.add_parser("route", help="Route task to best model")
    route.add_argument("task", help="Task description")
    route.add_argument("--history-tokens", type=int, default=0)
    route.add_argument("--prefer-local", action="store_true")
    route.add_argument("--prefer-cheap", action="store_true")
    route.add_argument("--json", action="store_true", help="JSON output")

    # models command
    sub.add_parser("models", help="List all models in registry")

    # cost command
    cost_cmd = sub.add_parser("cost", help="Show cost summary (from log file)")
    cost_cmd.add_argument("--log", default="~/.hermes/cascade_cost.json",
                           help="Path to cost log file")

    # estimate command
    est = sub.add_parser("estimate", help="Estimate cost for a task")
    est.add_argument("task", help="Task description")
    est.add_argument("--model", default="finna-deepseek-v3",
                     help="Model to estimate for")

    args = parser.parse_args()

    if args.command == "classify":
        classifier = TaskClassifier()
        complexity, domain = classifier.classify(args.task, args.history_tokens)
        print(f"Task: {args.task[:100]}...")
        print(f"Complexity: {complexity.name} (level {complexity.value})")
        print(f"Domain: {domain}")

    elif args.command == "route":
        router = CascadeRouter()
        model, complexity, domain = router.route(
            args.task, args.history_tokens,
            prefer_local=args.prefer_local,
            prefer_cheap=args.prefer_cheap,
        )
        fallback = router.get_fallback_chain(model, complexity)

        if args.json:
            print(json.dumps({
                "task_preview": args.task[:200],
                "complexity": complexity.name,
                "complexity_level": complexity.value,
                "domain": domain,
                "primary_model": model.name,
                "primary_tier": model.tier.name,
                "cost_per_1k": {
                    "input": model.cost_per_1k_input,
                    "output": model.cost_per_1k_output,
                },
                "fallback_chain": [m.name for m in fallback],
                "estimated_cost": round(estimate_cost(model, args.task), 6),
            }, ensure_ascii=False, indent=2))
        else:
            print(f"Task: {args.task[:120]}...")
            print(f"Complexity: {complexity.name} | Domain: {domain}")
            print(f"→ Primary: {model.name} [{model.tier.name}]")
            print(f"  Cost: ${model.cost_per_1k_input}/1k in, ${model.cost_per_1k_output}/1k out")
            est = estimate_cost(model, args.task)
            print(f"  Estimated: ${est:.6f}")
            if fallback:
                print(f"  Fallback: {' → '.join(m.name for m in fallback)}")

    elif args.command == "models":
        print(f"{'Model':<25} {'Tier':<10} {'Input $/1k':>10} {'Output $/1k':>10} {'Tags'}")
        print("-" * 85)
        for m in DEFAULT_MODELS:
            print(f"{m.name:<25} {m.tier.name:<10} {m.cost_per_1k_input:>10.4f} "
                  f"{m.cost_per_1k_output:>10.4f} {', '.join(m.tags)}")

    elif args.command == "cost":
        log_path = Path(args.log).expanduser()
        if log_path.exists():
            data = json.loads(log_path.read_text())
            router = CascadeRouter()
            router._cost_log = data
            summary = router.cost_summary()
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"No cost log found at {log_path}")

    elif args.command == "estimate":
        router = CascadeRouter()
        model = next((m for m in DEFAULT_MODELS if m.name == args.model), None)
        if not model:
            print(f"Model '{args.model}' not found")
            sys.exit(1)
        est = estimate_cost(model, args.task)
        tokens = estimate_tokens(args.task)
        print(f"Model: {model.name} [{model.tier.name}]")
        print(f"Input: ~{tokens} tokens")
        print(f"Estimated cost: ${est:.6f}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()