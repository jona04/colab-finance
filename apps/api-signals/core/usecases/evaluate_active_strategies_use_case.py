import logging
from typing import Dict, List, Optional, Tuple

from ..services.strategy_reconciler_service import StrategyReconcilerService

from ..repositories.strategy_repository import StrategyRepository
from ..repositories.strategy_episode_repository import StrategyEpisodeRepository
from ..repositories.signal_repository import SignalRepository
from ..repositories.indicator_set_repository import IndicatorSetRepository


class EvaluateActiveStrategiesUseCase:
    """
    Evaluates all ACTIVE strategies tied to a given indicator set when a new snapshot arrives.
    Applies gates similar to the backtest (breakout, high-vol, tiers + cooldown)
    and reconciles desired episode with Liquidity Provider by emitting signals (PENDING).
    """

    def __init__(
        self,
        strategy_repo: StrategyRepository,
        episode_repo: StrategyEpisodeRepository,
        signal_repo: SignalRepository,
        reconciling_service: StrategyReconcilerService,
        logger: Optional[logging.Logger] = None,
    ):
        self._strategy_repo = strategy_repo
        self._episode_repo = episode_repo
        self._signal_repo = signal_repo
        self._reconciler = reconciling_service
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    @staticmethod
    def _trend_at(ema_fast_val: float, ema_slow_val: float) -> str:
        return "up" if ema_fast_val > ema_slow_val else "down"

    @staticmethod
    def _gate_breakout(P: float, Pa: float, Pb: float, eps: float) -> Optional[str]:
        if P > Pb * (1 + eps):
            return "cross_max"
        if P < Pa * (1 - eps):
            return "cross_min"
        return None

    @staticmethod
    def _gate_high_vol(atr_pct: Optional[float], threshold: Optional[float]) -> bool:
        return (atr_pct is not None) and (threshold is not None) and (atr_pct > threshold)

    @staticmethod
    def _apply_major_cap_and_floor(pct_below: float, pct_above: float,
                                   max_major_side_pct: Optional[float],
                                   min_major_side_pct_high_vol: Optional[float],
                                   high_vol: bool) -> Tuple[float, float]:
        major = max(pct_below, pct_above)
        minor = min(pct_below, pct_above)
        # cap
        if max_major_side_pct is not None and major > max_major_side_pct:
            scale = max_major_side_pct / major
            major *= scale; minor *= scale
        # floor in high vol
        if high_vol and min_major_side_pct_high_vol is not None and major < min_major_side_pct_high_vol:
            scale = (min_major_side_pct_high_vol / major) if major > 0 else 1.0
            major *= scale; minor *= scale
        # restore orientation
        if pct_below >= pct_above:
            pct_below, pct_above = major, minor
        else:
            pct_below, pct_above = minor, major
        return float(pct_below), float(pct_above)

    def _pick_band_for_trend(self, P: float, trend: str, params: Dict, atr_pct_now: Optional[float],
                             force_high_vol: Optional[bool] = None,
                             cap_override: Optional[float] = None) -> Tuple[float, float, str, str, bool]:
        # base skew
        if trend == "down":
            majority = "token1"; mode = "trend_down"
            pct_below = float(params.get("skew_low_pct", 0.09))
            pct_above = float(params.get("skew_high_pct", 0.01))
        else:
            majority = "token2"; mode = "trend_up"
            pct_below = float(params.get("skew_high_pct", 0.01))
            pct_above = float(params.get("skew_low_pct", 0.09))

        # vol regime
        if force_high_vol is not None:
            high_vol = bool(force_high_vol)
        else:
            th = params.get("vol_high_threshold_pct")
            high_vol = (atr_pct_now is not None and th is not None and atr_pct_now > th)

        pct_below, pct_above = self._apply_major_cap_and_floor(
            pct_below, pct_above,
            max_major_side_pct=cap_override if cap_override is not None else params.get("max_major_side_pct"),
            min_major_side_pct_high_vol=params.get("vol_high_min_major_side_pct"),
            high_vol=high_vol,
        )
        Pa = max(1e-12, P * (1.0 - pct_below))
        Pb = P * (1.0 + pct_above)
        mid_pad = 1e-12 * P
        Pa = min(P - mid_pad, Pa)
        Pb = max(P + mid_pad, Pb)
        return Pa, Pb, mode, majority, high_vol

    # ===== execute =====

    async def execute_for_snapshot(self, indicator_set: Dict, snapshot: Dict) -> None:
        """
        Evaluate every ACTIVE strategy that references the given indicator_set with the provided snapshot.
        Potentially closes/opens episodes and emits reconciliation signals into 'signals'.
        """
        symbol = snapshot["symbol"]
        P = float(snapshot["close"])
        ema_f = float(snapshot["ema_fast"])
        ema_s = float(snapshot["ema_slow"])
        atr_pct = float(snapshot["atr_pct"])
        ts = int(snapshot["ts"])

        strategies = await self._strategy_repo.get_active_by_indicator_set(indicator_set_id=indicator_set["cfg_hash"])
        # NOTE: If you prefer ObjectId, replace cfg_hash by real _id in both strategy and indicator_set.
        if not strategies:
            return

        for strat in strategies:
            params = strat["params"]
            eps = float(params.get("eps", 1e-6))
            cooloff = int(params.get("cooloff_bars", 1))

            # 1) load current episode or open the first one if none
            current = await self._episode_repo.get_open_by_strategy(strat_id := strat["name"])
            if current is None:
                # first open centered band using initial range around price
                Pa, Pb, mode, majority, _ = self._pick_band_for_trend(
                    P, self._trend_at(ema_f, ema_s), params, atr_pct
                )
                new_ep = {
                    "_id": f"ep_{strat_id}_{ts}",
                    "strategy_id": strat_id,
                    "symbol": symbol,
                    "pool_type": "standard",
                    "mode_on_open": mode,
                    "majority_on_open": majority,
                    "open_time": ts,
                    "open_time_iso": snapshot.get("created_at_iso", None),
                    "open_price": P,
                    "Pa": Pa, "Pb": Pb,
                    "last_event_bar": 0,
                    "atr_streak": {tier["name"]: 0 for tier in params.get("tiers", [])},
                }
                await self._episode_repo.open_new(new_ep)
                # reconcile desired vs LP
                signal = await self._reconciler.reconcile(strat_id, new_ep, symbol)
                if signal:
                    await self._signal_repo.upsert_signal({
                        "strategy_id": strat_id,
                        "indicator_set_id": indicator_set["cfg_hash"],
                        "cfg_hash": indicator_set["cfg_hash"],
                        "symbol": symbol,
                        "ts": ts,
                        "signal_type": signal["signal_type"],
                        "payload": signal["payload"],
                        "status": "PENDING",
                        "attempts": 0,
                    })
                continue

            # cooldown logic needs a bar index; we can approximate by counting from open_time
            i_since_open = current.get("last_event_bar", 0) + 1

            # 2) gates
            trigger = None

            brk = self._gate_breakout(P, current["Pa"], current["Pb"], eps)
            if brk and (i_since_open >= cooloff):
                trigger = brk

            elif self._gate_high_vol(atr_pct, params.get("vol_high_threshold_pct")) and current.get("pool_type") != "high_vol":
                trigger = "high_vol"

            # tiers – from narrow to wide
            elif current["Pa"] < P < current["Pb"]:
                tiers = list(params.get("tiers", []))
                tiers.sort(key=lambda t: t["atr_pct_threshold"])  # ensure ordering
                # update streaks and pick the narrowest allowed
                streaks = current.get("atr_streak", {})
                chosen_tier = None
                for tier in reversed(tiers):
                    name = tier["name"]
                    allowed_from = tier.get("allowed_from", [])
                    if current.get("pool_type") not in allowed_from and current.get("pool_type") != name:
                        continue
                    # update streak counter
                    if atr_pct <= float(tier["atr_pct_threshold"]):
                        streaks[name] = int(streaks.get(name, 0)) + 1
                    else:
                        streaks[name] = 0
                    if streaks[name] >= int(tier["bars_required"]):
                        chosen_tier = tier
                        break
                if chosen_tier and (i_since_open >= cooloff):
                    self._logger.info(f"Tier : {chosen_tier}")
                    trigger = f"tighten_{chosen_tier['name']}"
                # persist updated streaks even if no trigger
                await self._episode_repo.update_partial(current["_id"], {"atr_streak": streaks, "last_event_bar": i_since_open})

            # 3) if no trigger, nothing to do this bar
            if not trigger:
                continue

            # 4) close current and open next band based on trigger
            await self._episode_repo.close_episode(
                current["_id"], {
                    "close_time": ts,
                    "close_time_iso": snapshot.get("created_at_iso", None),
                    "close_reason": trigger,
                    "close_price": P,
                }
            )

            trend_now = self._trend_at(ema_f, ema_s)

            def _open_with_cap(pool_type: str, cap_override: Optional[float]):
                Pa, Pb, mode, majority, _ = self._pick_band_for_trend(
                    P, trend_now, params, atr_pct, cap_override=cap_override
                )
                return {
                    "_id": f"ep_{strat_id}_{ts}",
                    "strategy_id": strat_id,
                    "symbol": symbol,
                    "pool_type": pool_type,
                    "mode_on_open": mode,
                    "majority_on_open": majority,
                    "open_time": ts,
                    "open_time_iso": snapshot.get("created_at_iso", None),
                    "open_price": P,
                    "Pa": Pa, "Pb": Pb,
                    "last_event_bar": 0,
                    "atr_streak": {tier["name"]: 0 for tier in params.get("tiers", [])},
                }

            new_ep = None
            if trigger in ("cross_min", "cross_max"):
                # try tiers (narrowest allowed), else standard
                tiers = params.get("tiers", [])
                if tiers:
                    tiers_sorted = sorted(tiers, key=lambda t: t["atr_pct_threshold"])
                    for tier in reversed(tiers_sorted):  # narrowest first
                        cap = float(tier["max_major_side_pct"])
                        new_ep = _open_with_cap(tier["name"], cap_override=cap)
                        break
                if new_ep is None:
                    new_ep = _open_with_cap("standard", cap_override=params.get("standard_max_major_side_pct"))
            elif trigger == "high_vol":
                new_ep = _open_with_cap("high_vol", cap_override=params.get("high_vol_max_major_side_pct"))
            elif trigger.startswith("tighten_"):
                tier_name = trigger.split("_", 1)[1]
                tier = next((t for t in params.get("tiers", []) if t["name"] == tier_name), None)
                cap = float(tier["max_major_side_pct"]) if tier else params.get("standard_max_major_side_pct")
                new_ep = _open_with_cap(tier_name if tier else "standard", cap_override=cap)

            if new_ep:
                await self._episode_repo.open_new(new_ep)
                # reconcile with LP and emit signal if needed
                signal = await self._reconciler.reconcile(strat_id, new_ep, symbol)
                if signal:
                    await self._signal_repo.upsert_signal({
                        "strategy_id": strat_id,
                        "indicator_set_id": indicator_set["cfg_hash"],
                        "cfg_hash": indicator_set["cfg_hash"],
                        "symbol": symbol,
                        "ts": ts,
                        "signal_type": signal["signal_type"],
                        "payload": signal["payload"],
                        "status": "PENDING",
                        "attempts": 0,
                    })
