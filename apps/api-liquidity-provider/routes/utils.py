

from typing import Optional

from fastapi import HTTPException
from web3 import Web3
from ..domain.models import StatusCore
from ..services.chain_reader import USD_SYMBOLS, compute_status, sqrtPriceX96_to_price_t1_per_t0
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
    
def estimate_eth_usd_from_pool(ad) -> float | None:
    """
    Best-effort ETH/USD using current vault pool.
    Returns None if we can't infer.
    """
    meta2 = ad.pool_meta()
    dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
    sym0b = str(meta2["sym0"]).upper()
    sym1b = str(meta2["sym1"]).upper()
    sqrtPb, _ = ad.slot0()
    p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)

    # se token0=ETH e token1=USDC, p_t1_t0b = USDC per ETH
    if sym0b in {"WETH","ETH"} and sym1b in USD_SYMBOLS:
        return float(p_t1_t0b)

    # se token0=USDC e token1=ETH, então invertido
    if sym1b in {"WETH","ETH"} and sym0b in USD_SYMBOLS:
        return float(0 if p_t1_t0b == 0 else 1.0/p_t1_t0b)

    return None

def resolve_pool_from_vault(v: dict, pool_override: Optional[str]) -> str:
    """
    Retorna o endereço do pool Uniswap (checksum).
    - Se pool_override é "0x..." => usa direto.
    - Se pool_override é uma chave (ex: "AERO_USDC") => usa v["swap_pools"][key] com dex == "uniswap".
    - Se nada vier => tenta chave "AERO_USDC" em v["swap_pools"].
    """
    if pool_override:
        if pool_override.lower().startswith("0x"):
            return Web3.to_checksum_address(pool_override)
        sp = (v.get("swap_pools") or {}).get(pool_override)
        if not sp:
            raise HTTPException(400, f"swap_pools key not found: {pool_override}")
        return Web3.to_checksum_address(sp["pool"])

    # default: tente "AERO_USDC"
    sp = (v.get("swap_pools") or {}).get("AERO_USDC")
    if not sp or str(sp.get("dex")).lower() != "uniswap":
        raise HTTPException(400, "Missing swap_pools.AERO_USDC with dex='uniswap' or pass pool_override")
    return Web3.to_checksum_address(sp["pool"])