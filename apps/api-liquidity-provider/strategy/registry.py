# apps/api/strategy/registry.py

"""
Strategy registry for the API.

- Handlers receive:
    params: dict-like (StrategyParams.model_dump())
    context: dict with:
        alias, dex, adapter, status, state_repo (baseline/fees_cum)
  and must return a dict compatible with StrategyProposal.model fields.

Current handlers:
- "breakeven_single_sided"
"""

import math
import time
from typing import Dict, Any, Tuple
from ..domain.models import StatusCore
from ..services.chain_reader import compute_status
from ..services.state_repo import load_state
from ..adapters.uniswap_v3 import UniswapV3Adapter

USD_NAMES = {"USDC", "USDbC", "USDCE", "USDT", "DAI", "USDD", "USDP", "BUSD"}
ETH_NAMES = {"ETH", "WETH"}

def _is_usdc(sym: str) -> bool:
    return sym.upper() in USD_NAMES

def _is_eth(sym: str) -> bool:
    return sym.upper() in ETH_NAMES

def _detect_indices_usdc_eth(sym0: str, sym1: str) -> Tuple[int, int]:
    s0, s1 = sym0.upper(), sym1.upper()
    usdc_idx = 0 if _is_usdc(s0) else (1 if _is_usdc(s1) else -1)
    eth_idx  = 0 if _is_eth(s0)  else (1 if _is_eth(s1)  else -1)
    if usdc_idx < 0 or eth_idx < 0 or usdc_idx == eth_idx:
        raise ValueError("Unable to detect USDC/ETH sides from symbols")
    return usdc_idx, eth_idx

def _price_token1_per_token0_scaled(tick: int, dec0: int, dec1: int) -> float:
    base = pow(1.0001, tick)
    scale = pow(10.0, dec0 - dec1)
    return base * scale

def _usdc_per_eth_at_tick(tick: int, dec0: int, dec1: int, usdc_idx: int, eth_idx: int) -> float:
    p_t1_t0 = _price_token1_per_token0_scaled(tick, dec0, dec1)
    # If token1 is USDC and token0 is ETH => USDC/ETH = p_t1_t0 ; else inverse
    if usdc_idx == 1 and eth_idx == 0:
        return p_t1_t0
    return math.inf if p_t1_t0 == 0.0 else (1.0 / p_t1_t0)

def _align_up(tick: int, spacing: int) -> int:
    r = tick % spacing
    return tick + (spacing - r) if r != 0 else tick + spacing

def _align_down(tick: int, spacing: int) -> int:
    r = tick % spacing
    return tick - r if r != 0 else tick - spacing

def _tick_from_usdc_per_eth_target(usdc_per_eth: float, dec0: int, dec1: int,
                                   usdc_idx: int, eth_idx: int) -> int:
    # Convert desired USDC/ETH into p_t1_t0 scaled (considering token order)
    if usdc_idx == 1 and eth_idx == 0:
        desired_p_t1_t0 = float(usdc_per_eth)
    else:
        desired_p_t1_t0 = 1.0 / float(usdc_per_eth)
    scale = pow(10.0, dec0 - dec1)
    base = desired_p_t1_t0 / scale
    if base <= 0.0:
        return -2**31
    return int(round(math.log(base) / math.log(1.0001)))

def _prices_and_deltas(tk: int, dec0: int, dec1: int, cur_p_t1_t0: float, cur_p_t0_t1: float):
    p_t1_t0 = _price_token1_per_token0_scaled(tk, dec0, dec1)  # ETH/USDC
    p_t0_t1 = math.inf if p_t1_t0 == 0.0 else (1.0 / p_t1_t0)  # USDC/ETH
    d1 = (p_t1_t0 / cur_p_t1_t0 - 1.0) * 100.0
    d0 = (p_t0_t1 / cur_p_t0_t1 - 1.0) * 100.0
    s1 = "+" if d1 >= 0 else "-"
    s0 = "+" if d0 >= 0 else "-"
    return p_t1_t0, p_t0_t1, d1, d0, s1, s0

