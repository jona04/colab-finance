import os
import time
import math
import json
import re

from html import escape
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

from bot.state_utils import load as _state_load, save as _state_save
from bot.vault_registry import (
    active_alias,
)
from bot.strategy.registry import handlers
from bot.config import get_settings
from bot.utils.log import log_info, log_warn
from bot.chain import Chain

def load_strategies(path: str | None = None):
    """
    Reads strategies JSON from disk. Defaults to:
      bot/strategy/examples/strategies.json

    Raises FileNotFoundError if not present.
    """
    if path is None:
        base = Path(__file__).resolve().parent
        path = base / "strategy" / "examples" / "strategies.json"
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Strategies file not found: {path}")

    return json.loads(path.read_text(encoding="utf-8"))

try:
    STRATEGIES = load_strategies(os.environ.get("STRATEGIES_FILE"))
    log_info(f"Loaded {len(STRATEGIES)} strategies.")
except Exception as _e:
    STRATEGIES = []
    log_warn(f"Failed to load strategies: {_e}")
    

# ===== /simulate_range helpers ==================================================

def _price_token1_per_token0_scaled_from_tick(tick: int, dec0: int, dec1: int) -> float:
    """
    Returns price token1/token0 with decimals scaling:
      p_t1_t0_scaled = 1.0001^tick * 10^(dec0 - dec1)
    """
    base = pow(1.0001, tick)
    scale = pow(10.0, dec0 - dec1)
    return base * scale

def _usdc_eth_views_from_tick(tick: int, dec0: int, dec1: int, usdc_idx: int, eth_idx: int) -> tuple[float, float]:
    """
    Given a tick, returns (ETH/USDC, USDC/ETH) respecting pool order.
    """
    p_t1_t0 = _price_token1_per_token0_scaled_from_tick(tick, dec0, dec1)
    # If token1=USDC & token0=ETH -> p_t1_t0 is USDC/ETH; else ETH/USDC
    if usdc_idx == 1 and eth_idx == 0:
        usdc_per_eth = p_t1_t0
        eth_per_usdc = 0.0 if usdc_per_eth == 0 else 1.0 / usdc_per_eth
    else:
        eth_per_usdc = p_t1_t0
        usdc_per_eth = float("inf") if eth_per_usdc == 0 else 1.0 / eth_per_usdc
    return eth_per_usdc, usdc_per_eth

def _detect_indices_usdc_eth(sym0: str, sym1: str) -> tuple[int, int]:
    """
    Returns (usdc_idx, eth_idx) from token symbols. Raises on failure.
    """
    s0, s1 = sym0.upper(), sym1.upper()

    def is_usdc(s: str) -> bool:
        return any(tag in s for tag in ("USDC", "USDBC", "USDCE"))

    def is_eth(s: str) -> bool:
        return any(tag in s for tag in ("WETH", "ETH"))

    u = 0 if is_usdc(s0) else (1 if is_usdc(s1) else -1)
    e = 0 if is_eth(s0) else (1 if is_eth(s1) else -1)
    if u < 0 or e < 0 or u == e:
        raise ValueError("Unable to detect USDC/ETH indices from symbols.")
    return u, e

def _tick_from_usdc_per_eth_target(usdc_per_eth: float,
                                   dec0: int, dec1: int,
                                   usdc_idx: int, eth_idx: int) -> int:
    """
    Returns the integer tick whose scaled p_t1_t0 implies the given USDC/ETH,
    handling token order automatically.

    If token1=USDC and token0=ETH: p_t1_t0_scaled = USDC/ETH.
    Else: p_t1_t0_scaled = 1 / (USDC/ETH).
    """
    if usdc_per_eth <= 0:
        raise ValueError("Invalid USDC/ETH price (<=0).")

    if usdc_idx == 1 and eth_idx == 0:
        desired_p_t1_t0 = float(usdc_per_eth)
    else:
        desired_p_t1_t0 = 1.0 / float(usdc_per_eth)

    scale = pow(10.0, dec0 - dec1)
    base = desired_p_t1_t0 / scale
    if base <= 0.0:
        raise ValueError("Invalid scaled price base.")
    return int(round(math.log(base) / math.log(1.0001)))

