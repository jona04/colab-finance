"""
High-level read service that builds the "status" panel:
- live prices
- in/out-of-range and % outside
- uncollected fees (callStatic)
- USD valuation (price-only V(P))
This reuses the math/flow from your bot.observer.VaultObserver, simplified here.
"""

from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Dict, Any, Tuple
from ..config import get_settings
from .state_repo import load_state, save_state
from ..adapters.uniswap_v3 import UniswapV3Adapter
getcontext().prec = 80
Q96 = Decimal(2) ** 96

@dataclass
class UsdPanel:
    usd_value: float
    delta_usd: float
    baseline_usd: float

def _pct_from_dtick(d: int) -> float:
    factor = pow(1.0001, abs(d))
    return (factor - 1.0) * 100.0

def _sqrtPriceX96_to_price(sqrtP: int, dec0: int, dec1: int) -> float:
    ratio = Decimal(sqrtP) / Q96
    px = ratio * ratio
    scale = Decimal(10) ** (dec0 - dec1)
    return float(px * scale)  # token1/token0 (ETH per USDC if order matches)

def _prices_from_tick(tick: int, dec0: int, dec1: int) -> Dict[str, float]:
    p_t1_t0 = pow(1.0001, tick) * pow(10.0, dec0 - dec1)
    p_t0_t1 = float("inf") if p_t1_t0 == 0 else (1.0 / p_t1_t0)
    return {"tick": tick, "p_t1_t0": p_t1_t0, "p_t0_t1": p_t0_t1}

def compute_status(adapter: UniswapV3Adapter, dex, alias: str) -> Dict[str, Any]:
    """
    Build a full "status" dict using the adapter.
    """
    s = get_settings()
    st = load_state(dex, alias)

    meta = adapter.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = meta["sym0"], meta["sym1"]
    spacing = int(meta["spacing"])

    sqrtP, tick = adapter.slot0()
    vstate = adapter.vault_state()
    lower, upper, liq = int(vstate["lower"]), int(vstate["upper"]), int(vstate["liq"])

    spot_price = _sqrtPriceX96_to_price(sqrtP, dec0, dec1)  # token1/token0
    out_of_range = tick < lower or tick >= upper
    if out_of_range:
        d_ticks = (lower - tick) if tick < lower else (tick - upper)
        pct_outside_tick = _pct_from_dtick(d_ticks)
    else:
        pct_outside_tick = 0.0

    # fees preview
    fees0 = fees1 = 0
    if int(vstate.get("tokenId", 0) or 0) != 0:
        fees0, fees1 = adapter.call_static_collect(vstate["tokenId"], adapter.vault.address)

    fees0_h = float(fees0) / (10 ** dec0)
    fees1_h = float(fees1) / (10 ** dec1)
    usdc_per_eth = float("inf") if spot_price == 0 else (1.0 / float(spot_price))
    fees_usd = fees0_h + fees1_h * usdc_per_eth

    # USD panel (price-only)
    # read inventory (idle + in-position)
    erc0 = adapter.erc20(meta["token0"])
    erc1 = adapter.erc20(meta["token1"])
    bal0_idle = erc0.functions.balanceOf(adapter.vault.address).call()
    bal1_idle = erc1.functions.balanceOf(adapter.vault.address).call()

    amt0_pos = amt1_pos = 0
    if liq > 0:
        a0, a1 = adapter.amounts_in_position_now(lower, upper, liq)
        amt0_pos, amt1_pos = int(a0), int(a1)

    # subtract cumul collected fees from "live stock"
    fees_cum = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})
    adj0 = max(0, (bal0_idle + amt0_pos) - int(fees_cum.get("token0_raw", 0) or 0))
    adj1 = max(0, (bal1_idle + amt1_pos) - int(fees_cum.get("token1_raw", 0) or 0))
    adj0_h = adj0 / (10 ** dec0)
    adj1_h = adj1 / (10 ** dec1)
    total_usd = adj0_h + adj1_h * usdc_per_eth

    baseline = st.get("vault_initial_usd", None)
    if baseline is None:
        baseline = total_usd
        st["vault_initial_usd"] = baseline
        save_state("uniswap", alias, st)  # same namespace used above

    usd_panel = UsdPanel(
        usd_value=float(total_usd),
        delta_usd=float(total_usd - float(baseline)),
        baseline_usd=float(baseline),
    )

    prices_block = {
        "current": _prices_from_tick(tick, dec0, dec1),
        "lower": _prices_from_tick(lower, dec0, dec1),
        "upper": _prices_from_tick(upper, dec0, dec1),
    }
    range_side = "inside"
    if out_of_range:
        range_side = "below" if tick < lower else "above"

    return {
        "tick": tick,
        "lower": lower,
        "upper": upper,
        "spacing": spacing,
        "prices": prices_block,
        "fees_uncollected": {"token0": fees0_h, "token1": fees1_h, "usd": fees_usd, "sym0": sym0, "sym1": sym1},
        "out_of_range": out_of_range,
        "pct_outside_tick": pct_outside_tick,
        "usd_panel": asdict(usd_panel),
        "range_side": range_side,
        "sym0": sym0, "sym1": sym1,
    }
