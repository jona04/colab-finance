"""
Simple persistent state manager for bot memory.
Stores entry_price, out_since, baseline_usd, last_signals, etc.
"""

import json
import time
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

    # ---------------------
    # Alerts dedupe/cooldown
    # ---------------------
    def should_send_alert(self, alert_key: str, payload_hash: str,
                          cooldown_sec: int = 60, dedup_window_sec: int = 180) -> bool:
        """
        Return True se:
         - nunca enviou esse alert_key, ou
         - passou cooldown desde o último envio, e
         - payload mudou (ou se mudou dentro da janela de dedupe, ignorar)
        """
        now = int(time.time())
        alerts_meta = self.data.get("_alerts_meta", {})
        meta = alerts_meta.get(alert_key, {})

        last_ts = int(meta.get("last_ts", 0))
        last_hash = meta.get("last_hash", "")

        # dedupe: se payload igual dentro da janela, não envia
        if last_hash == payload_hash and (now - last_ts) < dedup_window_sec:
            return False

        # cooldown: se ainda no cooldown, não envia
        if (now - last_ts) < cooldown_sec:
            return False

        return True

    def mark_alert_sent(self, alert_key: str, payload_hash: str):
        alerts_meta = self.data.get("_alerts_meta", {})
        alerts_meta[alert_key] = {
            "last_ts": int(time.time()),
            "last_hash": payload_hash
        }
        self.data["_alerts_meta"] = alerts_meta
        self.save()