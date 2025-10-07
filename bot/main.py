"""
Main bot loop — observes the vault, evaluates JSON-driven strategies, and prints signals.
"""
import os
import hashlib
import time
from pathlib import Path
from bot.utils.formatters import fmt_alert_range
from bot.config import get_settings
from bot.chain import Chain
from bot.observer.vault_observer import VaultObserver
from bot.observer.state_manager import StateManager
from bot.telegram_client import TelegramClient
from bot.strategy.registry import handlers
from bot.utils.log import log_info, log_warn


def load_strategies(path: str | None = None):
    """
    Load strategies JSON relative to this file (not the CWD),
    unless an absolute/explicit path is provided.
    """
    if path is None:
        # bot/main.py -> bot/
        base = Path(__file__).resolve().parent
        path = base / "strategy" / "examples" / "strategies.json"
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Strategies file not found: {path}")

    import json
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_all(strategies, obs):
    results = []
    for strat in strategies:
        if not strat.get("active", True):
            continue
        fn = handlers.get(strat["id"])
        if not fn:
            continue
        res = fn(strat["params"], obs)
        if res and res.get("trigger"):
            results.append({"id": strat["id"], **res})
    return results


def main():
    s = get_settings()
    ch = Chain(s.rpc_url, s.pool, s.nfpm, s.vault)
    observer = VaultObserver(ch)
    strategies = load_strategies(os.environ.get("STRATEGIES_FILE"))

    # Telegram + state manager for dedupe/cooldown
    tg = TelegramClient()
    sm = StateManager("bot/state.json")

    # Use centralized thresholds from Settings
    cooldown = int(s.alerts_cooldown_sec)
    dedupwin = int(s.alerts_dedup_window_sec)
    
    # internal keys for alert timers/counters
    KEY_RPC_FAIL_COUNT = "rpc_fail_count"
    KEY_TWAP_FALSE_SINCE = "twap_false_since"
    KEY_RANGE_OUT_SINCE = "range_out_since"  # we keep a local override to observer.state["out_since"]


    log_info("Observer up. Polling on-chain state...")

    while True:
        try:
            # --- Read on-chain; if fails, handle RPC failure alert path ---
            try:
                obs = observer.snapshot(twap_window=s.twap_window)
                snap = observer.usd_snapshot()
                vstate = ch.vault_state()
                # reset RPC failure streak when success
                sm.set(KEY_RPC_FAIL_COUNT, 0)
            except Exception as e:
                # RPC failure handling
                rpc_fail = int(sm.get(KEY_RPC_FAIL_COUNT, 0)) + 1
                sm.set(KEY_RPC_FAIL_COUNT, rpc_fail)
                log_warn(f"loop read error (RPC?) streak={rpc_fail}: {e}")

                if rpc_fail >= s.alert_rpc_fail_max:
                    txt = f"RPC failing for {rpc_fail} consecutive attempts."
                    payload_hash = hashlib.sha256(txt.encode()).hexdigest()
                    if sm.should_send_alert("rpc_failure", payload_hash, cooldown, dedupwin):
                        tg.send_text(f"[rpc_failure] {txt}")
                        sm.mark_alert_sent("rpc_failure", payload_hash)
                # skip the rest of the loop on this iteration
                time.sleep(s.check_interval)
                continue

            # Logging blocks
            pr_cur = obs["prices"]["current"]
            pr_low = obs["prices"]["lower"]
            pr_upp = obs["prices"]["upper"]

            log_info(
                "PRICES  (token1/token0 = ETH/USDC | token0/token1 = USDC/ETH)\n"
                f"  current: tick={pr_cur['tick']:,} | ETH/USDC={pr_cur['p_t1_t0']:.10f} | USDC/ETH={pr_cur['p_t0_t1']:.4f}\n"
                f"  lower:   tick={pr_low['tick']:,} | ETH/USDC={pr_low['p_t1_t0']:.10f} | USDC/ETH={pr_low['p_t0_t1']:.4f}\n"
                f"  upper:   tick={pr_upp['tick']:,} | ETH/USDC={pr_upp['p_t1_t0']:.10f} | USDC/ETH={pr_upp['p_t0_t1']:.4f}"
            )

            log_info(
                f"STATE    inRange={not obs['out_of_range']} | "
                f"pct_outside≈{obs['pct_outside_tick']:.3f}% | twap_window={s.twap_window}s | "
                f"fees≈${obs['uncollected_fees_usd']:.4f} | vol={obs['volatility_pct']:.3f}%"
            )

            log_info(
                f"USD      total≈${snap.usd_value:,.2f} | ΔUSD={snap.delta_usd:+.2f} | "
                f"baseline=${snap.baseline_usd:,.2f} | USDC/ETH={snap.spot_price:.2f}"
            )
            
            # ---------- ALERTS (dedupe/cooldown via StateManager) ----------
            now = time.time()

            # A) OUT OF RANGE for X minutes
            if obs["out_of_range"]:
                # prefer observer.state["out_since"] but keep our own sentinel if absent
                out_since = float(observer.state.get("out_since", 0) or sm.get(KEY_RANGE_OUT_SINCE, 0) or 0)
                if out_since == 0:
                    out_since = now
                    sm.set(KEY_RANGE_OUT_SINCE, out_since)
                minutes = (now - out_since) / 60.0
                if minutes >= s.alert_out_of_range_minutes:
                    md = fmt_alert_range(obs)
                    payload_hash = hashlib.sha256(md.encode()).hexdigest()
                    if sm.should_send_alert("range_out", payload_hash, cooldown, dedupwin):
                        tg.send_markdown(md)
                        sm.mark_alert_sent("range_out", payload_hash)
            else:
                # reset our sentinel when back in range
                if sm.get(KEY_RANGE_OUT_SINCE, 0):
                    sm.set(KEY_RANGE_OUT_SINCE, 0)
            
            # B) twapOk=false for Y minutes
            twap_ok = bool(vstate.get("twapOk", True))
            tf_since = float(sm.get(KEY_TWAP_FALSE_SINCE, 0) or 0)
            if not twap_ok:
                if tf_since == 0:
                    tf_since = now
                    sm.set(KEY_TWAP_FALSE_SINCE, tf_since)
                minutes = (now - tf_since) / 60.0
                if minutes >= s.alert_twap_false_minutes:
                    txt = f"twapOk=false for ~{minutes:.1f} min (window={s.twap_window}s)."
                    payload_hash = hashlib.sha256(txt.encode()).hexdigest()
                    if sm.should_send_alert("twap_false", payload_hash, cooldown, dedupwin):
                        tg.send_text(f"[twap_false] {txt}")
                        sm.mark_alert_sent("twap_false", payload_hash)
            else:
                if tf_since != 0:
                    sm.set(KEY_TWAP_FALSE_SINCE, 0)
            
            # C) Fees USD > threshold (if configured > 0)
            fees_usd = float(obs.get("uncollected_fees_usd", 0.0))
            thr = float(s.alert_fees_usd_threshold or 0.0)
            if thr > 0 and fees_usd >= thr:
                txt = f"Uncollected fees ≈ ${fees_usd:.4f} (>= ${thr:.4f})."
                payload_hash = hashlib.sha256(txt.encode()).hexdigest()
                if sm.should_send_alert("fees_high", payload_hash, cooldown, dedupwin):
                    tg.send_text(f"[fees_high] {txt}")
                    sm.mark_alert_sent("fees_high", payload_hash)
                    
            # ---------- STRATEGY SIGNALS ----------
            signals = evaluate_all(strategies, obs)
            if signals:
                for sig in signals:
                    base_msg = f"[{sig['id']}] {sig['reason']} | action={sig.get('action','')}"
                    if "lower" in sig and "upper" in sig:
                        base_msg += f" | lower={sig['lower']} upper={sig['upper']}"
                    log_warn(base_msg)

                    # keep your previous behavior: range-related signals -> MD block, else text
                    if sig["id"].startswith("rebalance") or obs["out_of_range"]:
                        md = fmt_alert_range(obs)
                        payload_hash = hashlib.sha256(md.encode()).hexdigest()
                        alert_key = f"range:{sig['id']}"
                        if sm.should_send_alert(alert_key, payload_hash, cooldown, dedupwin):
                            tg.send_markdown(md)
                            sm.mark_alert_sent(alert_key, payload_hash)
                        else:
                            log_info("[ALERT] skipped (dedup/cooldown)")
                    else:
                        txt = base_msg
                        payload_hash = hashlib.sha256(txt.encode()).hexdigest()
                        alert_key = f"generic:{sig['id']}"
                        if sm.should_send_alert(alert_key, payload_hash, cooldown, dedupwin):
                            tg.send_text(txt)
                            sm.mark_alert_sent(alert_key, payload_hash)
                        else:
                            log_info("[ALERT] skipped (dedup/cooldown)")

                # append signals to state.json
                alerts = observer.state.get("alerts", [])
                for sig in signals:
                    sig["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                alerts.extend(signals)
                observer.state["alerts"] = alerts[-100:]
                observer._save_state()
            else:
                log_info("No signals.")
        except Exception as e:
            log_warn(f"loop error: {e}")

        time.sleep(s.check_interval)


if __name__ == "__main__":
    main()