def _tick_from_eth_per_usdc_target(eth_per_usdc: float,
                                   dec0: int, dec1: int,
                                   usdc_idx: int, eth_idx: int) -> int:
    """
    Returns tick from ETH/USDC directly.
    We transform to USDC/ETH and reuse the function above.
    """
    if eth_per_usdc <= 0:
        raise ValueError("Invalid ETH/USDC price (<=0).")
    usdc_per_eth = 1.0 / float(eth_per_usdc)
    return _tick_from_usdc_per_eth_target(usdc_per_eth, dec0, dec1, usdc_idx, eth_idx)

def _align_tick(tick: int, spacing: int, direction: str = "nearest") -> int:
    """
    Aligns a tick to tickSpacing.
      direction: "down" | "up" | "nearest"
    """
    r = tick % spacing
    if direction == "down":
        return tick - r
    if direction == "up":
        return tick + (spacing - r if r != 0 else 0)
    # nearest
    down = tick - r
    up = tick + (spacing - r if r != 0 else 0)
    return down if abs(down - tick) <= abs(up - tick) else up

def _center_and_width(lower: int, upper: int) -> tuple[float, int]:
    """
    Returns (center_float, width_int) in ticks.
    """
    return (lower + upper) / 2.0, upper - lower

def _resize_width_around_center(lower: int, upper: int, spacing: int, pct: float, increase: bool) -> tuple[int, int]:
    """
    Symmetrically resizes the width around the same center by +/- pct.
    pct: e.g., 0.10 = 10%
    increase=True => widen; False => narrow.
    Result ticks are aligned to spacing and guaranteed lower<upper with at least 1*spacing width.
    """
    c, w = _center_and_width(lower, upper)
    if w <= 0:
        raise ValueError("Invalid width (<=0).")
    factor = 1.0 + pct if increase else max(1e-9, 1.0 - pct)
    new_w = max(spacing, int(round(w * factor / spacing)) * spacing)
    # keep center, split half/half
    half = new_w // 2
    # if even-odd issues, enforce at least 1*spacing separation
    new_lower = _align_tick(int(round(c)) - half, spacing, "down")
    new_upper = _align_tick(int(round(c)) + (new_w - (int(round(c)) - new_lower)), spacing, "up")
    if new_upper <= new_lower:
        new_upper = new_lower + spacing
    return new_lower, new_upper

def _parse_percent_flag(arg: str) -> float:
    """
    Parses 'increase_width=10%' or 'decrease_width=15%' -> 0.10 / 0.15
    """
    m = re.match(r"^(increase_width|decrease_width)\s*=\s*([0-9]+(\.[0-9]+)?)\s*%$", arg.strip(), re.IGNORECASE)
    if not m:
        raise ValueError("Invalid width flag. Use increase_width=10% or decrease_width=10%.")
    pct = float(m.group(2)) / 100.0
    if pct < 0 or pct > 1e6:
        raise ValueError("Unreasonable percentage.")
    return pct

def _estimate_mint_amounts_needed(cur_tick: int, lower: int, upper: int,
                                  dec0: int, dec1: int) -> tuple[Decimal, Decimal]:
    """
    Estimates the *human* (decimals-adjusted) amounts needed for a mint at the current tick,
    using canonical Uniswap v3 formulas with sqrt ratios.
    Returns (need0_human, need1_human).
    Note: For in-range case, both are >0; for out-of-range, it's single-sided.
    """
    Pa = _sqrt_ratio_from_tick(lower)
    Pb = _sqrt_ratio_from_tick(upper)
    P  = _sqrt_ratio_from_tick(cur_tick)

    # Use L=1 scaling, amounts are proportional to L.
    # amount0 = L*(Pb - P)/(P*Pb) when P between Pa and Pb; else piecewise
    # amount1 = L*(P - Pa)         when P between Pa and Pb; else piecewise
    if P <= Pa:
        amt0 = (Pb - Pa) / (Pa * Pb)  # token0-only
        amt1 = Decimal(0)
    elif P >= Pb:
        amt0 = Decimal(0)
        amt1 = (Pb - Pa)              # token1-only
    else:
        amt0 = (Pb - P) / (P * Pb)
        amt1 = (P - Pa)

    # humanize: amounts per unit of L, caller can scale if desired.
    h0 = amt0  # already dimensionless for per-L; will just present ratios
    h1 = amt1
    # we keep them as "per unit of L". Users can read proportion; we also show vault balances next to it.
    return (h0, h1)

