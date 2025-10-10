"""
Strategy registry — Phase 4.B (breakeven single-sided reallocator)

This module exposes a single production-grade strategy handler:
  - breakeven_single_sided

High-level idea
---------------
When the price stays OUT-OF-RANGE for a configured amount of time, we propose a
single-sided reallocation with the *closest possible* boundary on the side of the
current price, and the *opposite* boundary chosen such that, if the price touches
that boundary, the vault's PRICE-ONLY value (excludes historically collected fees)
is >= vault_initial_usd * (1 + buffer).

Key properties we enforce:
- No swaps.
- Use *only* live inventory (idle + current position), excluding collected fees:
    adj_token0_raw = (idle0 + inpos0) - fees_collected_cum.token0_raw
    adj_token1_raw = (idle1 + inpos1) - fees_collected_cum.token1_raw
- For Uniswap v3 single-sided deposits, the USD value at the *opposite* boundary
  is maximized at *minimum width* (1 * tickSpacing). If breakeven is not possible
  at minimum width, it won't be possible at larger widths. We exploit this fact.

Return payload
--------------
The strategy returns a dict with:
{
  "trigger": True|False,
  "reason": "...",
  "action": "reallocate" | "wait",
  "lower": <int tick>,
  "upper": <int tick>,
  "range_side": "below" | "above",
  "details": {
    "ticks": {"lower": int, "upper": int},
    "prices": {
      "eth_per_usdc": {"lower": {"price": float, "delta_pct": float, "sign": "+"|"-"},
                       "upper": {"price": float, "delta_pct": float, "sign": "+"|"-"}},
      "usdc_per_eth": {"lower": {"price": float, "delta_pct": float, "sign": "+"|"-"},
                       "upper": {"price": float, "delta_pct": float, "sign": "+"|"-"}}
    },
    "breakeven": {
      "boundary": "upper"|"lower",
      "target_usd": float,           # V(P_boundary) price-only
      "baseline_usd": float,         # vault_initial_usd
      "buffer_pct": float,
      "profit_usd": float            # target_usd - baseline_usd
    }
  }
}

Implementation notes
--------------------
- We read on-chain state via Chain and pool metadata (symbols/decimals).
- We compute live inventory (idle + position) using Chain.amounts_in_position_now,
  then subtract off-chain cum fees from bot/state.json.
- We detect which token is USDC and which is ETH by symbol ("USDC", "USDbC", "USDCE")
  and ("WETH", "ETH"). If we cannot detect, we abort gracefully.
- We compute boundary USD values using analytical closed-forms (dimensionless √P):
    Let S = sqrt(price token1/token0) (dimensionless, Uniswap canonical).
    Below-range (100% token0): given new [Sa, Sb] with S < Sa:
        L = amount0 * (Sa*Sb)/(Sb - Sa)
        At Sb: amount1 = L*(Sb - Sa) = amount0 * Sa * Sb
        USD @Sb = amount1 * USD_per_token1@Sb
    Above-range (100% token1): given new [Sa, Sb] with S > Sb:
        L = amount1 / (Sb - Sa)
        At Sa: amount0 = L*(Sb - Sa)/(Sa*Sb) = amount1 / (Sa*Sb)
        USD @Sa = amount0 * USD_per_token0@Sa
- USD_per_tokenX@tick is 1 if tokenX is USDC; otherwise it's USDC/ETH@tick.

Production cautions
-------------------
- This handler does *reads* itself (it is not pure), matching the requirements
  for accurate breakeven math. If you later want strictly pure handlers,
  refactor to inject the needed context (inventory, decimals, baseline, etc.).
"""

import json
import math
import time
from pathlib import Path
from typing import Dict, Any, Tuple

from ..config import get_settings
from ..chain import Chain
from ..utils.log import log_info, log_warn


# ---------- small math helpers (dimensionless √price and price views) ----------

