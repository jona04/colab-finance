"""
Vault observer — collects live state from the vault/pool and derives metrics
used by strategy evaluation (pct_outside, out_since, spot_price, entry_price, volatility, etc.).
"""

import time
import json
import math
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Dict, Any

from bot.chain import Chain

getcontext().prec = 80
Q96 = Decimal(2) ** 96

@dataclass
class VaultSnapshot:
    usd_value: float                  # V(P) (preço-apenas, exclui fees coletadas)
    delta_usd: float                  # V(P) - vault_initial_usd
    baseline_usd: float               # = vault_initial_usd
    token0_idle: float
    token1_idle: float
    token0_in_pos: float
    token1_in_pos: float
    spot_price: float                 # USDC/ETH (sempre em dólares por ETH)
    
@dataclass
class VaultObservation:
    tick: int
    lower: int
    upper: int
    spacing: int

    spot_price: float                 # token1 per token0 (e.g., ETH per USDC or vice-versa depending on order)
    pct_outside_tick: float
    out_of_range: bool
    out_since: float

    volatility_pct: float             # simple rolling volatility (optional placeholder)
    entry_price: float | None

    # extras for strategies/filters
    uncollected_fees_token0: int
    uncollected_fees_token1: int
    uncollected_fees_usd: float


class VaultObserver:
    """
    High-level observer that uses Chain to read on-chain state and derive strategy metrics.
    """

    def __init__(self, chain: Chain, state_path: str = "bot/state.json"):
        self.chain = chain
        self.state_path = state_path
        self.state = self._load_state()
        self._price_series = []  # rolling list for simple vol calc

        # cache pool meta
        self._meta = self.chain.pool_meta()  # {token0, token1, fee, spacing, sym0, sym1, dec0, dec1}

        # ensure Fase 4.A keys
        self._ensure_phase4_keys()
        
    # ---------------------
    # helpers
    # ---------------------

    def _ensure_phase4_keys(self):
        """
        Initialize Fase 4.A state keys if missing.
        - vault_initial_usd: set lazily on first usd_snapshot() if absent (using price-apenas V(P))
        - fees_collected_cum: dict with raw token units accumulated off-chain after successful exec
        - fees_cum_usd: running USD total of collected fees (for relatórios — não entra no V(P))
        """
        st = self.state
        if "fees_collected_cum" not in st:
            st["fees_collected_cum"] = {
                "token0_raw": 0,   # integers in raw token units (dec0/dec1)
                "token1_raw": 0
            }
        if "fees_cum_usd" not in st:
            st["fees_cum_usd"] = 0.0
        # vault_initial_usd: será definido no primeiro usd_snapshot() (se o user ainda não rodou /baseline set)
        self._save_state()
        
    @staticmethod
    def _pct_from_dtick(d_ticks: int) -> float:
        """
        Exact % distance implied by tick difference using Uniswap base: (1.0001^|d| - 1)*100.
        This is directionless magnitude in ETH/USDC terms.
        """
        factor = pow(1.0001, abs(d_ticks))
        return (factor - 1.0) * 100.0

    def _sqrtPriceX96_to_price(self, sqrtP: int) -> Decimal:
        """
        Converts sqrtPriceX96 to price token1/token0, scaled by token decimals.
        price = (sqrtP / Q96)^2 * 10^(dec0 - dec1)
        """
        dec0 = self._meta["dec0"]
        dec1 = self._meta["dec1"]
        ratio = Decimal(sqrtP) / Q96
        px = ratio * ratio  # token1/token0 in raw decimals
        scale = Decimal(10) ** (dec0 - dec1)
        return px * scale

    def _simple_vol(self, window: int = 20) -> float:
        """
        Naive rolling volatility (% std of log returns) with small window for MVP.
        """
        s = self._price_series[-window:]
        if len(s) < 3:
            return 0.0
        lr = [math.log(s[i+1]/s[i]) for i in range(len(s)-1) if s[i] > 0]
        if len(lr) < 2:
            return 0.0
        mean = sum(lr) / len(lr)
        var = sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)
        return float((var ** 0.5) * 100.0)

    # ---------------------
    # price/tick helpers
    # ---------------------

    @staticmethod
    def _price_token1_per_token0_from_tick(tick: int) -> float:
        """
        Uniswap v3 canonical mapping:
        price(token1/token0) = 1.0001 ^ tick
        """
        return float(pow(1.0001, tick))

    @classmethod
    def _price_token0_per_token1_from_tick(cls, tick: int) -> float:
        """
        Inverse price for convenience:
        price(token0/token1) = 1 / price(token1/token0)
        """
        p = cls._price_token1_per_token0_from_tick(tick)
        return float("inf") if p == 0.0 else (1.0 / p)

    def _price_token1_per_token0_from_tick_scaled(self, tick: int) -> float:
        """
        Uniswap v3 mapping with decimals:
        price(token1/token0) = 1.0001^tick * 10^(dec0 - dec1)
        """
        dec0, dec1 = self._meta["dec0"], self._meta["dec1"]
        base = pow(1.0001, tick)
        scale = pow(10.0, dec0 - dec1)  # e.g., 10^(6-18) = 1e-12 for USDC/WETH
        return base * scale

    def _price_token0_per_token1_from_tick_scaled(self, tick: int) -> float:
        """
        Inverse price with decimals adjustment:
        price(token0/token1) = 1 / price(token1/token0)
        """
        p = self._price_token1_per_token0_from_tick_scaled(tick)
        return float("inf") if p == 0.0 else (1.0 / p)

    def prices_from_tick(self, tick: int) -> dict:
        """
        Returns both price views for a given tick, already scaled by decimals:
        - p_t1_t0: token1/token0 (ETH/USDC)
        - p_t0_t1: token0/token1 (USDC/ETH)
        """
        p_t1_t0 = self._price_token1_per_token0_from_tick_scaled(tick)
        p_t0_t1 = float("inf") if p_t1_t0 == 0.0 else (1.0 / p_t1_t0)
        return {"tick": tick, "p_t1_t0": p_t1_t0, "p_t0_t1": p_t0_t1}

    # -------------------------------
    # Persistence helpers
    # -------------------------------
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)
            
    # ---------------------
    # public API
    # ---------------------

    def snapshot(self, twap_window: int = 60) -> Dict[str, Any]:
        """
        Builds a full observation from on-chain data + persisted state.
        """
        sqrtP, tick = self.chain.slot0()
        _ = self.chain.observe_twap_tick(twap_window)  # value kept for guards/UI if needed
        vs = self.chain.vault_state()
        lower, upper, liq = vs["lower"], vs["upper"], vs["liq"]
        spacing = self._meta["spacing"]

        # spot price in token1/token0
        spot_price_dec = self._sqrtPriceX96_to_price(sqrtP)
        spot_price = float(spot_price_dec)

        # rolling volatility (quick & dirty)
        self._price_series.append(spot_price)
        self._price_series = self._price_series[-100:]
        vol_pct = self._simple_vol(window=20)

        # in/out of range and % outside (tick-based and price-based)
        out_of_range = tick < lower or tick >= upper
        
        # tick-based magnitude
        if out_of_range:
            d_ticks = (lower - tick) if tick < lower else (tick - upper)
            pct_outside_tick = self._pct_from_dtick(d_ticks)
        else:
            d_ticks = 0
            pct_outside_tick = 0.0

        # manage out_since persisted (0.0 means “not out of range” / unset)
        out_since = float(self.state.get("out_since", 0.0))

        state_changed = False
        
        if out_of_range:
            # set only on transition into "out"
            if out_since == 0.0:
                out_since = time.time()
                self.state["out_since"] = out_since
                state_changed = True
        else:
            # clear only on transition back "in"
            if out_since != 0.0:
                out_since = 0.0
                self.state["out_since"] = 0.0
                state_changed = True

        # ensure entry_price exists after the *first open*
        entry_price = self.state.get("entry_price", None)
        token_id = self.chain.vault_state()["tokenId"]
        if token_id != 0 and entry_price is None:
            # use current spot as baseline entry for MVP
            self.state["entry_price"] = spot_price
            entry_price = spot_price
            state_changed = True

        if state_changed:
            self._save_state()
        
        # fees (callStatic)
        fees0, fees1 = (0, 0)
        if token_id != 0:
            fees0, fees1 = self.chain.call_static_collect(token_id, self.chain.vault.address)

        # quick USD-estimate: assume token0 is USDC (6 decimals) and token1 is ETH priced in token0 units.
        # spot_price = token1/token0 (ETH por USDC) -> USD por ETH = 1 / spot_price
        price_usd_per_token1 = float("inf") if spot_price == 0 else (1.0 / spot_price)
        
        fees_usd = (
            float(fees0) / (10 ** self._meta["dec0"])
            + (float(fees1) / (10 ** self._meta["dec1"])) * price_usd_per_token1
        )
        fees0_human = float(fees0) / (10 ** self._meta["dec0"])
        fees1_human = float(fees1) / (10 ** self._meta["dec1"])
        
        obs = VaultObservation(
            tick=tick,
            lower=lower,
            upper=upper,
            spacing=spacing,
            spot_price=spot_price,
            pct_outside_tick=pct_outside_tick,
            out_of_range=out_of_range,
            out_since=out_since,
            volatility_pct=vol_pct,
            entry_price=entry_price,
            uncollected_fees_token0=fees0,
            uncollected_fees_token1=fees1,
            uncollected_fees_usd=fees_usd,
        )
        
        prices_block = {
            "current": self.prices_from_tick(tick),
            "lower": self.prices_from_tick(lower),
            "upper": self.prices_from_tick(upper),
        }
        
        # Which side of the range?
        if out_of_range:
            if tick < lower:
                range_side = "below"
                # compare current to LOWER boundary
                # ETH/USDC grows when tick grows
                pct_out_eth_usdc = (prices_block["lower"]["p_t1_t0"] / prices_block["current"]["p_t1_t0"] - 1.0) * 100.0
                # USDC/ETH moves inversely -> symmetrical magnitude but compute explicitly
                pct_out_usdc_eth = (prices_block["current"]["p_t0_t1"] / prices_block["lower"]["p_t0_t1"] - 1.0) * 100.0
            else:
                range_side = "above"
                # compare current to UPPER boundary
                pct_out_eth_usdc = (prices_block["current"]["p_t1_t0"] / prices_block["upper"]["p_t1_t0"] - 1.0) * 100.0
                pct_out_usdc_eth = (prices_block["upper"]["p_t0_t1"] / prices_block["current"]["p_t0_t1"] - 1.0) * 100.0
        else:
            range_side = "inside"
            pct_out_eth_usdc = 0.0
            pct_out_usdc_eth = 0.0
        
        # Sorted range views by PRICE (not by tick), to avoid confusion on the UI
        p0_low = prices_block["lower"]["p_t0_t1"]   # USDC/ETH at tickLower
        p0_up  = prices_block["upper"]["p_t0_t1"]   # USDC/ETH at tickUpper
        p1_low = prices_block["lower"]["p_t1_t0"]   # ETH/USDC at tickLower
        p1_up  = prices_block["upper"]["p_t1_t0"]   # ETH/USDC at tickUpper

        range_prices = {
            "usdc_per_eth_min": min(p0_low, p0_up),
            "usdc_per_eth_max": max(p0_low, p0_up),
            "eth_per_usdc_min": min(p1_low, p1_up),
            "eth_per_usdc_max": max(p1_low, p1_up),
        }

        result = asdict(obs)
        result["prices"] = prices_block
        result["fees_human"] = {
            "token0": fees0_human,
            "token1": fees1_human,
            "sym0": self._meta["sym0"],
            "sym1": self._meta["sym1"],
        }
        result["range_side"] = range_side
        result["pct_outside_eth_per_usdc"] = pct_out_eth_usdc
        result["pct_outside_usdc_per_eth"] = pct_out_usdc_eth
        result["range_prices"] = range_prices
        return result

    def usd_snapshot(self) -> VaultSnapshot:
        """
        Computes real-time USD valuation of vault:
        - Liquidity currently in range (token0/token1)
        - Idle balances held in vault
        - Tracks baseline_usd and delta_usd (PnL)
        """
        vstate = self.chain.vault_state()
        dec0, dec1 = self._meta["dec0"], self._meta["dec1"]

        # --- Get position amounts ---
        amount0_pos, amount1_pos = self.chain.amounts_in_position_now(
            vstate["lower"], vstate["upper"], vstate["liq"]
        )

        # --- Idle balances ---
        erc0 = self.chain.erc20(self._meta["token0"])
        erc1 = self.chain.erc20(self._meta["token1"])
        bal0_idle = erc0.functions.balanceOf(self.chain.vault.address).call()
        bal1_idle = erc1.functions.balanceOf(self.chain.vault.address).call()

        # spot as token1/token0 (ETH per USDC)
        sqrtP, _ = self.chain.slot0()
        spot_t1_per_t0 = float(self._sqrtPriceX96_to_price(sqrtP))
        # want USD per ETH (token0/token1) = inverse
        price_usd_per_token1 = float("inf") if spot_t1_per_t0 == 0.0 else (1.0 / spot_t1_per_t0)
        
        # --- Normalize amounts ---
        token0_idle = bal0_idle / (10 ** dec0)
        token1_idle = bal1_idle / (10 ** dec1)
        token0_in_pos = amount0_pos / (10 ** dec0)
        token1_in_pos = amount1_pos / (10 ** dec1)

        # --- Subtract cumulated collected fees (RAW) from "estoque vivo" ---
        fees_col = self.state.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})
        adj_token0 = (bal0_idle + amount0_pos) - int(fees_col.get("token0_raw", 0) or 0)
        adj_token1 = (bal1_idle + amount1_pos) - int(fees_col.get("token1_raw", 0) or 0)
        
        # Guard: não deixar negativo (casos raros de drift/rounding)
        if adj_token0 < 0: adj_token0 = 0
        if adj_token1 < 0: adj_token1 = 0
        
        adj_token0_h = adj_token0 / (10 ** dec0)
        adj_token1_h = adj_token1 / (10 ** dec1)
        
         # --- USD estimation (preço-apenas) ---
        # sempre: somar o que é USDC + (ETH * USDC/ETH)
        total_usd = adj_token0_h + adj_token1_h * price_usd_per_token1
        
        # --- Baseline (vault_initial_usd) ---
        vault_initial = self.state.get("vault_initial_usd", None)
        if vault_initial is None:
            # define baseline uma única vez (se o user não fizer /baseline set manual)
            vault_initial = total_usd
            self.state["vault_initial_usd"] = vault_initial
            self._save_state()

        delta_usd = total_usd - float(vault_initial)
        
        return VaultSnapshot(
            usd_value=total_usd,
            delta_usd=delta_usd,
            baseline_usd=float(vault_initial),
            token0_idle=token0_idle,
            token1_idle=token1_idle,
            token0_in_pos=token0_in_pos,
            token1_in_pos=token1_in_pos,
            spot_price=price_usd_per_token1,
        )

    def record_entry_price_on_rebalance(self, price: float) -> None:
        """
        Call this from your rebalance path (or just before suggesting a new range)
        to pin the current 'entry price' for loss-aware checks.
        """
        self.state["entry_price"] = float(price)

    def pnl_vs_entry_usd(self, amount0_raw: int, amount1_raw: int) -> float:
        """
        Rough PnL vs stored entry baseline (in USD-ish), assuming token0=USDC and token1=ETH.
        Uses current spot price and compares to baseline_usd (stores if missing).
        """
        dec0, dec1 = self._meta["dec0"], self._meta["dec1"]
        spot = self._price_series[-1] if self._price_series else 0.0
        current_usd = (amount0_raw / (10 ** dec0)) + (amount1_raw / (10 ** dec1)) * spot

        base_usd = self.state.get("vault_initial_usd", None)
        if base_usd is None:
            self.state["vault_initial_usd"] = current_usd
            self._save_state()
            base_usd = current_usd

        return float(current_usd - base_usd)
