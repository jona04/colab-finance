"""
Registry of strategy handlers.
Each strategy is a pure function that receives `params` and the observed vault state (`obs`).
They return a signal dict: {"trigger": bool, "reason": str, ...}.
"""

import time
from ..utils.ticks import pct_to_ticks, align_to_spacing


def range_follow_usdc(params, obs):
    """
    Bidirectional range follower.
    Decides new range based on whether the price is above or below the current range.

    If price is above the range => 100% ETH side → propose new range below the price.
    If price is below the range => 100% USDC side → propose new range above the price.
    """
    tick = obs["tick"]
    upper, lower = obs["upper"], obs["lower"]
    spacing = obs["spacing"]

    out_pct = params["rebalance_if_outside_pct"]
    near = params["near_pct"]
    far = params["far_pct"]

    if obs["pct_outside_tick"] < out_pct:
        return {"trigger": False}

    # Above the current range (100% token1 = ETH)
    if tick > upper:
        new_lower = align_to_spacing(tick - pct_to_ticks(far), spacing)
        new_upper = align_to_spacing(tick - pct_to_ticks(near), spacing)
        side = "above"
    # Below the current range (100% token0 = USDC)
    elif tick < lower:
        new_lower = align_to_spacing(tick + pct_to_ticks(near), spacing)
        new_upper = align_to_spacing(tick + pct_to_ticks(far), spacing)
        side = "below"
    else:
        return {"trigger": False}

    return {
        "trigger": True,
        "reason": f"Price {side} range by {obs['pct_outside_tick']:.2f}%",
        "action": "reallocate",
        "lower": new_lower,
        "upper": new_upper,
        "width_ticks": abs(new_upper - new_lower)
    }


def time_based_recenter(params, obs):
    """
    Simple time-based refresh.
    If price remains outside the range for more than `outside_duration_sec`,
    suggest reallocation (not recenter) based on current side of liquidity.
    """
    if not obs["out_of_range"]:
        obs["out_since"] = 0
        return {"trigger": False}

    if time.time() - obs.get("out_since", time.time()) > params["outside_duration_sec"]:
        side = "above" if obs["tick"] > obs["upper"] else "below"
        return {
            "trigger": True,
            "reason": f"Out of range {side} for too long",
            "action": "reallocate"
        }

    return {"trigger": False}


def loss_avoidance_rebalance(params, obs):
    """
    Tracks entry price (average of last position).
    Before performing rebalance, estimates whether the new rebalance would result in
    a loss compared to the stored entry price.

    If price moved in the opposite direction and the unrealized PnL < -alert_threshold_pct,
    emits a warning instead of a rebalance signal.
    """
    if not params["enable_loss_check"]:
        return {"trigger": False}

    entry_price = obs.get("entry_price")
    current_price = obs["spot_price"]

    if not entry_price:
        return {"trigger": False}

    delta_pct = ((current_price - entry_price) / entry_price) * 100

    if delta_pct < -params["alert_threshold_pct"]:
        return {
            "trigger": True,
            "reason": f"Potential loss {delta_pct:.2f}% vs entry",
            "action": "warn",
            "severity": "high"
        }

    return {"trigger": False}


def volatility_adjust_width(params, obs):
    """
    Adjusts range width dynamically based on volatility.
    If volatility exceeds thresholds, increase or decrease width accordingly.
    """
    vol = obs.get("volatility_pct", 0)
    width = obs["upper"] - obs["lower"]

    if vol > params["widen_if_vol_above_pct"]:
        width = int(width * params["adjust_factor"][0])
        return {"trigger": True, "reason": "High volatility, widening range", "width": width}

    if vol < params["tighten_if_vol_below_pct"]:
        width = int(width * params["adjust_factor"][1])
        return {"trigger": True, "reason": "Low volatility, tightening range", "width": width}

    return {"trigger": False}


def profit_threshold_guard(params, obs):
    """
    Ensures fees collected exceed the gas cost threshold before rebalancing.
    """
    profit_usd = obs.get("uncollected_fees_usd", 0)
    gas_usd = params["gas_usd_estimate"]

    if profit_usd - gas_usd < params["min_fee_usd"]:
        return {"trigger": False}

    return {
        "trigger": True,
        "reason": f"Fees {profit_usd:.2f} USD > threshold",
        "action": "collect_fees"
    }


handlers = {
    "range_follow_usdc": range_follow_usdc,
    "time_based_recenter": time_based_recenter,
    "loss_avoidance_rebalance": loss_avoidance_rebalance,
    "volatility_adjust_width": volatility_adjust_width,
    "profit_threshold_guard": profit_threshold_guard,
}
