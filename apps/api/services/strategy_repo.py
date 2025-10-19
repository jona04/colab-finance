# apps/api/services/strategy_repo.py

"""
StrategyRepo
------------
Persistence for strategy configuration per DEX.
We store it under data/<dex>/strategies.json.

This module is intentionally thin; validation happens in Pydantic models.
"""

import json
from pathlib import Path
from typing import Optional
from ..domain.strategy_models import StrategiesConfig

BASE = Path(__file__).resolve().parents[3]  # repo root
DATA_DIR = BASE / "data"

def _path_for() -> Path:
    d = DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "strategies.json"

def load_strategies() -> StrategiesConfig:
    p = _path_for()
    if not p.exists():
        return StrategiesConfig()
    with p.open("r") as f:
        raw = json.load(f)
    return StrategiesConfig(**raw)

def save_strategies(cfg: StrategiesConfig) -> None:
    p = _path_for()
    with p.open("w") as f:
        json.dump(cfg.model_dump(), f, indent=2)