def breakeven_single_sided(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Proposes a single-sided range on the out-of-range side such that the
    opposite boundary achieves price-only breakeven >= baseline*(1+buffer).

    It uses:
      - adapter: UniswapV3Adapter
      - compute_status(): live prices, holdings, baseline
      - state_repo: for fees_cum + baseline fallback
    """
    alias = ctx["alias"]; dex = ctx["dex"]; ad: UniswapV3Adapter = ctx["adapter"]

    # status already has: tick/lower/upper/spacing/prices/holdings/usd_panel/out_of_range
    st: StatusCore = compute_status(ad, dex, alias)
    if not st.out_of_range:
        return {"trigger": False, "reason": "Price is inside the range."}

    # how long out-of-range? (we can persist this in state_repo if wanted; for now, simple immediate trigger)
    minutes_out = float(params.get("minimum_minutes_out_of_range", 10))
    # opcional: você pode calcular out_since e checar; aqui vamos assumir que UI/cron só chama quando faz sentido
    # para manter paridade com o bot, deixo uma checagem mínima:
    # (se quiser algo real, persistir out_since no state_repo quando detecta transição.)
    # Para já: não bloquear pela janela de tempo.
    _ = minutes_out

    meta = ad.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = meta["sym0"], meta["sym1"]

    usdc_idx, eth_idx = _detect_indices_usdc_eth(sym0, sym1)

    tick = st.tick
    lower_now = st.lower; upper_now = st.upper
    spacing = st.spacing
    pcur = st.prices.current
    cur_p_t1_t0 = pcur.p_t1_t0  # ETH/USDC
    cur_p_t0_t1 = pcur.p_t0_t1  # USDC/ETH
    range_side = st.range_side  # "below" | "above"

    # Params
    near_k = max(1, int(params.get("min_ticks_from_price_on_near_side", 1)))
    buffer_pct = float(params.get("breakeven_buffer_pct", 0.0))
    opp_steps_limit = max(0, int(params.get("max_opposite_side_expansions", 100)))
    gap_pct = float(params.get("upper_gap_usdc_per_eth_pct", 0.01))

    baseline = float(st["usd_panel"]["baseline_usd"] or 0.0)
    if baseline <= 0:
        return {"trigger": False, "reason": "Baseline not set yet."}
    required_usd = baseline * (1.0 + buffer_pct)

    # "Price-only" inventory (live). Usamos holdings.totals (já ajustado pela sua lógica).
    # Para single-sided breakeven, o lado relevante é:
    #   - below: token0 (USDC-like)
    #   - above: token1 (ETH-like)
    tot0 = float(st["holdings"]["totals"]["token0"])
    tot1 = float(st["holdings"]["totals"]["token1"])

    if range_side == "below":
        # Near side: LOWER > tick
        lower = _align_up(tick, spacing) + (near_k - 1) * spacing
        if gap_pct <= 0: gap_pct = 0.01
        lower_usdc_per_eth = _usdc_per_eth_at_tick(lower, dec0, dec1, usdc_idx, eth_idx)
        target_upper_usdc_per_eth = lower_usdc_per_eth * (1.0 - gap_pct)
        raw_upper_tick = _tick_from_usdc_per_eth_target(target_upper_usdc_per_eth, dec0, dec1, usdc_idx, eth_idx)
        upper = _align_up(raw_upper_tick, spacing)
        if upper <= lower:
            upper = lower + spacing
        if not (tick < lower):
            lower = _align_up(tick, spacing) + near_k * spacing
            if upper <= lower: upper = lower + spacing

        # USD @ upper (single-sided token0). Aproximação: tok1@upper ≈ tok0 * S_lower*S_upper;
        # Como já temos tot0 em unidades humanas, convertemos proporcionalmente com o mesmo fator.
        # Para evitar confusão, aplicamos a fórmula via "raw-like": usar dec0 para escalar e depois valor USDC/ETH@upper.
        Sa = math.sqrt(pow(1.0001, lower))
        Sb = math.sqrt(pow(1.0001, upper))
        # amount1 ≈ amount0 * Sa*Sb (em “mesma base”); convertemos p/ USD via USDC/ETH@upper.
        usdc_per_eth_upper = _usdc_per_eth_at_tick(upper, dec0, dec1, usdc_idx, eth_idx)
        # tot0 está em USDC (humano). Precisamos de ETH-equivalente @upper? Para manter simples:
        # USD_alvo ≈ (tot0 * Sa*Sb) * (USDC/ETH@upper)  -> mas tot0 está em USDC, não em “raw 0”.
        # A heurística do bot trabalhava com RAW. Aqui vamos aproximar mantendo a proporcionalidade:
        target_usd = tot0 * Sa * Sb * usdc_per_eth_upper

        p1_low, p0_low, d1_low, d0_low, s1_low, s0_low = _prices_and_deltas(lower, dec0, dec1, cur_p_t1_t0, cur_p_t0_t1)
        p1_up,  p0_up,  d1_up,  d0_up,  s1_up,  s0_up  = _prices_and_deltas(upper,  dec0, dec1, cur_p_t1_t0, cur_p_t0_t1)

        return {
            "trigger": target_usd + 1e-12 >= required_usd,
            "reason": (
                "Below-range: propose single-sided token0. "
                "Upper derived from USDC/ETH percentage gap off Lower."
            ),
            "action": "reallocate",
            "id": "breakeven_single_sided",
            "name": "Single-Sided Breakeven Reallocator",
            "lower": int(lower),
            "upper": int(upper),
            "range_side": "below",
            "details": {
                "ticks": {"lower": int(lower), "upper": int(upper)},
                "prices": {
                    "current": {"tick": int(tick), "eth_per_usdc": cur_p_t1_t0, "usdc_per_eth": cur_p_t0_t1},
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
                    "boundary": "upper",
                    "target_usd": float(target_usd),
                    "baseline_usd": float(baseline),
                    "buffer_pct": buffer_pct,
                    "profit_usd": float(target_usd - baseline),
                },
            },
        }

    else:  # "above"
        upper = _align_down(tick, spacing) - (near_k - 1) * spacing
        lower = upper - spacing
        # expand lower ↓ até bater required_usd (com limite)
        steps = 0
        Sa = math.sqrt(pow(1.0001, lower))
        Sb = math.sqrt(pow(1.0001, upper))
        usdc_per_eth_lower = _usdc_per_eth_at_tick(lower, dec0, dec1, usdc_idx, eth_idx)
        # USD_alvo ≈ (tot1 / (Sa*Sb)) * USDC/ETH@lower
        target_usd = (tot1 / max(1e-18, Sa * Sb)) * usdc_per_eth_lower
        while target_usd + 1e-12 < required_usd and steps < opp_steps_limit:
            lower -= spacing
            steps += 1
            Sa = math.sqrt(pow(1.0001, lower))
            usdc_per_eth_lower = _usdc_per_eth_at_tick(lower, dec0, dec1, usdc_idx, eth_idx)
            target_usd = (tot1 / max(1e-18, Sa * Sb)) * usdc_per_eth_lower

        p1_low, p0_low, d1_low, d0_low, s1_low, s0_low = _prices_and_deltas(lower, dec0, dec1, cur_p_t1_t0, cur_p_t0_t1)
        p1_up,  p0_up,  d1_up,  d0_up,  s1_up,  s0_up  = _prices_and_deltas(upper,  dec0, dec1, cur_p_t1_t0, cur_p_t0_t1)

        return {
            "trigger": target_usd + 1e-12 >= required_usd,
            "reason": (
                f"Above-range: single-sided token1. Expanded lower {steps}× to reach/bid breakeven."
            ),
            "action": "reallocate",
            "id": "breakeven_single_sided",
            "name": "Single-Sided Breakeven Reallocator",
            "lower": int(lower),
            "upper": int(upper),
            "range_side": "above",
            "details": {
                "ticks": {"lower": int(lower), "upper": int(upper)},
                "prices": {
                    "current": {"tick": int(tick), "eth_per_usdc": cur_p_t1_t0, "usdc_per_eth": cur_p_t0_t1},
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
                    "boundary": "lower",
                    "target_usd": float(target_usd),
                    "baseline_usd": float(baseline),
                    "buffer_pct": buffer_pct,
                    "profit_usd": float(target_usd - baseline),
                },
            },
        }

# Public registry
handlers = {
    "breakeven_single_sided": breakeven_single_sided,
}