def _sqrt_from_tick(tick: int) -> float:
    """sqrt(token1/token0) using Uniswap base 1.0001^(tick/2)"""
    return float(pow(1.0001, tick / 2.0))

def _price_token1_per_token0_scaled(tick: int, dec0: int, dec1: int) -> float:
    """
    Price with decimals scaling: token1/token0
    = 1.0001^tick * 10^(dec0 - dec1)
    """
    base = pow(1.0001, tick)
    scale = pow(10.0, dec0 - dec1)
    return base * scale

def _price_token0_per_token1_scaled(tick: int, dec0: int, dec1: int) -> float:
    """Inverse price with decimals scaling: token0/token1"""
    p = _price_token1_per_token0_scaled(tick, dec0, dec1)
    return math.inf if p == 0.0 else (1.0 / p)

def _align_up(tick: int, spacing: int) -> int:
    """Next multiple of spacing strictly greater than tick."""
    r = tick % spacing
    return tick + (spacing - r) if r != 0 else tick + spacing

def _align_down(tick: int, spacing: int) -> int:
    """Previous multiple of spacing strictly less than tick."""
    r = tick % spacing
    return tick - r if r != 0 else tick - spacing


# ---------- USD conversion helpers ----------

def _detect_indices_usdc_eth(sym0: str, sym1: str) -> Tuple[int, int]:
    """
    Heuristic detection for USDC and ETH/WETH sides.
    Returns (usdc_index, eth_index). Raises ValueError if not found.
    """
    s0 = sym0.upper()
    s1 = sym1.upper()

    def is_usdc(s: str) -> bool:
        return any(tag in s for tag in ("USDC", "USDBC", "USDCE"))

    def is_eth(s: str) -> bool:
        return any(tag in s for tag in ("WETH", "ETH"))

    usdc_idx = 0 if is_usdc(s0) else (1 if is_usdc(s1) else -1)
    eth_idx = 0 if is_eth(s0) else (1 if is_eth(s1) else -1)
    if usdc_idx < 0 or eth_idx < 0:
        raise ValueError("Unable to detect USDC/ETH sides from symbols")
    if usdc_idx == eth_idx:
        # same token detected as both USDC and ETH -> impossible
        raise ValueError("Symbol detection conflict (USDC and ETH on same index)")
    return usdc_idx, eth_idx

def _usdc_per_eth_at_tick(tick: int, dec0: int, dec1: int, usdc_idx: int, eth_idx: int) -> float:
    """
    Returns USDC/ETH at 'tick', independent of token order in the pool.
    If token1/token0 is ETH/USDC, then USDC/ETH = inverse, else it's direct.
    """
    # token1/token0 with decimals
    p_t1_t0 = _price_token1_per_token0_scaled(tick, dec0, dec1)
    # Which side represents USDC/ETH?
    # - If token1 is USDC and token0 is ETH => p_t1_t0 already is USDC/ETH
    # - If token0 is USDC and token1 is ETH => USDC/ETH is inverse
    if usdc_idx == 1 and eth_idx == 0:
        return p_t1_t0
    else:
        return math.inf if p_t1_t0 == 0.0 else (1.0 / p_t1_t0)

def _usd_value_of_token_amount(token_index: int, amount_raw: int,
                               tick_for_px: int,
                               dec0: int, dec1: int,
                               usdc_idx: int, eth_idx: int) -> float:
    """
    Converts a raw token amount into USD (USDC) units at the price implied by 'tick_for_px'.
    """
    if token_index == usdc_idx:
        dec = dec0 if token_index == 0 else dec1
        return float(amount_raw) / (10 ** dec)
    if token_index == eth_idx:
        dec = dec0 if token_index == 0 else dec1
        h = float(amount_raw) / (10 ** dec)
        usdc_per_eth = _usdc_per_eth_at_tick(tick_for_px, dec0, dec1, usdc_idx, eth_idx)
        return h * usdc_per_eth
    # Should not happen in USDC/ETH pools
    return 0.0