def _read_idle_and_pool_amounts(ch: Chain, dec0: int, dec1: int) -> tuple[Decimal, Decimal, Decimal, Decimal, int, int, int]:
    """
    Reads idle balances (token0/token1), current tick, and estimates pool amounts from current liquidity.
    Returns (idle0, idle1, pool0, pool1, lower, upper, cur_tick) — all balances in human units.
    """
    t0 = ch.pool.functions.token0().call()
    t1 = ch.pool.functions.token1().call()
    c0 = ch.erc20(t0)
    c1 = ch.erc20(t1)
    idle0 = Decimal(c0.functions.balanceOf(ch.vault.address).call()) / (Decimal(10) ** dec0)
    idle1 = Decimal(c1.functions.balanceOf(ch.vault.address).call()) / (Decimal(10) ** dec1)

    token_id = _read_token_id_from_vault(ch)
    lower = upper = 0
    pool0 = pool1 = Decimal(0)
    cur_tick = int(ch.pool.functions.slot0().call()[1])

    if token_id > 0:
        pos = ch.nfpm.functions.positions(token_id).call()
        lower = int(pos[5]); upper = int(pos[6])
        L = abs(int(pos[7]))
        if L > 0:
            a0, a1 = _amounts_from_liquidity(L, cur_tick, lower, upper)
            pool0 = a0 / (Decimal(10) ** dec0)
            pool1 = a1 / (Decimal(10) ** dec1)

    return idle0, idle1, pool0, pool1, lower, upper, cur_tick

def _fmt_range_block_html(lower: int, upper: int, spacing: int,
                          dec0: int, dec1: int, usdc_idx: int, eth_idx: int) -> str:
    """
    Builds an HTML block with ticks and prices at bounds in both views.
    """
    e_low, u_low = _usdc_eth_views_from_tick(lower, dec0, dec1, usdc_idx, eth_idx)
    e_up,  u_up  = _usdc_eth_views_from_tick(upper, dec0, dec1, usdc_idx, eth_idx)
    return (
        f"<b>Range</b> ticks: <code>{lower}</code> → <code>{upper}</code>  (spacing=<code>{spacing}</code>)\n"
        f"• ETH/USDC: lower=<code>{e_low:.10f}</code> | upper=<code>{e_up:.10f}</code>\n"
        f"• USDC/ETH: lower=<code>{u_low:.2f}</code> | upper=<code>{u_up:.2f}</code>"
    )
              
def _sqrt_ratio_from_tick(tick: int) -> Decimal:
    # sqrt(1.0001^tick)  — versão float/Decimal (aprox. suficiente para exibição)
    return Decimal(1.0001) ** (Decimal(tick) / Decimal(2))


# balances_cmd helpers

def _erc20_meta(ch: Chain, addr: str):
    c = ch.erc20(addr)
    sym = c.functions.symbol().call()
    dec = int(c.functions.decimals().call())
    return c, sym, dec

def _amounts_from_liquidity(liq: int, cur_tick: int, lower: int, upper: int):
    """
    Estima amounts (token0, token1) para uma posição Uniswap V3.
    Fórmulas (region-based):
      if P <= Pa: amount0 = L*(Pb - Pa)/(Pa*Pb), amount1 = 0
      if Pa < P < Pb: amount0 = L*(Pb - P)/(P*Pb), amount1 = L*(P - Pa)
      if P >= Pb: amount0 = 0, amount1 = L*(Pb - Pa)
    Onde P = sqrt(price), Pa = sqrt(price at lower), Pb = sqrt(price at upper)
    """
    L = Decimal(liq)
    P  = _sqrt_ratio_from_tick(cur_tick)
    Pa = _sqrt_ratio_from_tick(lower)
    Pb = _sqrt_ratio_from_tick(upper)
    if P <= Pa:
        amt0 = L * (Pb - Pa) / (Pa * Pb)
        amt1 = Decimal(0)
    elif P >= Pb:
        amt0 = Decimal(0)
        amt1 = L * (Pb - Pa)
    else:
        amt0 = L * (Pb - P) / (P * Pb)
        amt1 = L * (P - Pa)
    return amt0, amt1

