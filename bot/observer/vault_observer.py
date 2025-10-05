"""
Vault observer — collects live state from the vault/pool and derives metrics
used by strategy evaluation (pct_outside, out_since, spot_price, entry_price, volatility, etc.).
"""

import time
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Dict, Any

from bot.chain import Chain
from bot.observer.state_manager import StateManager

getcontext().prec = 80
Q96 = Decimal(2) ** 96

@dataclass
class VaultObservation:
    tick: int
    lower: int
    upper: int
    spacing: int

    spot_price: float                 # token1 per token0 (e.g., ETH per USDC or vice-versa depending on order)
    pct_outside: float
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

    def __init__(self, chain: Chain, state_file: str = "state.json"):
        self.chain = chain
        self.state = StateManager(state_file)
        self._price_series = []  # rolling list for simple vol calc

        # cache pool meta
        self._meta = self.chain.pool_meta()  # {token0, token1, fee, spacing, sym0, sym1, dec0, dec1}

    # ---------------------
    # helpers
    # ---------------------

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
        import math
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
    # public API
    # ---------------------

    def snapshot(self, twap_window: int = 60) -> Dict[str, Any]:
        """
        Builds a full observation from on-chain data + persisted state.
        """
        sqrtP, tick = self.chain.slot0()
        twap_tick = self.chain.observe_twap_tick(twap_window)
        lower, upper, liq = self.chain.vault_state()["lower"], self.chain.vault_state()["upper"], self.chain.vault_state()["liq"]
        spacing = self._meta["spacing"]

        # spot price in token1/token0
        spot_price_dec = self._sqrtPriceX96_to_price(sqrtP)
        spot_price = float(spot_price_dec)

        # rolling volatility (quick & dirty)
        self._price_series.append(spot_price)
        self._price_series = self._price_series[-100:]
        vol_pct = self._simple_vol(window=20)

        # in/out of range and pct outside
        out_of_range = tick < lower or tick >= upper
        if out_of_range:
            if tick < lower:
                # distance below lower in percentage (approx with ticks difference / tick magnitude)
                pct_outside = abs((lower - tick) / max(1, abs(tick))) * 100.0
            else:
                pct_outside = abs((tick - upper) / max(1, abs(tick))) * 100.0
        else:
            pct_outside = 0.0

        # manage out_since persisted
        out_since = self.state.get("out_since", 0)
        if out_of_range and not out_since:
            out_since = time.time()
            self.state.set("out_since", out_since)
        elif not out_of_range and out_since:
            out_since = 0
            self.state.set("out_since", 0)

        # ensure entry_price exists after the *first open*
        entry_price = self.state.get("entry_price", None)
        token_id = self.chain.vault_state()["tokenId"]
        if token_id != 0 and entry_price is None:
            # use current spot as baseline entry for MVP
            self.state.set("entry_price", spot_price)
            entry_price = spot_price

        # fees (callStatic)
        fees0, fees1 = (0, 0)
        if token_id != 0:
            fees0, fees1 = self.chain.call_static_collect(token_id, self.chain.vault.address)

        # quick USD-estimate: assume token0 is USDC (6 decimals) and token1 is ETH priced in token0 units.
        # If your pair differs, adapt here or fetch a price oracle.
        fees_usd = float(fees0 / (10 ** self._meta["dec0"])) + float(fees1 / (10 ** self._meta["dec1"])) * spot_price

        obs = VaultObservation(
            tick=tick,
            lower=lower,
            upper=upper,
            spacing=spacing,
            spot_price=spot_price,
            pct_outside=pct_outside,
            out_of_range=out_of_range,
            out_since=out_since,
            volatility_pct=vol_pct,
            entry_price=entry_price,
            uncollected_fees_token0=fees0,
            uncollected_fees_token1=fees1,
            uncollected_fees_usd=fees_usd,
        )
        return asdict(obs)

    def record_entry_price_on_rebalance(self, price: float) -> None:
        """
        Call this from your rebalance path (or just before suggesting a new range)
        to pin the current 'entry price' for loss-aware checks.
        """
        self.state.set("entry_price", float(price))

    def pnl_vs_entry_usd(self, amount0_raw: int, amount1_raw: int) -> float:
        """
        Rough PnL vs stored entry baseline (in USD-ish), assuming token0=USDC and token1=ETH.
        Uses current spot price and compares to baseline_usd (stores if missing).
        """
        dec0, dec1 = self._meta["dec0"], self._meta["dec1"]
        spot = self._price_series[-1] if self._price_series else 0.0
        current_usd = (amount0_raw / (10 ** dec0)) + (amount1_raw / (10 ** dec1)) * spot

        base_usd = self.state.get("baseline_usd", None)
        if base_usd is None:
            self.state.set("baseline_usd", current_usd)
            base_usd = current_usd

        return float(current_usd - base_usd)
