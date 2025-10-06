"""
Lightweight Telegram client (HTTP only) para alertas.
- send_text / send_markdown
- dedupe/cooldown controlado externamente pelo StateManager
"""
from __future__ import annotations
import os
import time
import json
import requests
from typing import Optional, Dict, Any

from .utils.log import log_info, log_warn


class TelegramClient:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, timeout: int = 10):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout = timeout
        if not self.token or not self.chat_id:
            log_warn("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID). Alerts will be skipped.")
            self.enabled = False
        else:
            self.enabled = True
        self._base = f"https://api.telegram.org/bot{self.token}"

    def _post(self, method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            r = requests.post(f"{self._base}/{method}", json=payload, timeout=self.timeout)
            if r.status_code != 200:
                log_warn(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:300]}")
                return None
            data = r.json()
            if not data.get("ok"):
                log_warn(f"[TELEGRAM] API not ok: {json.dumps(data)[:300]}")
                return None
            return data
        except Exception as e:
            log_warn(f"[TELEGRAM] error: {e}")
            return None

    def send_text(self, msg: str, disable_web_page_preview: bool = True) -> Optional[int]:
        if not self.enabled:
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": msg,
            "disable_web_page_preview": disable_web_page_preview
        }
        resp = self._post("sendMessage", payload)
        if resp and "result" in resp:
            msg_id = resp["result"].get("message_id")
            log_info(f"[TELEGRAM] sent text msg_id={msg_id}")
            return msg_id
        return None

    def send_markdown(self, msg: str) -> Optional[int]:
        if not self.enabled:
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": msg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True
        }
        resp = self._post("sendMessage", payload)
        if resp and "result" in resp:
            msg_id = resp["result"].get("message_id")
            log_info(f"[TELEGRAM] sent md msg_id={msg_id}")
            return msg_id
        return None
