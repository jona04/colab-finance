"""
CLI: show last N alerts stored in state.json
Usage:
    python -m bot.alerts [N]
"""

import sys
import json
from bot.utils.log import log_info

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    try:
        with open("bot/state.json", "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        log_info("No state.json found.")
        return

    alerts = state.get("alerts", [])
    if not alerts:
        log_info("No alerts recorded.")
        return

    log_info(f"Showing last {min(n, len(alerts))} alerts:")
    for a in alerts[-n:]:
        print(f"[{a['time']}] {a['id']} | {a['reason']} | {a.get('action','')}")

if __name__ == "__main__":
    main()
