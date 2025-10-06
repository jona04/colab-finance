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

    # telegram + state manager para dedupe/cooldown
    tg = TelegramClient()
    sm = StateManager("bot/state.json")  # use o mesmo arquivo do observer se preferir unificar

    # parâmetros de dedupe/cooldown
    cooldown = int(os.environ.get("ALERTS_COOLDOWN_SEC", "60"))
    dedupwin = int(os.environ.get("ALERTS_DEDUP_WINDOW_SEC", "180"))
    
    log_info("Observer up. Polling on-chain state...")

    while True:
        try:
            obs = observer.snapshot(twap_window=s.twap_window)

            # preços em ambas visões
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

            snap = observer.usd_snapshot()
            log_info(
                f"USD      total≈${snap.usd_value:,.2f} | ΔUSD={snap.delta_usd:+.2f} | "
                f"baseline=${snap.baseline_usd:,.2f} | USDC/ETH={snap.spot_price:.2f}"
            )
            
            signals = evaluate_all(strategies, obs)
            if signals:
                for sig in signals:
                    # log humano
                    base_msg = f"[{sig['id']}] {sig['reason']} | action={sig.get('action','')}"
                    if "lower" in sig and "upper" in sig:
                        base_msg += f" | lower={sig['lower']} upper={sig['upper']}"
                    log_warn(base_msg)

                    # alerta Telegram com dedupe/cooldown
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
                observer.state["alerts"] = alerts[-100:]  # keep last 100
                observer._save_state()

            else:
                log_info("No signals.")
        except Exception as e:
            log_warn(f"loop error: {e}")

        time.sleep(s.check_interval)


if __name__ == "__main__":
    main()
