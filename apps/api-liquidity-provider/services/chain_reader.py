"""
High-level read service that builds the "status" panel:
- live prices
- in/out-of-range and % outside
- uncollected fees (callStatic)
- USD valuation (price-only V(P))
This reuses the math/flow from your bot.observer.VaultObserver, simplified here.
"""

import math
from time import time
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Dict, Any, Tuple
from ..config import get_settings
from .state_repo import load_state, save_state
from ..adapters.uniswap_v3 import UniswapV3Adapter
from ..domain.models import (
    PricesBlock, PricesPanel, RewardsCollectedCum, UsdPanelModel,
    HoldingsSide, HoldingsMeta, HoldingsBlock,
    FeesUncollected, StatusCore, FeesCollectedCum
)
from web3 import Web3

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

def sqrtPriceX96_to_price_t1_per_t0(sqrtP: int, dec0: int, dec1: int) -> float:
    """
    Returns price as token1 per token0 (e.g., USDC per WETH if token0=WETH, token1=USDC).
    """
    ratio = Decimal(sqrtP) / Q96
    px = ratio * ratio
    scale = Decimal(10) ** (dec0 - dec1)
    return float(px * scale)

def prices_from_tick(tick: int, dec0: int, dec1: int) -> Dict[str, float]:
    p_t1_t0 = pow(1.0001, tick) * pow(10.0, dec0 - dec1)  # token1/token0
    p_t0_t1 = float("inf") if p_t1_t0 == 0 else (1.0 / p_t1_t0)
    return {"tick": tick, "p_t1_t0": p_t1_t0, "p_t0_t1": p_t0_t1}

def price_to_tick(p_t1_t0: float, dec0: int, dec1: int) -> int:
    if p_t1_t0 <= 0:
        raise ValueError("price must be > 0")
    ratio = p_t1_t0 / (10 ** (dec0 - dec1))
    # arredonda pro tick inteiro mais próximo
    tick_float = math.log(ratio, 1.0001)
    return int(round(tick_float))

def _is_usd_symbol(sym: str) -> bool:
    try:
        return sym.upper() in USD_SYMBOLS
    except Exception:
        return False

def _is_stable_addr(addr: str) -> bool:
    s = get_settings()
    try:
        return addr.lower() in {a.lower() for a in (s.STABLE_TOKEN_ADDRESSES or [])}
    except Exception:
        return False

def _value_usd(
    amt0_h: float, amt1_h: float,
    p_t1_t0: float, p_t0_t1: float,
    sym0: str, sym1: str,
    t0_addr: str, t1_addr: str
) -> float:
    """Converte (token0, token1) -> USD/USDC quando dá; senão, usa fallback (token1 como quote)."""
    token1_is_usd = _is_usd_symbol(sym1) or _is_stable_addr(t1_addr)
    token0_is_usd = _is_usd_symbol(sym0) or _is_stable_addr(t0_addr)

    if token1_is_usd:
        return amt0_h * p_t1_t0 + amt1_h
    if token0_is_usd:
        return amt1_h * p_t0_t1 + amt0_h
    # fallback: trata token1 como quote
    return amt0_h * p_t1_t0 + amt1_h