async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode: ParseMode | None = None):
    """
    Safe reply helper:
    - Works even if update.message is None (e.g., channel posts, edited messages).
    - Uses effective_chat.id to send messages.
    """
    chat = update.effective_chat
    if not chat:
        return
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode=parse_mode
    )
    
def _allowed_chat(update: Update) -> bool:
    """
    Authorization gate:
      1) TELEGRAM_CHAT_ID must match (group/channel/DM), if configured.
      2) Otherwise allow if user is in ALLOWED_USER_IDS (from Settings).
      3) If BLOCK_DM=true in Settings, reject private chats.
    """
    chat = update.effective_chat
    user = update.effective_user
    s = get_settings()

    # 3) block DMs if requested
    try:
        if s.block_dm and chat and getattr(chat, "type", None) == ChatType.PRIVATE:
            return False
    except Exception:
        pass

    # 1) exact chat id match (still read from env – Telegram infra var)
    tgid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tgid and chat and str(chat.id) == str(tgid):
        return True

    # 2) per-user allow-list (Settings)
    if s.allowed_user_ids and user:
        return str(user.id) in s.allowed_user_ids

    return False

def _validate_ticks(lower: int, upper: int, spacing: int):
    """
    Validates tick bounds:
      - lower < upper
      - both multiples of tickSpacing
    Raises ValueError on invalid input.
    """
    if lower >= upper:
        raise ValueError("lower must be < upper")
    if lower % spacing != 0 or upper % spacing != 0:
        raise ValueError(f"ticks must be multiples of spacing={spacing}")

def _read_token_id_from_vault(ch: Chain) -> int:
    """
    Best-effort attempt to fetch the Uniswap V3 position tokenId from the vault.

    Priority:
      1) vault.tokenId()      (common naming)
      2) vault.positionId()   (some projects)
      3) ch.vault_state().get("tokenId")
    Returns 0 if nothing is found.
    """
    try:
        return int(ch.vault.functions.tokenId().call())
    except Exception:
        pass
    try:
        return int(ch.vault.functions.positionId().call())
    except Exception:
        pass
    try:
        vs = ch.vault_state()
        if "tokenId" in vs:
            return int(vs["tokenId"])
    except Exception:
        pass
    return 0

def evaluate_all(strategies, obs):
    """
    Evaluates all active strategies against the current observation.

    Returns a list of dicts with at least:
      { "id": <strategy_id>, "trigger": True, "reason": "...", ... }
    and optionally { "lower": int, "upper": int } if there is a range suggestion.
    """
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

def fmt_prices_block(obs: dict) -> str:
    """
    HTML-safe: mostra ETH/USDC e USDC/ETH em current/lower/upper.
    """
    pr_cur = obs["prices"]["current"]
    pr_low = obs["prices"]["lower"]
    pr_up  = obs["prices"]["upper"]

    return (
        "<b>PRICES</b> (token1/token0 = ETH/USDC | token0/token1 = USDC/ETH)\n"
        f"  current: tick={pr_cur['tick']:,} | ETH/USDC={pr_cur['p_t1_t0']:.10f} | USDC/ETH={pr_cur['p_t0_t1']:.2f}\n"
        f"  lower:   tick={pr_low['tick']:,} | ETH/USDC={pr_low['p_t1_t0']:.10f} | USDC/ETH={pr_low['p_t0_t1']:.2f}\n"
        f"  upper:   tick={pr_up['tick']:,} | ETH/USDC={pr_up['p_t1_t0']:.10f} | USDC/ETH={pr_up['p_t0_t1']:.2f}"
    )

