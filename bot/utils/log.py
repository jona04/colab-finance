"""
Simple colored logger for bot console and (future) Telegram output.
"""

from datetime import datetime

def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")

def log_info(msg: str):
    print(f"\033[94m[{_ts()}][INFO]\033[0m {msg}")

def log_warn(msg: str):
    print(f"\033[93m[{_ts()}][WARN]\033[0m {msg}")

def log_error(msg: str):
    print(f"\033[91m[{_ts()}][ERROR]\033[0m {msg}")