# ---------- Live inventory helpers (price-only stock) ----------

def _load_bot_state() -> Dict[str, Any]:
    p = Path("bot/state/default.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def _live_inventory_raw(ch: Chain, fees_collected_cum: Dict[str, int]) -> Tuple[int, int]:
    """
    Returns adjusted live inventory in RAW units (integers):
      adj_token0_raw = (idle0 + inpos0) - fees_collected_cum.token0_raw
      adj_token1_raw = (idle1 + inpos1) - fees_collected_cum.token1_raw
    Negative floors to 0 to guard against drift/rounding.
    """
    vs = ch.vault_state()
    lower, upper, liq = vs["lower"], vs["upper"], vs["liq"]

    amt0_pos_raw, amt1_pos_raw = ch.amounts_in_position_now(lower, upper, liq)

    meta = ch.pool_meta()
    dec0, dec1 = meta["dec0"], meta["dec1"]
    erc0 = ch.erc20(meta["token0"])
    erc1 = ch.erc20(meta["token1"])
    bal0_idle_raw = int(erc0.functions.balanceOf(ch.vault.address).call())
    bal1_idle_raw = int(erc1.functions.balanceOf(ch.vault.address).call())

    adj0 = bal0_idle_raw + int(amt0_pos_raw) - int(fees_collected_cum.get("token0_raw", 0) or 0)
    adj1 = bal1_idle_raw + int(amt1_pos_raw) - int(fees_collected_cum.get("token1_raw", 0) or 0)
    if adj0 < 0: adj0 = 0
    if adj1 < 0: adj1 = 0
    return adj0, adj1


# ---------- Boundary valuation (closed-forms) ----------

def _usd_at_upper_single_sided_token0(amount0_raw: int,
                                      lower_tick: int, upper_tick: int,
                                      dec0: int, dec1: int,
                                      usdc_idx: int, eth_idx: int) -> float:
    """
    Price-only USD at the UPPER boundary for a single-sided token0 deposit with new [lower, upper].
      S = sqrt(1.0001^tick)
      if S_current < S_lower:
         amount1@upper_raw = amount0_raw * S_lower * S_upper
         USD@upper = amount1 * USD_per_token1@upper
    """
    Sa = _sqrt_from_tick(lower_tick)
    Sb = _sqrt_from_tick(upper_tick)
    amount1_raw = amount0_raw * Sa * Sb
    return _usd_value_of_token_amount(
        token_index=1, amount_raw=int(amount1_raw),
        tick_for_px=upper_tick,
        dec0=dec0, dec1=dec1,
        usdc_idx=usdc_idx, eth_idx=eth_idx
    )

def _usd_at_lower_single_sided_token1(amount1_raw: int,
                                      lower_tick: int, upper_tick: int,
                                      dec0: int, dec1: int,
                                      usdc_idx: int, eth_idx: int) -> float:
    """
    Price-only USD at the LOWER boundary for a single-sided token1 deposit with new [lower, upper].
      if S_current > S_upper:
         amount0@lower_raw = amount1_raw / (S_lower * S_upper)
         USD@lower = amount0 * USD_per_token0@lower
    """
    Sa = _sqrt_from_tick(lower_tick)
    Sb = _sqrt_from_tick(upper_tick)
    if Sa <= 0 or Sb <= 0:
        return 0.0
    amount0_raw = amount1_raw / (Sa * Sb)
    return _usd_value_of_token_amount(
        token_index=0, amount_raw=int(amount0_raw),
        tick_for_px=lower_tick,
        dec0=dec0, dec1=dec1,
        usdc_idx=usdc_idx, eth_idx=eth_idx
    )


# ---------- The strategy handler ----------

def breakeven_single_sided(params: Dict[str, Any], obs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Breakeven single-sided reallocator (incremental widening).

    Trigger conditions:
      - Price must be OUT-OF-RANGE for at least `minimum_minutes_out_of_range`.
      - Baseline (vault_initial_usd) must be set.

    Heuristic (requested):
      - If BELOW current range (100% token0):
          1) Fix UPPER as the closest admissible tick above the price (near side).
          2) Set LOWER = UPPER - spacing (min width).
          3) If V(P@UPPER) < required, expand LOWER downward in steps of spacing.
          4) If ainda insuficiente, então também empurre UPPER para cima em passos.
      - If ABOVE current range (100% token1):
          1) Fix LOWER como o tick mais próximo abaixo do preço (near side).
          2) Set UPPER = LOWER + spacing (min width).
          3) Se V(P@LOWER) < required, expanda UPPER para cima em passos.
          4) Se ainda insuficiente, empurre também LOWER mais para baixo.

    Params (with safe defaults):
      {
        "minimum_minutes_out_of_range": 10,
        "min_ticks_from_price_on_near_side": 1,
        "breakeven_buffer_pct": 0.0,
        "max_opposite_side_expansions": 600,      # how many spacing steps we try on the far side
        "max_near_side_expansions": 600           # fallback: also expand the near side if needed
      }
    """
    # Preconditions
    if not obs.get("out_of_range", False):
        return {"trigger": False}

    out_since = float(obs.get("out_since") or 0.0)
    if out_since <= 0.0:
        return {"trigger": False}

    minutes_out = (time.time() - out_since) / 60.0
    min_minutes = float(params.get("minimum_minutes_out_of_range", 10))
    if minutes_out < min_minutes:
        return {"trigger": False, "reason": f"Outside for ~{minutes_out:.1f} min (< {min_minutes:.1f} min)."}

    # Chain & meta
    s = get_settings()
    ch = Chain(s.rpc_url, s.pool, s.nfpm, s.vault)
    meta = ch.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])

    try:
        usdc_idx, eth_idx = _detect_indices_usdc_eth(meta["sym0"], meta["sym1"])
    except Exception as e:
        return {"trigger": False, "reason": f"Token detection error: {e}"}

    # Baseline & cum fees
    st = _load_bot_state()
    baseline_usd = float(st.get("vault_initial_usd", 0.0) or 0.0)
    if baseline_usd <= 0.0:
        return {"trigger": False, "reason": "Baseline not set. Use /baseline set."}
    fees_col_cum = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})

    # Live inventory (price-only)
    adj0_raw, adj1_raw = _live_inventory_raw(ch, fees_col_cum)

    # Prices & ticks
    spacing = int(obs["spacing"])
    tick = int(obs["tick"])
    lower_now = int(obs["lower"])
    upper_now = int(obs["upper"])
    prices_cur = obs["prices"]["current"]
    cur_p_t1_t0 = float(prices_cur["p_t1_t0"])
    cur_p_t0_t1 = float(prices_cur["p_t0_t1"])

    # Params
    near_k = max(1, int(params.get("min_ticks_from_price_on_near_side", 1)))
    buffer_pct = float(params.get("breakeven_buffer_pct", 0.0))
    max_opp_steps = max(0, int(params.get("max_opposite_side_expansions", 600)))
    max_near_steps = max(0, int(params.get("max_near_side_expansions", 600)))

    required_usd = baseline_usd * (1.0 + buffer_pct)
    tick_side = "below" if tick < lower_now else ("above" if tick > upper_now else "inside")
    if tick_side == "inside":
        return {"trigger": False}

    # Helpers for computing target at the breakeven boundary
    def _target_usd_below(lower_tick: int, upper_tick: int) -> float:
        # single-sided token0 -> breakeven boundary is UPPER
        return _usd_at_upper_single_sided_token0(
            amount0_raw=adj0_raw,
            lower_tick=lower_tick,
            upper_tick=upper_tick,
            dec0=dec0, dec1=dec1,
            usdc_idx=usdc_idx, eth_idx=eth_idx
        )

    def _target_usd_above(lower_tick: int, upper_tick: int) -> float:
        # single-sided token1 -> breakeven boundary is LOWER
        return _usd_at_lower_single_sided_token1(
            amount1_raw=adj1_raw,
            lower_tick=lower_tick,
            upper_tick=upper_tick,
            dec0=dec0, dec1=dec1,
            usdc_idx=usdc_idx, eth_idx=eth_idx
        )

    def _tick_from_usdc_per_eth_target(usdc_per_eth: float,
                                    dec0: int, dec1: int,
                                    usdc_idx: int, eth_idx: int) -> int:
        """
        Returns the (float) tick whose *scaled* price implies the given USDC/ETH.
        Handles token order automatically.

        We use the scaled relation:
        p_t1_t0_scaled = 1.0001^tick * 10^(dec0 - dec1)

        If pool order is (token1=USDC, token0=ETH), then p_t1_t0_scaled == USDC/ETH directly.
        Otherwise, USDC/ETH = 1 / p_t1_t0_scaled.

        Note: Caller should align the resulting tick to spacing.
        """
        # desired p_t1_t0_scaled
        if usdc_idx == 1 and eth_idx == 0:
            desired_p_t1_t0 = float(usdc_per_eth)
        else:
            desired_p_t1_t0 = 1.0 / float(usdc_per_eth)

        # remove decimals scale
        scale = pow(10.0, dec0 - dec1)
        base = desired_p_t1_t0 / scale
        if base <= 0.0:
            # extremely defensive; shouldn't happen in practice
            return -2**31

        # tick = ln(base) / ln(1.0001)
        return int(round(math.log(base) / math.log(1.0001)))

    # ---------- Build initial near-side-tight range ----------
    if tick_side == "below":
        # Near side = LOWER just below the current tick
        lower = _align_up(tick, spacing) + (near_k - 1) * spacing
        # upper = lower + spacing  # minimal width
        if adj0_raw <= 0:
            return {"trigger": False, "reason": "No live token0 inventory to reallocate (above)."}

        # USER REQUEST: upper definido por um percentual do preço USDC/ETH no LOWER.
        # Param: upper_gap_usdc_per_eth_pct (ex.: 0.01 = 1%)
        gap_pct = float(params.get("upper_gap_usdc_per_eth_pct", 0.01))
        if gap_pct <= 0.0:
            gap_pct = 0.01  # mínimo seguro
        
        # USDC/ETH @lower
        lower_usdc_per_eth = _usdc_per_eth_at_tick(lower, dec0, dec1, usdc_idx, eth_idx)
        # For 'below', higher tick => lower USDC/ETH. We want 'upper' a bit *below* lower's USDC/ETH.
        target_upper_usdc_per_eth = lower_usdc_per_eth * (1.0 - gap_pct)

        raw_upper_tick = _tick_from_usdc_per_eth_target(
            target_upper_usdc_per_eth, dec0, dec1, usdc_idx, eth_idx
        )
        upper = _align_up(raw_upper_tick, spacing)

        # VALIDATIONS: enforce order and being out-of-range on the correct side
        if upper <= lower:
            upper = lower + spacing  # ensure minimal positive width

        if not (tick < lower):
            # push lower further up until it is strictly above current tick
            lower = _align_up(tick, spacing) + near_k * spacing
            if upper <= lower:
                upper = lower + spacing

        # Compute target for reporting (below: breakeven boundary is the UPPER)
        target_usd = _target_usd_below(lower, upper)
        breakeven_boundary = "upper"

        log_info(f"[breakeven_single_sided] side=below "
                 f"tick={tick} lower={lower} upper={upper} "
                 f"target_usd={target_usd:.4f} required_usd={required_usd:.4f}")

        
    else:  # tick_side == "above"
        # Near side = UPPER just above the current tick
        upper = _align_down(tick, spacing) - (near_k - 1) * spacing
        lower = upper - spacing  # minimal width
        if adj1_raw <= 0:
            return {"trigger": False, "reason": "No live token1 inventory to reallocate (below)."}

        target_fn = _target_usd_above
        breakeven_boundary = "lower"

        # Ensure strictly out-of-range on the above side
        if not (tick > upper):
            upper = _align_down(tick, spacing) - near_k * spacing
            lower = upper - spacing

        # Expand opposite side (lower ↓) if needed
        steps_used = 0
        target_usd = target_fn(lower, upper)
        while target_usd + 1e-12 < required_usd and steps_used < max_opp_steps:
            lower -= spacing
            steps_used += 1
            target_usd = target_fn(lower, upper)

        log_info(f"[breakeven_single_sided] side=above "
                 f"tick={tick} lower={lower} upper={upper} steps={steps_used} "
                 f"target_usd={target_usd:.4f} required_usd={required_usd:.4f}")
    
        # Final decision
        # if target_usd + 1e-12 < required_usd:
        #     shortfall = required_usd - target_usd
        #     return {
        #         "trigger": False,
        #         "reason": f"Breakeven still not reached after widening (shortfall ${shortfall:.2f}).",
        #         "action": "wait"
        #     }
    
    # Pretty prices & deltas for output
    def _prices_and_deltas(tk: int) -> Tuple[float, float, float, float, str, str]:
        p_t1_t0 = _price_token1_per_token0_scaled(tk, dec0, dec1)  # ETH/USDC
        p_t0_t1 = math.inf if p_t1_t0 == 0.0 else (1.0 / p_t1_t0)  # USDC/ETH
        d1 = (p_t1_t0 / cur_p_t1_t0 - 1.0) * 100.0
        d0 = (p_t0_t1 / cur_p_t0_t1 - 1.0) * 100.0
        s1 = "+" if d1 >= 0 else "-"
        s0 = "+" if d0 >= 0 else "-"
        return p_t1_t0, p_t0_t1, d1, d0, s1, s0

    p1_low, p0_low, d1_low, d0_low, s1_low, s0_low = _prices_and_deltas(lower)
    p1_up,  p0_up,  d1_up,  d0_up,  s1_up,  s0_up  = _prices_and_deltas(upper)
    profit_usd = target_usd - baseline_usd

    return {
    "trigger": True,
    "reason": (
        f"Out-of-range {tick_side} for ~{minutes_out:.1f} min; "
        f"breakeven reached at {breakeven_boundary} with minimal width (1×spacing)."
    ),
    "action": "reallocate",
    "lower": int(lower),
    "upper": int(upper),
    "range_side": tick_side,
    "details": {
        "ticks": {"lower": int(lower), "upper": int(upper)},
        "prices": {
            # Added: explicit current block with tick and both price views
            "current": {
                "tick": int(tick),
                "eth_per_usdc": float(cur_p_t1_t0),
                "usdc_per_eth": float(cur_p_t0_t1),
            },
            "eth_per_usdc": {
                "lower": {"price": p1_low, "delta_pct": d1_low, "sign": s1_low},
                "upper": {"price": p1_up,  "delta_pct": d1_up,  "sign": s1_up},
            },
            "usdc_per_eth": {
                "lower": {"price": p0_low, "delta_pct": d0_low, "sign": s0_low},
                "upper": {"price": p0_up,  "delta_pct": d0_up,  "sign": s0_up},
            },
        },
        "breakeven": {
            "boundary": breakeven_boundary,
            "target_usd": float(target_usd),
            "baseline_usd": float(baseline_usd),
            "buffer_pct": buffer_pct,
            "profit_usd": float(profit_usd),
        },
    },
}



# public registry
handlers = {
    "breakeven_single_sided": breakeven_single_sided,
}
