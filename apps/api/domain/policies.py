"""
Policy helpers reused across DEX adapters/endpoints.
"""

from dataclasses import dataclass
from time import time

@dataclass
class TwapGuard:
    window_sec: int
    max_deviation_ticks: int

    def ok(self, spot_tick: int, twap_tick: int) -> bool:
        return abs(spot_tick - twap_tick) <= self.max_deviation_ticks

def cooldown_ok(last_rebalance_ts: int | None, now_ts: int, min_cooldown_sec: int) -> bool:
    if not last_rebalance_ts:
        return True
    return (now_ts - int(last_rebalance_ts)) >= int(min_cooldown_sec)
