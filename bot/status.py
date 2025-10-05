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

    # símbolos para impressão bonita
    fees = obs.get("fees_human", {})
    sym0 = fees.get("sym0", "TOKEN0")   # USDC
    sym1 = fees.get("sym1", "TOKEN1")   # WETH

    # preços formatados
    pr_cur = obs["prices"]["current"]
    pr_low = obs["prices"]["lower"]
    pr_up  = obs["prices"]["upper"]

    # ---- valores em USD por bloco ----
    usdc_per_eth = snap.spot_price  # USDC/ETH
    idle_usd = snap.token0_idle + snap.token1_idle * usdc_per_eth
    pool_usd = snap.token0_in_pos + snap.token1_in_pos * usdc_per_eth
    total_usd = idle_usd + pool_usd  # deve bater com snap.usd_value

    # composição por USD
    usd_in_usdc = (snap.token0_idle + snap.token0_in_pos)
    usd_in_eth  = (snap.token1_idle + snap.token1_in_pos) * usdc_per_eth
    tot_usd_safe = total_usd if total_usd > 0 else 1.0
    pct_usdc = 100.0 * (usd_in_usdc / tot_usd_safe)
    pct_eth  = 100.0 * (usd_in_eth  / tot_usd_safe)

    # ---- impressão estruturada ----
    log_info("=== VAULT STATUS JSON ===")
    print(json.dumps(obs, indent=2))

    log_info("=== RANGE & PRICES ===")
    print(
        "RANGE  (sorted by price)\n"
        f"  USDC/ETH: [{obs['range_prices']['usdc_per_eth_min']:.2f} , {obs['range_prices']['usdc_per_eth_max']:.2f}]\n"
        f"  ETH/USDC: [{obs['range_prices']['eth_per_usdc_min']:.10f} , {obs['range_prices']['eth_per_usdc_max']:.10f}]"
    )
    print(
        f"STATE  side={'inside' if not obs['out_of_range'] else 'above' if obs['range_side']=='above' else 'below'} | "
        f"inRange={not obs['out_of_range']} | "
        f"pct_outside_tick≈{obs['pct_outside_tick']:.3f}% | "
        f"pct_outside(ETH/USDC)≈{obs['pct_outside_eth_per_usdc']:.3f}% | "
        f"pct_outside(USDC/ETH)≈{obs['pct_outside_usdc_per_eth']:.3f}% | "
        f"twap_window={s.twap_window}s | vol={obs['volatility_pct']:.3f}%"
    )

    print(
        f"FEES   uncollected: {obs['fees_human']['token0']:.6f} {sym0} + "
        f"{obs['fees_human']['token1']:.6f} {sym1} (≈ ${obs['uncollected_fees_usd']:.4f})"
    )

    # ---- breakdown por token/onde está ----
    print("\nASSETS (human units)")
    print(
        f"  Idle (vault): {snap.token0_idle:.6f} {sym0} | {snap.token1_idle:.6f} {sym1} "
        f"(≈ ${idle_usd:,.2f})"
    )
    print(
        f"  In position:  {snap.token0_in_pos:.6f} {sym0} | {snap.token1_in_pos:.6f} {sym1} "
        f"(≈ ${pool_usd:,.2f})"
    )
    t0_total = snap.token0_idle + snap.token0_in_pos
    t1_total = snap.token1_idle + snap.token1_in_pos
    print(
        f"  Totals:       {t0_total:.6f} {sym0} | {t1_total:.6f} {sym1} "
        f"(≈ ${total_usd:,.2f})"
    )
    print(
        f"  Composition:  {pct_usdc:5.2f}% {sym0} | {pct_eth:5.2f}% {sym1} "
        f"(USDC/ETH spot={usdc_per_eth:,.2f})"
    )

    # painel USD final
    log_info(
        f"USD Value=${snap.usd_value:,.2f} | ΔUSD={snap.delta_usd:+.2f} | "
        f"Baseline=${snap.baseline_usd:,.2f} | Spot USDC/ETH={usdc_per_eth:,.2f}"
    )

if __name__ == "__main__":
    main()
