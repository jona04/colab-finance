"""
Simple persistent state manager for bot memory.
Stores entry_price, out_since, baseline_usd, last_signals, etc.
"""

import json
import os
from typing import Any

class StateManager:
    def __init__(self, filename: str = "state.json"):
        self.filename = filename
        self.data = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def save(self) -> None:
        with open(self.filename, "w") as f:
            json.dump(self.data, f, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def append_list(self, key: str, value: Any, cap: int = 100) -> None:
        arr = self.data.get(key, [])
        arr.append(value)
        self.data[key] = arr[-cap:]
        self.save()