def fmt_state_block(obs: dict, spot_usdc_per_eth: float, twap_window: int) -> str:
    """
    HTML-safe: estado do range, fees e spot.
    """
    fees = obs.get("fees_human", {})
    sym0 = escape(fees.get("sym0", "TOKEN0"))
    sym1 = escape(fees.get("sym1", "TOKEN1"))

    in_range = "✅" if not obs["out_of_range"] else "❌"
    side = escape(obs.get("range_side", "-"))

    return (
        f"<b>STATE</b> side={side} | inRange={in_range} | "
        f"pct_outside_tick≈{obs['pct_outside_tick']:.3f}% | twap_window={twap_window}s | vol={obs['volatility_pct']:.3f}%\n"
        f"<b>FEES</b>  uncollected: {fees.get('token0', 0.0):.6f} {sym0} + {fees.get('token1', 0.0):.6f} {sym1} "
        f"(≈ ${obs['uncollected_fees_usd']:.4f})\n"
        f"<i>Spot USDC/ETH ≈ {spot_usdc_per_eth:,.2f}</i>"
    )

def fmt_usd_panel(snap) -> str:
    """
    HTML-safe: painel USD.
    """
    return (
        f"<b>USD</b> total≈${snap.usd_value:,.2f} | ΔUSD={snap.delta_usd:+.2f} | "
        f"baseline=${snap.baseline_usd:,.2f}"
    )

def _resolve_alias_from_args(args) -> str:
    """
    Resolve vault alias. Se última arg começa com "@", usa esse alias e remove-o de args.
    Caso contrário, usa o 'active' salvo em bot/vaults.json.
    """
    a = active_alias()
    if args and args[-1].startswith("@"):
        a = args[-1][1:]
        args.pop()
    if not a:
        raise RuntimeError("No active vault. Use /vault_add ou /vault_select primeiro.")
    return a

def _load_bot_state_for(alias: str) -> dict:
    return _state_load(alias)

def _save_bot_state_for(alias: str, d: dict):
    _state_save(alias, d)

def _add_collected_fees_to_state(
    pre_exec_fees0_raw: int,
    pre_exec_fees1_raw: int,
    usdc_per_eth: float,
    dec0: int,
    dec1: int,
    alias: str
):
    """
    Called only after a successful on-chain action.
    Adds *pre-exec* uncollected fees snapshot into off-chain cumulative counters.
    """
    st = _load_bot_state_for(alias)
    fees_col = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})
    fees_col["token0_raw"] = int(fees_col.get("token0_raw", 0) or 0) + int(pre_exec_fees0_raw or 0)
    fees_col["token1_raw"] = int(fees_col.get("token1_raw", 0) or 0) + int(pre_exec_fees1_raw or 0)
    st["fees_collected_cum"] = fees_col

    # Humanize with provided decimals (no global CTX usage).
    fees0_h = (pre_exec_fees0_raw or 0) / (10 ** dec0)
    fees1_h = (pre_exec_fees1_raw or 0) / (10 ** dec1)
    add_usd = float(fees0_h + fees1_h * float(usdc_per_eth))
    st["fees_cum_usd"] = float(st.get("fees_cum_usd", 0.0) or 0.0) + add_usd
    st["last_fees_update_ts"] = datetime.utcnow().isoformat() + "Z"

    _save_bot_state_for(alias, st)
  
