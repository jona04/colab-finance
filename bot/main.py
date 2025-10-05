"""
Main bot loop — observes the vault, evaluates JSON-driven strategies, and prints signals.
"""
import os
import time
from pathlib import Path
from bot.config import get_settings
from bot.chain import Chain
from bot.observer.vault_observer import VaultObserver
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

    log_info("Observer up. Polling on-chain state...")

    while True:
        try:
            obs = observer.snapshot(twap_window=s.twap_window)
            log_info(f"tick={obs['tick']} inRange={not obs['out_of_range']} twap_window={s.twap_window}s "
                     f"fees≈${obs['uncollected_fees_usd']:.4f} vol={obs['volatility_pct']:.3f}%")

            signals = evaluate_all(strategies, obs)
            if signals:
                for sig in signals:
                    msg = f"[{sig['id']}] {sig['reason']} | action={sig.get('action','')}"
                    if "lower" in sig and "upper" in sig:
                        msg += f" | lower={sig['lower']} upper={sig['upper']}"
                    log_warn(msg)
            else:
                log_info("No signals.")

        except Exception as e:
            log_warn(f"loop error: {e}")

        time.sleep(s.check_interval)


if __name__ == "__main__":
    main()
