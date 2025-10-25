

from ..adapters.aerodrome import AerodromeAdapter
from ..config import get_settings


def tick_spacing_candidates(ad: AerodromeAdapter) -> list[int]:
    # 1) tente spacings do .env
    s = get_settings()
    if s.AERO_TICK_SPACINGS:
        try:
            lst = [int(x.strip()) for x in s.AERO_TICK_SPACINGS.split(",") if x.strip()]
            if lst: return lst
        except Exception:
            pass
    # 2) sempre considere o spacing do pool atual (existe 100%)
    cand = { int(ad.pool_contract().functions.tickSpacing().call()) }
    # 3) defaults típicos
    for x in (1, 10, 60, 200):
        cand.add(x)
    return sorted(cand)