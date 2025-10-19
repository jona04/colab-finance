# apps/api/strategy/engine.py

"""
StrategyEngine
--------------
Loads strategies config, evaluates active handlers and returns proposals.

This is intentionally pure/stateless; callers inject (dex, alias, adapter).
"""

from typing import List, Dict, Any
from ..services.strategy_repo import load_strategies
from ..strategy.registry import handlers
from ..domain.strategy_models import StrategyProposal, StrategyDetails

def evaluate_strategies(dex: str, alias: str, adapter, extra_ctx: Dict[str, Any]) -> List[StrategyProposal]:
    cfg = load_strategies()
    proposals: List[StrategyProposal] = []
    for item in cfg.strategies:
        if not item.active:
            continue
        fn = handlers.get(item.id)
        if not fn:
            proposals.append(StrategyProposal(
                trigger=False, reason="handler not found", id=item.id, name=item.name or item.id
            ))
            continue

        ctx = {
            "dex": dex,
            "alias": alias,
            "adapter": adapter,
            **(extra_ctx or {}),
        }
        raw = fn(item.params.model_dump(), ctx) or {}
        # normalize
        proposals.append(
            StrategyProposal(
                trigger=bool(raw.get("trigger", False)),
                reason=raw.get("reason"),
                action=raw.get("action"),
                id=raw.get("id", item.id),
                name=raw.get("name", item.name or item.id),
                lower=raw.get("lower"),
                upper=raw.get("upper"),
                range_side=raw.get("range_side"),
                details=StrategyDetails(**(raw.get("details") or {})) if raw.get("details") else None,
            )
        )
    return proposals
