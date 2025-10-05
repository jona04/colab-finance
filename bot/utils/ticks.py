"""
Tick/percent utilities for Uniswap v3.
- pct_to_ticks: converts percentage distance to ticks using ln(1.0001)
- align_to_spacing: aligns a tick to the pool's tick spacing
"""

import math

LN_1_0001 = math.log(1.0001)

def pct_to_ticks(pct: float) -> int:
    """
    Convert a percentage (e.g., 1.0 = 1%) to a nearest tick distance.
    Uses: ticks = ln(1 + pct/100) / ln(1.0001)
    """
    if pct <= 0:
        return 0
    return int(round(math.log(1.0 + pct / 100.0) / LN_1_0001))

def align_to_spacing(tick: int, spacing: int, mode: str = "floor") -> int:
    """
    Align the given tick to a valid multiple of tick spacing.
    mode: "floor" | "ceil" | "nearest"
    """
    if spacing <= 0:
        return tick
    if mode == "ceil":
        # ceiling to next multiple
        return ((tick + spacing - 1) // spacing) * spacing
    if mode == "nearest":
        # round to nearest spacing multiple
        q = tick / spacing
        return int(round(q) * spacing)
    # default floor
    return (tick // spacing) * spacing
