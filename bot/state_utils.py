# bot/state_utils.py
"""
Small helpers to store per-vault state files.
Each vault alias persists into bot/state/<alias>.json
"""

import os
from pathlib import Path
from typing import Dict, Any
import json

_BASE = Path("bot/state")

def ensure_dir() -> None:
    """Create base state directory if missing."""
    _BASE.mkdir(parents=True, exist_ok=True)

def path_for(alias: str) -> Path:
    """Return state file path for a given alias: bot/state/<alias>.json"""
    ensure_dir()
    return _BASE / f"{alias}.json"

def load(alias: str) -> Dict[str, Any]:
    """Read state dict for alias (empty if missing)."""
    p = path_for(alias)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def save(alias: str, data: Dict[str, Any]) -> None:
    """Write state dict for alias."""
    p = path_for(alias)
    p.write_text(json.dumps(data, indent=2))