def compute_status(adapter, dex, alias: str) -> StatusCore:
    """
    Build a full "status" model from on-chain reads.

    Rules for USD valuation:
      - If token1 is USD-like: USD = token0 * (token1/token0) + token1
      - If token0 is USD-like: USD = token1 * (token0/token1) + token0
      - Else (no stables): fallback to quote token1 => USD ~= token0 * (t1/t0) + token1

    What we return:
      - Prices (spot/lower/upper) with both price views
      - Out-of-range and how far in % (by ticks)
      - Uncollected fees (preview via callStatic collect)
      - Inventory breakdown: vault_idle, in_position, totals  (NO subtraction of collected fees)
      - Cumulative *already collected* fees in a separate block (raw + human + USD)
      - USD panel with baseline/delta
    """
    st = load_state(dex, alias)

    # ---- pool & vault metadata
    meta = adapter.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = meta["sym0"], meta["sym1"]
    t0_addr, t1_addr = meta["token0"], meta["token1"]
    spacing = int(meta["spacing"])

    # ---- slot0 and vault state
    sqrtP, tick = adapter.slot0()
    vstate = adapter.vault_state()
    lower, upper, liq = int(vstate["lower"]), int(vstate["upper"]), int(vstate["liq"])

    twap_ok = bool(vstate.get("twapOk", True))
    last_rebalance = int(vstate.get("lastRebalance", 0))
    min_cd = int(vstate.get("min_cd", 0))

    # --- gauge & staking flags
    gauge_addr = vstate.get("gauge")
    has_gauge = bool(gauge_addr)
    is_staked = bool(vstate.get("staked", False))
    token_id = int(vstate.get("tokenId", 0) or 0)

    gauge_rewards_block = None

    if has_gauge and token_id != 0:
        try:
            if dex == "pancake":
                # MasterChefV3 (Pancake)
                mc = adapter.gauge_contract()
                if mc is not None:
                    pending_raw = int(mc.functions.pendingCake(int(token_id)).call())
                    reward_token_addr = mc.functions.CAKE().call()
                    erc = adapter.erc20(reward_token_addr)
                    r_sym = erc.functions.symbol().call()
                    r_dec = int(erc.functions.decimals().call())
                    pending_h = float(pending_raw) / (10 ** r_dec)
                    usd_est = pending_h if (r_sym.upper() in USD_SYMBOLS or _is_stable_addr(reward_token_addr)) else None

                    gauge_rewards_block = {
                        "reward_token": reward_token_addr,
                        "reward_symbol": r_sym,
                        "pending_raw": pending_raw,
                        "pending_amount": pending_h,
                        "pending_usd_est": (float(usd_est) if usd_est is not None else None),
                    }
            else:
                gauge = adapter.gauge_contract()

                adapter_onchain_addr = adapter.adapter_address()  # vamos adicionar isso (ver item 4)

                pending_raw = gauge.functions.earned(
                    Web3.to_checksum_address(adapter_onchain_addr),
                    token_id
                ).call()

                reward_token_addr = gauge.functions.rewardToken().call()

                erc20 = adapter.erc20_contract()

                reward_symbol = erc20.functions.symbol().call()
                reward_dec    = erc20.functions.decimals().call()

                pending_human = float(pending_raw) / (10 ** reward_dec)

                # tentativa de "usd_est": se reward é estável tipo USDC, trata 1:1
                usd_est = None
                if reward_symbol.upper() in USD_SYMBOLS or _is_stable_addr(reward_token_addr):
                    usd_est = pending_human
                # se for AERO/WETH etc, você pode deixar None agora e calcular depois

                gauge_rewards_block = {
                    "reward_token": reward_token_addr,
                    "reward_symbol": reward_symbol,
                    "pending_raw": int(pending_raw),
                    "pending_amount": pending_human,
                    "pending_usd_est": (float(usd_est) if usd_est is not None else None),
                }
        except Exception as e:
            gauge_rewards_block = {
                "error": f"gauge_read_failed: {str(e)}"
            }
    else:
        gauge_rewards_block = None
    
    
    gauge_reward_balances = None
    try:
        # Só faz sentido se conhecemos o reward token
        if gauge_rewards_block and "reward_token" in gauge_rewards_block:
            reward_token_addr = gauge_rewards_block["reward_token"]
            reward_symbol     = gauge_rewards_block.get("reward_symbol", "REWARD")

            # contrato ERC20 do reward
            erc_reward = adapter.erc20(reward_token_addr)
            reward_dec = int(erc_reward.functions.decimals().call())

            # saldos
            in_vault_raw   = int(erc_reward.functions.balanceOf(adapter.vault.address).call())
            in_vault_h   = float(in_vault_raw) / (10 ** reward_dec)

            gauge_reward_balances = {
                "token": reward_token_addr,
                "symbol": reward_symbol,
                "decimals": reward_dec,
                "in_vault_raw": in_vault_raw,
                "in_vault": in_vault_h,
            }
    except Exception as e:
        gauge_reward_balances = {"error": f"reward_balance_read_failed: {str(e)}"}
    
    
    # position location
    if token_id == 0:
        position_location = "none"
    else:
        position_location = "gauge" if is_staked else "pool"

    now = adapter.w3.eth.get_block("latest").timestamp
    cooldown_remaining_seconds = int(last_rebalance + min_cd - now)
    cooldown_active = cooldown_remaining_seconds > 0

    # ---- prices
    p_t1_t0 = sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)
    p_t0_t1 = (0.0 if p_t1_t0 == 0 else 1.0 / p_t1_t0)

    out_of_range = tick < lower or tick >= upper
    pct_outside_tick = _pct_from_dtick((lower - tick) if (out_of_range and tick < lower) else (tick - upper)) if out_of_range else 0.0

    # ---- uncollected fees (preview)
    fees0 = fees1 = 0
    if token_id != 0:
        fees0, fees1 = adapter.call_static_collect(token_id, adapter.vault.address)
    fees0_h = float(fees0) / (10 ** dec0)
    fees1_h = float(fees1) / (10 ** dec1)
    fees_usd = _value_usd(fees0_h, fees1_h, p_t1_t0, p_t0_t1, sym0, sym1, t0_addr, t1_addr)

    # ---- balances
    erc0 = adapter.erc20(t0_addr)
    erc1 = adapter.erc20(t1_addr)
    bal0_idle_raw = int(erc0.functions.balanceOf(adapter.vault.address).call())
    bal1_idle_raw = int(erc1.functions.balanceOf(adapter.vault.address).call())

    amt0_pos_raw = amt1_pos_raw = 0
    if liq > 0:
        a0, a1 = adapter.amounts_in_position_now(lower, upper, liq)
        amt0_pos_raw, amt1_pos_raw = int(a0), int(a1)

    adj0_idle = bal0_idle_raw / (10 ** dec0)
    adj1_idle = bal1_idle_raw / (10 ** dec1)
    amt0_pos = amt0_pos_raw / (10 ** dec0)
    amt1_pos = amt1_pos_raw / (10 ** dec1)

    tot0 = adj0_idle + amt0_pos
    tot1 = adj1_idle + amt1_pos

    idle_usd = _value_usd(adj0_idle, adj1_idle, p_t1_t0, p_t0_t1, sym0, sym1, t0_addr, t1_addr)
    pos_usd  = _value_usd(amt0_pos,  amt1_pos,  p_t1_t0, p_t0_t1, sym0, sym1, t0_addr, t1_addr)
    total_usd = _value_usd(tot0, tot1, p_t1_t0, p_t0_t1, sym0, sym1, t0_addr, t1_addr)

    # ---- cumulative fees already collected
    cum = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0}) or {}
    cum0_raw = int(cum.get("token0_raw", 0) or 0)
    cum1_raw = int(cum.get("token1_raw", 0) or 0)
    cum0 = cum0_raw / (10 ** dec0)
    cum1 = cum1_raw / (10 ** dec1)
    cum_usd = _value_usd(cum0, cum1, p_t1_t0, p_t0_t1, sym0, sym1, t0_addr, t1_addr)

    cum = st.get("rewards_usdc_cum", {}) or {}
    rewards_usdc_raw = int(cum.get("usdc_raw", 0))
    rewards_usdc     = float(cum.get("usdc_human", 0.0))
    
    baseline = st.get("vault_initial_usd")
    if baseline is None:
        baseline = total_usd
        st["vault_initial_usd"] = baseline
        save_state(dex, alias, st)

    prices_panel = PricesPanel(
        current=PricesBlock(**prices_from_tick(tick,  dec0, dec1)),
        lower=  PricesBlock(**prices_from_tick(lower, dec0, dec1)),
        upper=  PricesBlock(**prices_from_tick(upper, dec0, dec1)),
    )

    usd_panel = UsdPanelModel(
        usd_value=float(total_usd),
        delta_usd=float(total_usd - float(baseline)),
        baseline_usd=float(baseline),
    )

    holdings = HoldingsBlock(
        vault_idle=HoldingsSide(token0=adj0_idle, token1=adj1_idle, usd=idle_usd),
        in_position=HoldingsSide(token0=amt0_pos, token1=amt1_pos, usd=pos_usd),
        totals=HoldingsSide(token0=tot0, token1=tot1, usd=total_usd),
        decimals=HoldingsMeta(token0=dec0, token1=dec1),
        symbols={"token0": sym0, "token1": sym1},
        addresses={"token0": t0_addr, "token1": t1_addr},
    )

    fees_uncollected = FeesUncollected(
        token0=fees0_h, token1=fees1_h, usd=float(fees_usd), sym0=sym0, sym1=sym1
    )

    fees_collected_cum = FeesCollectedCum(
        token0_raw=cum0_raw,
        token1_raw=cum1_raw,
        token0=cum0,
        token1=cum1,
        usd=float(cum_usd),
    )

    range_side = "inside" if not out_of_range else ("below" if tick < lower else "above")

    rewards_block = RewardsCollectedCum(
        usdc_raw=rewards_usdc_raw,
        usdc=rewards_usdc,
    )
        
    return StatusCore(
        tick=tick,
        lower=lower,
        upper=upper,
        spacing=spacing,
        twap_ok=twap_ok,
        last_rebalance=last_rebalance,
        cooldown_remaining_seconds=cooldown_remaining_seconds,
        cooldown_active=cooldown_active,
        prices=prices_panel,
        gauge_rewards=gauge_rewards_block,
        gauge_reward_balances=gauge_reward_balances,
        rewards_collected_cum=rewards_block,
        fees_uncollected=fees_uncollected,
        fees_collected_cum=fees_collected_cum,
        out_of_range=out_of_range,
        pct_outside_tick=pct_outside_tick,
        usd_panel=usd_panel,
        range_side=range_side,
        sym0=sym0,
        sym1=sym1,
        holdings=holdings,
        has_gauge=has_gauge,
        gauge=gauge_addr,
        staked=is_staked,
        position_location=position_location
    )