def _fmt_breakeven_details_html(s: dict) -> str:
    """
    Build a rich HTML block for the breakeven_single_sided strategy result.
    Assumes s["details"] exists (only when trigger=True).
    """
    d = s.get("details", {}) or {}
    ticks = d.get("ticks", {})
    prices = d.get("prices", {})
    be = d.get("breakeven", {})

    # ETH/USDC
    e_lower = prices.get("eth_per_usdc", {}).get("lower", {}) or {}
    e_upper = prices.get("eth_per_usdc", {}).get("upper", {}) or {}
    # USDC/ETH
    u_lower = prices.get("usdc_per_eth", {}).get("lower", {}) or {}
    u_upper = prices.get("usdc_per_eth", {}).get("upper", {}) or {}

    curr_usdc_per_eth = float(prices.get("current", {}).get("usdc_per_eth", 0.0))
    curr_eth_per_usdc = float(prices.get("current", {}).get("eth_per_usdc", 0.0))
    curr_tick = float(prices.get("current", {}).get("tick", 0))

    side = s.get("range_side", "-")
    be_boundary = be.get("boundary", "-")
    profit_usd = be.get("profit_usd", 0.0)
    baseline = be.get("baseline_usd", 0.0)
    target = be.get("target_usd", 0.0)
    buf = be.get("buffer_pct", 0.0)

    # consolidated delta lines vs current (both price views)
    ed_low = abs(float(e_lower.get("delta_pct", 0.0)))
    ed_up  = abs(float(e_upper.get("delta_pct", 0.0)))
    ud_low = abs(float(u_lower.get("delta_pct", 0.0)))
    ud_up  = abs(float(u_upper.get("delta_pct", 0.0)))

    lines = []
    lines.append(f"<b>action</b>=reallocate | side=<code>{escape(side)}</code>")
    lines.append(f"<b>ticks</b>: lower=<code>{ticks.get('lower')}</code> | upper=<code>{ticks.get('upper')}</code>")

    # ETH/USDC block (unchanged)
    lines.append("<b>ETH/USDC</b>: "
                 f"lower=<code>{e_lower.get('price', 0.0):.10f}</code> ({e_lower.get('sign','')}{ed_low:.3f}%) | "
                 f"upper=<code>{e_upper.get('price', 0.0):.10f}</code> ({e_upper.get('sign','')}{ed_up:.3f}%)")

    # USDC/ETH block — FIXED order labeling (lower then upper)
    lines.append("<b>USDC/ETH</b>: "
                 f"upper=<code>{u_lower.get('price', 0.0):.2f}</code> ({u_lower.get('sign','')}{ud_low:.3f}%) | "
                 f"lower=<code>{u_upper.get('price', 0.0):.2f}</code> ({u_upper.get('sign','')}{ud_up:.3f}%)")

    # NEW: concise consolidated delta line
    lines.append(f"<b>Δ vs current</b>: USDC/ETH → lower {u_lower.get('sign','')}{ud_low:.3f}% | upper {u_upper.get('sign','')}{ud_up:.3f}% "
                 f"| ETH/USDC → lower {e_lower.get('sign','')}{ed_low:.3f}% | upper {e_upper.get('sign','')}{ed_up:.3f}%")

    lines.append(f"<b>USDC/ETH</b>: Current=<code>{curr_usdc_per_eth:.2f}</code>")
    lines.append(f"<b>ETH/USDC</b>: Current=<code>{curr_eth_per_usdc:.6f}</code>")
    lines.append(f"<b>Tick</b>: Current=<code>{curr_tick:.2f}</code>")
    lines.append(f"<b>breakeven at</b> <code>{be_boundary}</code> | "
                 f"target V(P)≈<code>${target:,.2f}</code> vs baseline≈<code>${baseline:,.2f}</code> "
                 f"(buffer={buf*100:.3f}%)")
    lines.append(f"<b>profit at boundary</b>: <code>${profit_usd:,.2f}</code>")
    return "\n".join(lines)

def _reason_when_not_triggered(strat: dict, obs: dict, res: dict, alias: str) -> str:
    """
    Produce a human-friendly reason when a strategy did not trigger.
    Uses the strategy's own 'reason' if present; otherwise derives a sensible default.
    """
    # If strategy provided a reason, prefer it.
    r = (res or {}).get("reason")
    if r:
        return r

    # Derive generic reasons for the breakeven strategy
    if strat.get("id") == "breakeven_single_sided":
        if not obs.get("out_of_range", False):
            return "Price is inside the current range."
        out_since = float(obs.get("out_since") or 0.0)
        minutes_out = (time.time() - out_since) / 60.0 if out_since else 0.0
        min_minutes = float(strat.get("params", {}).get("minimum_minutes_out_of_range", 10))
        if minutes_out < min_minutes:
            return f"Outside for ~{minutes_out:.1f} min (< required {min_minutes:.1f} min)."
        # Baseline missing?
        try:
            st = _load_bot_state_for(alias)
            if float(st.get("vault_initial_usd", 0.0) or 0.0) <= 0.0:
                return "Baseline not set. Use /baseline set."
        except Exception:
            pass
        return "Conditions not met for breakeven at minimal width."

    return "No trigger."
