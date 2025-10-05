"""
CLI: prints one-time snapshot (state + USD value + PnL)
Usage:
    python -m bot.status
"""

import json
from bot.config import get_settings
from bot.chain import Chain
from bot.observer.vault_observer import VaultObserver
from bot.utils.log import log_info

def main():
    s = get_settings()
    ch = Chain(s.rpc_url, s.pool, s.nfpm, s.vault)
    observer = VaultObserver(ch)

    obs = observer.snapshot(twap_window=s.twap_window)
    snap = observer.usd_snapshot()

    log_info("=== VAULT STATUS JSON ===")
    print(json.dumps(obs, indent=2))

    rp = obs["range_prices"]
    side = obs["range_side"]

    log_info("=== RANGE & PRICES ===")
    print(
        "RANGE  (sorted by price)\n"
        f"  USDC/ETH: [{rp['usdc_per_eth_min']:.2f} , {rp['usdc_per_eth_max']:.2f}]\n"
        f"  ETH/USDC: [{rp['eth_per_usdc_min']:.10f} , {rp['eth_per_usdc_max']:.10f}]"
    )

    print(
        f"STATE  side={side} | inRange={not obs['out_of_range']} | "
        f"pct_outside_tick≈{obs['pct_outside_tick']:.3f}% | "
        f"pct_outside(ETH/USDC)≈{obs['pct_outside_eth_per_usdc']:.3f}% | "
        f"pct_outside(USDC/ETH)≈{obs['pct_outside_usdc_per_eth']:.3f}% | "
        f"twap_window={s.twap_window}s | vol={obs['volatility_pct']:.3f}%"
    )

    fees = obs.get("fees_human", {})
    sym0 = fees.get("sym0", "TOKEN0")
    sym1 = fees.get("sym1", "TOKEN1")
    print(
        f"FEES   uncollected: {fees.get('token0', 0.0):.6f} {sym0} + {fees.get('token1', 0.0):.6f} {sym1} "
        f"(≈ ${obs['uncollected_fees_usd']:.4f})"
    )

    log_info(
        f"USD Value=${snap.usd_value:,.2f} | ΔUSD={snap.delta_usd:+.2f} | "
        f"Baseline=${snap.baseline_usd:,.2f} | Spot USDC/ETH={snap.spot_price:.2f}"
    )


if __name__ == "__main__":
    main()
