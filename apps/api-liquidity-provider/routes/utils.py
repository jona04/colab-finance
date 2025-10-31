

from ..domain.models import StatusCore
from ..services.chain_reader import compute_status
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

def snapshot_status(adapter, dex: str, alias: str) -> dict:
    """
    Returns a lightweight dict com saldos, faixa atual, fees pendentes etc
    pra comparação before/after.
    """
    core: StatusCore = compute_status(adapter, dex, alias)

    # valores principais que queremos comparar
    return {
        "tick": core.tick,
        "lower_tick": core.lower,
        "upper_tick": core.upper,
        "prices": {
            "p_t1_t0": float(core.prices.current.p_t1_t0),
            "p_t0_t1": float(core.prices.current.p_t0_t1),
        },
        "vault_idle": {
            "token0": core.holdings.vault_idle.token0,
            "token1": core.holdings.vault_idle.token1,
            "usd": core.holdings.vault_idle.usd,
        },
        "in_position": {
            "token0": core.holdings.in_position.token0,
            "token1": core.holdings.in_position.token1,
            "usd": core.holdings.in_position.usd,
        },
        "totals": {
            "token0": core.holdings.totals.token0,
            "token1": core.holdings.totals.token1,
            "usd": core.holdings.totals.usd,
        },
        "fees_uncollected": {
            "token0": core.fees_uncollected.token0,
            "token1": core.fees_uncollected.token1,
            "usd": core.fees_uncollected.usd,
        },
        "fees_collected_cum": {
            "token0": core.fees_collected_cum.token0,
            "token1": core.fees_collected_cum.token1,
            "usd": core.fees_collected_cum.usd,
        },
        "cooldown_active": core.cooldown_active,
        "cooldown_remaining_seconds": core.cooldown_remaining_seconds,
        "range_side": core.range_side,
        "usd_panel": {
            "usd_value": core.usd_panel.usd_value,
            "delta_usd": core.usd_panel.delta_usd,
            "baseline_usd": core.usd_panel.baseline_usd,
        },
    }