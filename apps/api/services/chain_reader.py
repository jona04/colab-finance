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

USD_SYMBOLS = {"USDC", "USDbC", "USDCE", "USDT", "DAI", "USDD", "USDP", "BUSD"}  # extend if you need

@dataclass
class UsdPanel:
    usd_value: float
    delta_usd: float
    baseline_usd: float

def _pct_from_dtick(d: int) -> float:
    factor = pow(1.0001, abs(d))
    return (factor - 1.0) * 100.0

def _sqrtPriceX96_to_price_t1_per_t0(sqrtP: int, dec0: int, dec1: int) -> float:
    """
    Returns price as token1 per token0 (e.g., USDC per WETH if token0=WETH, token1=USDC).
    """
    ratio = Decimal(sqrtP) / Q96
    px = ratio * ratio
    scale = Decimal(10) ** (dec0 - dec1)
    return float(px * scale)

def _sqrtPriceX96_to_price(sqrtP: int, dec0: int, dec1: int) -> float:
    ratio = Decimal(sqrtP) / Q96
    px = ratio * ratio
    scale = Decimal(10) ** (dec0 - dec1)
    return float(px * scale)  # token1/token0 (ETH per USDC if order matches)

def _prices_from_tick(tick: int, dec0: int, dec1: int) -> Dict[str, float]:
    p_t1_t0 = pow(1.0001, tick) * pow(10.0, dec0 - dec1)  # token1/token0
    p_t0_t1 = float("inf") if p_t1_t0 == 0 else (1.0 / p_t1_t0)
    return {"tick": tick, "p_t1_t0": p_t1_t0, "p_t0_t1": p_t0_t1}

def _is_usd_symbol(sym: str) -> bool:
    try:
        return sym.upper() in USD_SYMBOLS
    except Exception:
        return False

def compute_status(adapter: UniswapV3Adapter, dex, alias: str) -> Dict[str, Any]:
    """
    Build a full "status" dict using the adapter.

    USD valuation rule:
      - If token1 is USD-like: USD = token0 * (token1/token0) + token1
      - If token0 is USD-like: USD = token1 * (token0/token1) + token0
      - Else: fallback to treat token1 as quote (approx): USD ~= token0 * (t1/t0) + token1
    """
    st = load_state(dex, alias)

    meta = adapter.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = meta["sym0"], meta["sym1"]
    spacing = int(meta["spacing"])

    sqrtP, tick = adapter.slot0()
    vstate = adapter.vault_state()
    lower, upper, liq = int(vstate["lower"]), int(vstate["upper"]), int(vstate["liq"])

    # prices
    p_t1_t0 = _sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)  # token1 per token0
    p_t0_t1 = (0.0 if p_t1_t0 == 0 else 1.0 / p_t1_t0)

    out_of_range = tick < lower or tick >= upper
    pct_outside_tick = _pct_from_dtick((lower - tick) if (out_of_range and tick < lower) else (tick - upper)) if out_of_range else 0.0

    # fees preview
    fees0 = fees1 = 0
    token_id = int(vstate.get("tokenId", 0) or 0)
    if token_id != 0:
        fees0, fees1 = adapter.call_static_collect(token_id, adapter.vault.address)

    fees0_h = float(fees0) / (10 ** dec0)
    fees1_h = float(fees1) / (10 ** dec1)
    # For fees USD we apply the same valuation rule below after we compute valuation inputs

    # inventory (idle + in-position)
    erc0 = adapter.erc20(meta["token0"])
    erc1 = adapter.erc20(meta["token1"])
    bal0_idle = int(erc0.functions.balanceOf(adapter.vault.address).call())
    bal1_idle = int(erc1.functions.balanceOf(adapter.vault.address).call())

    amt0_pos = amt1_pos = 0
    if liq > 0:
        a0, a1 = adapter.amounts_in_position_now(lower, upper, liq)
        amt0_pos, amt1_pos = int(a0), int(a1)

    # subtract cumul collected fees from "live stock"
    fees_cum = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})
    adj0_raw = max(0, (bal0_idle + amt0_pos) - int(fees_cum.get("token0_raw", 0) or 0))
    adj1_raw = max(0, (bal1_idle + amt1_pos) - int(fees_cum.get("token1_raw", 0) or 0))
    adj0_h = adj0_raw / (10 ** dec0)
    adj1_h = adj1_raw / (10 ** dec1)

    # ---------- USD valuation (order-agnostic) ----------
    token1_is_usd = _is_usd_symbol(sym1)
    token0_is_usd = _is_usd_symbol(sym0)

    if token1_is_usd:
        # token1 is USD-like (e.g., USDC). price = token1 per token0 (USDC per WETH)
        total_usd = adj0_h * p_t1_t0 + adj1_h
        fees_usd = fees0_h * p_t1_t0 + fees1_h
    elif token0_is_usd:
        # token0 is USD-like. price we need is token0 per token1
        total_usd = adj1_h * p_t0_t1 + adj0_h
        fees_usd = fees1_h * p_t0_t1 + fees0_h
    else:
        # Fallback: treat token1 as quote (like above)
        total_usd = adj0_h * p_t1_t0 + adj1_h
        fees_usd = fees0_h * p_t1_t0 + fees1_h

    baseline = st.get("vault_initial_usd")
    if baseline is None:
        baseline = total_usd
        st["vault_initial_usd"] = baseline
        save_state(dex, alias, st)  # <-- use the provided dex, not hardcoded

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
    range_side = "inside" if not out_of_range else ("below" if tick < lower else "above")

    return {
        "tick": tick,
        "lower": lower,
        "upper": upper,
        "spacing": spacing,
        "prices": prices_block,
        "fees_uncollected": {
            "token0": fees0_h, "token1": fees1_h,
            "usd": float(fees_usd), "sym0": sym0, "sym1": sym1
        },
        "out_of_range": out_of_range,
        "pct_outside_tick": pct_outside_tick,
        "usd_panel": asdict(usd_panel),
        "range_side": range_side,
        "sym0": sym0, "sym1": sym1,
    }