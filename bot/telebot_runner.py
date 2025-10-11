# bot/telebot_runner.py
"""
Telegram bot runner (polling) for the Uni-Range-Bot.

Commands:
  /start
  /status
  /propose
  /rebalance <lower> <upper> [exec]
  /reload

Security / Auth:
- Only messages from TELEGRAM_CHAT_ID (chat/group/channel) OR ALLOWED_USER_IDS (comma-separated user IDs) are allowed.
- If neither is set, the runner refuses to start.

Behavior:
- /status: live on-chain snapshot + USD panel + fees.
- /propose: evaluates JSON strategies (bot/strategy/examples/strategies.json) and prints human-readable suggestions.
- /rebalance: validates (tickSpacing, bounds, cooldown, twapOk) and either dry-runs or executes:
     python -m bot.exec --lower X --upper Y --execute
  It returns stdout and stores a short execution trail in bot/state.json (exec_history).
- /reload: reloads strategies.json without restarting the runner.

Notes:
- Requires python-telegram-bot v20+.
- Leverages your existing Chain, VaultObserver, and strategy registry modules.
"""

import os
import shlex
import json
import subprocess
import time
from html import escape
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

from bot.config import get_settings
from bot.chain import Chain
from bot.observer.vault_observer import VaultObserver
from bot.strategy.registry import handlers
from bot.utils.log import log_info, log_warn
from decimal import Decimal, getcontext
from bot.vault_registry import (
    list_vaults as vault_list,
    add as vault_add,
    set_active as vault_set_active,
    active_alias,
    get as vault_get,
    set_pool as vault_set_pool
)
from bot.state_utils import path_for
from bot.state_utils import load as _state_load, save as _state_save
import re

getcontext().prec = 60  # precisão boa para os cálculos de sqrt/amounts


READ_ONLY = os.environ.get("READ_ONLY", "0").strip() in ("1", "true", "yes")
REQUIRE_CHAT_ONLY = os.environ.get("REQUIRE_CHAT_ONLY", "0").strip() in ("1", "true", "yes")  # exige TELEGRAM_CHAT_ID
BLOCK_DMS = os.environ.get("BLOCK_DMS", "0").strip() in ("1", "true", "yes") 

@contextmanager
def _env_override(mapping: dict[str, str | None]):
    """
    Temporarily override os.environ keys (only ones present in mapping).
    Restores the previous values on exit.
    """
    old = {}
    try:
        for k, v in mapping.items():
            old[k] = os.environ.get(k)
            if v is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
                
def _erc20_meta(ch: Chain, addr: str):
    c = ch.erc20(addr)
    sym = c.functions.symbol().call()
    dec = int(c.functions.decimals().call())
    return c, sym, dec

def _sqrt_ratio_from_tick(tick: int) -> Decimal:
    # sqrt(1.0001^tick)  — versão float/Decimal (aprox. suficiente para exibição)
    return Decimal(1.0001) ** (Decimal(tick) / Decimal(2))

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


class AppCtx:
    """
    Holds per-vault context (Chain + Observer + strategies) bound to one alias.
    """
    def __init__(self, alias: str, rpc_url: str, pool_addr: str, nfpm_addr: str, vault_addr: str):
        self.alias = alias
        self.s = get_settings()
        self.ch = Chain(rpc_url, pool_addr, nfpm_addr, vault_addr)
        self.observer = VaultObserver(self.ch, state_path=str(path_for(alias)))
        self.strategies = load_strategies(os.environ.get("STRATEGIES_FILE"))

class MultiVaultCtx:
    """
    Lazy per-alias ctx cache (Chain + Observer por vault).
    """
    def __init__(self):
        self.s = get_settings()
        self._by_alias: dict[str, AppCtx] = {}

    def get_or_create(self, alias: str) -> "AppCtx":
        if alias in self._by_alias:
            return self._by_alias[alias]
        v = vault_get(alias)
        if not v:
            raise RuntimeError(f"unknown vault alias: {alias}")
        rpc  = v.get("rpc_url") or self.s.rpc_url
        nfpm = v.get("nfpm")
        pool = v.get("pool")
        addr = v["address"]

        ctx = AppCtx(rpc, pool, nfpm, addr)
        self._by_alias[alias] = ctx
        return ctx

MVCTX = MultiVaultCtx()

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
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — simple greeting + command help.
    """
    if not _allowed_chat(update):
        return
    await _reply(
        update,
        context,
        (
            "👋 Uni Range Bot online.\n"
            "Comandos:\n"
            "• /status [@alias]\n"
            "• /balances [@alias]\n"
            "• /history [@alias]\n"
            "• /baseline <set|show> [@alias]\n"
            "• /propose [@alias]\n"
            "• /rebalance <lower> <upper> [exec] [@alias]\n"
            "• /deposit <token> <amount> [exec] [@alias]\n"
            "• /withdraw <pool|all> [exec] [@alias]\n"
            "• /reload\n"
            "\n"
            "Gestão de vaults:\n"
            "• /vault_create <alias> <nfpm> <pool> [rpc]   (deploy + registrar + tornar ativo)\n"
            "• /vault_add <alias> <vault> [pool] [nfpm] [rpc]\n"
            "• /vault_select <alias>\n"
            "• /vault_list\n"
            "• /vault_setpool <alias> <pool>\n"
            "\n"
            "Dica: acrescente @alias no fim do comando p/ agir em um vault específico.\n"
            "Ex.: /status @ethusdc | /rebalance 181800 182200 exec @ethusdc"
        )
    )


async def vault_list_cmd(update, context):
    if not _allowed_chat(update): return
    rows = vault_list()
    if not rows:
        await _reply(update, context, "No vaults yet. Use /vault_add <alias> <address> [pool] [nfpm] [rpc]")
        return
    act = active_alias()
    lines = []
    for v in rows:
        star = "⭐" if v["alias"] == act else " "
        lines.append(f"{star} @{v['alias']}  vault={v['address']}  pool={v.get('pool') or '-'}")
    await _reply(update, context, "\n".join(lines))

async def vault_add_cmd(update, context):
    if not _allowed_chat(update): return
    args = context.args or []
    if len(args) < 2:
        await _reply(update, context, "Usage: /vault_add <alias> <vault_addr> [pool_addr] [nfpm] [rpc_url]")
        return
    alias, addr = args[0], args[1]
    pool = args[2] if len(args) >= 3 else None
    nfpm = args[3] if len(args) >= 4 else None
    rpc  = args[4] if len(args) >= 5 else None
    try:
        vault_add(alias, addr, pool, nfpm, rpc)
        await _reply(update, context, f"✅ added @{alias} -> {addr}")
    except Exception as e:
        await _reply(update, context, f"⚠️ {e}")

async def vault_select_cmd(update, context):
    if not _allowed_chat(update): return
    args = context.args or []
    if not args:
        await _reply(update, context, "Usage: /vault_select <alias>")
        return
    try:
        vault_set_active(args[0])
        await _reply(update, context, f"✅ active vault = @{args[0]}")
    except Exception as e:
        await _reply(update, context, f"⚠️ {e}")

async def vault_set_pool_cmd(update, context):
    if not _allowed_chat(update): return
    args = context.args or []
    if len(args) < 2:
        await _reply(update, context, "Usage: /vault_setpool <alias> <pool_addr>")
        return
    try:
        vault_set_pool(args[0], args[1])
        await _reply(update, context, f"✅ set pool for @{args[0]} = {args[1]}")
    except Exception as e:
        await _reply(update, context, f"⚠️ {e}")
        
async def balances_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /balances — Show vault free balances, uncollected fees, and an estimate
    of the amounts currently allocated in the Uniswap V3 position.

    Notes:
    - A Uniswap V3 position that is OUT-OF-RANGE still has positive liquidity.
      The math below correctly shows 100% in token0 (below range) or 100% in token1 (above range).
    - If your vault_state() returns a signed 'liquidity' (e.g., a net value),
      we treat it as absolute to avoid false "no active liquidity".
    - Fees shown come from your observer snapshot for convenience/consistency.
    """
    if not _allowed_chat(update):
        return

    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)
        
        s = CTX.s
        ch = CTX.ch
        obs = CTX.observer.snapshot(twap_window=s.twap_window)

        # Pool + token metadata
        t0 = ch.pool.functions.token0().call()
        t1 = ch.pool.functions.token1().call()
        c0, sym0, dec0 = _erc20_meta(ch, t0)
        c1, sym1, dec1 = _erc20_meta(ch, t1)

        # Vault "free" balances (not in the position)
        bal0 = Decimal(c0.functions.balanceOf(s.vault).call()) / (Decimal(10) ** dec0)
        bal1 = Decimal(c1.functions.balanceOf(s.vault).call()) / (Decimal(10) ** dec1)

        # Uncollected fees (already humanized by observer)
        fees_h = obs.get("fees_human", {})
        fees0 = Decimal(str(fees_h.get("token0", 0)))
        fees1 = Decimal(str(fees_h.get("token1", 0)))

        # Read the active position from NFPM if possible
        token_id = _read_token_id_from_vault(ch)

        # Default/fallback values
        liq_raw = 0
        lower = int(obs["lower"])
        upper = int(obs["upper"])

        if token_id > 0:
            # NonfungiblePositionManager.positions(tokenId) layout:
            # (nonce, operator, token0, token1, fee, tickLower, tickUpper,
            #  liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128,
            #  tokensOwed0, tokensOwed1)
            pos = ch.nfpm.functions.positions(token_id).call()
            lower = int(pos[5])
            upper = int(pos[6])
            liq_raw = int(pos[7])

        # Treat liquidity as absolute to guard against signed/net values coming from elsewhere
        L = abs(int(liq_raw))

        # Current tick
        cur_tick = int(ch.pool.functions.slot0().call()[1])

        # Estimate amounts held in the position (works in-range and out-of-range)
        pool0 = pool1 = Decimal(0)
        if L > 0:
            a0, a1 = _amounts_from_liquidity(L, cur_tick, lower, upper)
            pool0 = a0 / (Decimal(10) ** dec0)
            pool1 = a1 / (Decimal(10) ** dec1)

        # Totals (free + pool + fees)
        tot0 = bal0 + pool0 + fees0
        tot1 = bal1 + pool1 + fees1

        # Curr price
        usdc_per_eth = Decimal(str(obs["prices"]["current"]["p_t0_t1"]))
        # Convert ALL token1 figures (ETH) to USDC
        bal1_usdc  = (bal1  * usdc_per_eth)
        pool1_usdc = (pool1 * usdc_per_eth)
        fees1_usdc = (fees1 * usdc_per_eth)
        tot1_usdc  = (tot1  * usdc_per_eth)
        
        # Build HTML reply
        html = []
        html.append(f"<b>Vault:</b> <code>{escape(s.vault)}</code>")
        if token_id > 0:
            html.append(f"<b>Position tokenId:</b> <code>{token_id}</code>")
        else:
            html.append("<b>Position tokenId:</b> <i>not found</i>")

        html.append("<b>Token0 / Token1:</b> "
                    f"<code>{escape(sym0)}</code> / <code>{escape(sym1)}</code>")

        html.append("")
        html.append("<b>Free (vault wallet)</b>")
        html.append(f"• {escape(sym0)}: <code>{bal0:.6f}</code>")
        html.append(f"• {escape(sym1)}: <code>{bal1:.6f}</code> (<code>{bal1_usdc:.2f}</code>)")

        html.append("")
        html.append("<b>Pool (position allocation — estimated)</b>")
        html.append(f"• ticks: <code>{lower}</code> → <code>{upper}</code> | curTick=<code>{cur_tick}</code>")
        if L > 0:
            html.append(f"• {escape(sym0)}: <code>{pool0:.6f}</code>")
            html.append(f"• {escape(sym1)}: <code>{pool1:.6f} (<code>{pool1_usdc:.2f}</code>)</code>")
        else:
            html.append("• no active liquidity (L=0)")

        html.append("")
        html.append("<b>Uncollected fees</b>")
        html.append(f"• {escape(sym0)}: <code>{fees0:.6f}</code>")
        html.append(f"• {escape(sym1)}: <code>{fees1:.6f}</code> (<code>{fees1_usdc:.2f}</code>)")

        html.append("")
        html.append("<b>Totals</b>")
        html.append(f"• {escape(sym0)}: <code>{tot0:.6f}</code>  (free + pool + fees)")
        html.append(f"• {escape(sym1)}: <code>{tot1:.6f}</code>  (free + pool + fees)  (<code>{tot1_usdc:.2f}</code>)")

        await _reply(update, context, "\n".join(html), parse_mode=ParseMode.HTML)

    except Exception as e:
        await _reply(update, context, f"⚠️ /balances error: {e}")
        

async def history_cmd(update, context):
    if not _allowed_chat(update):
        return
    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        
        st = _load_bot_state_for(alias)
        hist = st.get("exec_history", [])
        if not hist:
            await _reply(update, context,"No history yet.")
            return

        # monta 5 últimas
        lines = []
        for it in hist[-5:][::-1]:
            tx = it.get("tx")
            txs = (tx[:10] + "…" + tx[-6:]) if tx else "—"
            lines.append(
                f"- {it['ts']} | [{it['lower']},{it['upper']}] | tx={txs}"
            )
        await _reply(update, context,"\n".join(lines))
    except Exception as e:
        await _reply(update, context,f"⚠️ /history error: {e}")
        
        
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)
        
        obs = CTX.observer.snapshot(twap_window=CTX.s.twap_window)
        snap = CTX.observer.usd_snapshot()

        prices_html = fmt_prices_block(obs)
        state_html = fmt_state_block(obs, snap.spot_price, CTX.s.twap_window)
        usd_html = fmt_usd_panel(snap)

        st = _load_bot_state_for(alias)
        fees_usd_cum = float(st.get("fees_cum_usd", 0.0) or 0.0)
        
        extras = f"\n<b>Collected fees (cum)</b>: ≈ ${fees_usd_cum:,.2f}"
        text = (
            f"<b>Vault:</b> <code>{escape(CTX.ch.vault.address)}</code>\n"
            f"{prices_html}\n\n{state_html}\n\n{usd_html}{extras}"
        )
        await _reply(update, context, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await _reply(update, context,f"⚠️ /status error: {e}")


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


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /reload — reload the strategies JSON from disk.
    """
    if not _allowed_chat(update):
        return
    try:
        global STRATEGIES
        STRATEGIES = load_strategies(os.environ.get("STRATEGIES_FILE"))
        await _reply(update, context,"✅ strategies.json reloaded.")
    except Exception as e:
        await _reply(update, context,f"⚠️ /reload error: {e}")


async def propose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)
        v = vault_get(alias) or {}
        
        obs = CTX.observer.snapshot(twap_window=CTX.s.twap_window)

        # Always show each ACTIVE strategy with its status.
        strategies = [st for st in STRATEGIES if st.get("active", True)]
        if not strategies:
            await _reply(update, context, "ℹ️ No active strategies configured.")
            return

        blocks = []
        # Ensure registry.get_settings() sees the right values:
        env_map = {
            "RPC_URL": v.get("rpc_url") or CTX.s.rpc_url,
            "VAULT":   v.get("address"),
            "POOL":    v.get("pool"),
            "NFPM":    v.get("nfpm"),
            "ALIAS":   alias,
        }
        
        with _env_override(env_map):
            for st in strategies:
                sid = st.get("id", "unknown")
                fn = handlers.get(sid)
                if not fn:
                    blocks.append(f"• <b>{escape(sid)}</b>: handler not found.")
                    continue

                res = fn(st.get("params", {}), obs)
                header = f"<b>{escape(sid)}</b> — {escape(st.get('name',''))}"

                if res and res.get("trigger"):
                    # Pretty-print details for breakeven strategy; fallback to compact line for others
                    if sid == "breakeven_single_sided":
                        details_html = _fmt_breakeven_details_html(res)
                        blocks.append(f"{header}\n✅ <i>{escape(res.get('reason','triggered'))}</i>\n{details_html}")
                    else:
                        lower = res.get("lower")
                        upper = res.get("upper")
                        blocks.append(
                            f"{header}\n✅ <i>{escape(res.get('reason','triggered'))}</i>"
                            + (f"\nrange: lower=<code>{lower}</code> upper=<code>{upper}</code>" if lower and upper else "")
                        )
                else:
                    reason = _reason_when_not_triggered(st, obs, res or {}, alias)
                    blocks.append(f"{header}\n❕ <i>{escape(reason)}</i>")

            await _reply(update, context, "\n\n".join(blocks), parse_mode=ParseMode.HTML)

    except Exception as e:
        await _reply(update, context, f"⚠️ /propose error: {e}")
        

async def baseline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /baseline show
    /baseline set   -> define vault_initial_usd = V(P) (preço-apenas) no momento
    """
    if not _allowed_chat(update):
        return
    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)

        sub = args[0].lower() if args else "show"

        if sub == "set":
            snap = CTX.observer.usd_snapshot()  # já exclui fees coletadas
            # grava no state
            st = _load_bot_state_for(alias)
            st["vault_initial_usd"] = float(snap.usd_value)
            st["baseline_set_ts"] = datetime.utcnow().isoformat() + "Z"
            _save_bot_state_for(alias, st)
            await _reply(update, context,
                f"✅ Baseline set.\n"
                f"vault_initial_usd=${snap.usd_value:,.2f}  (preço-apenas, fees coletadas excluídas)"
            )
            return

        # default: show
        st = _load_bot_state_for(alias)
        vinit = st.get("vault_initial_usd", None)
        fees_usd_cum = float(st.get("fees_cum_usd", 0.0) or 0.0)
        if vinit is None:
            await _reply(update, context, "ℹ️ Baseline not set yet. Use /baseline set.")
            return
        snap = CTX.observer.usd_snapshot()
        msg = (
            f"<b>Baseline</b>\n"
            f"• vault_initial_usd: <code>${float(vinit):,.2f}</code>\n"
            f"• V(P) now (preço-apenas): <code>${snap.usd_value:,.2f}</code>\n"
            f"• Δ vs baseline: <code>{snap.delta_usd:+.2f}</code>\n"
            f"• Collected fees (cum, USD aprox): <code>${fees_usd_cum:,.2f}</code>"
        )
        await _reply(update, context, msg, parse_mode=ParseMode.HTML)

    except Exception as e:
        await _reply(update, context, f"⚠️ /baseline error: {e}")
        
        
async def rebalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /rebalance <lower> <upper> [exec]

    Flow:
      1) Parse args and validate (tickSpacing, bounds).
      2) Check cooldown and twapOk using vault state.
      3) If no "exec", do a dry-run and print the suggestion.
      4) If "exec", shell out to: python -m bot.exec --lower L --upper U --execute
         and return the stdout tail to the chat. Also append a short entry in bot/state.json.

    Notes:
      - Requires PRIVATE_KEY et al. in environment when executing.
      - This runner does not broadcast transactions directly; it delegates to your existing wrapper.
    """
    if not _allowed_chat(update):
        return
        
    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)
        
        if CTX.s.read_only_mode:
            args = context.args or []
            if len(args) >= 3 and args[2].lower() in ("exec", "execute", "run"):
                await _reply(update, context,
                    "🔒 Read-only mode is enabled. Execution commands are disabled. "
                    "Unset READ_ONLY_MODE to allow transactions."
                )
                return
        
        if len(args) < 2:
            await _reply(update, context,"Usage: /rebalance <lower> <upper> [exec]")
            return
        lower = int(args[0])
        upper = int(args[1])
        do_exec = (len(args) >= 3 and args[2].lower() in ("exec", "execute", "run"))

        # Validations
        vstate = CTX.ch.vault_state()
        spacing = CTX.ch.pool.functions.tickSpacing().call()
        _validate_ticks(lower, upper, spacing)

        # Cooldown (allow if never rebalanced: lastRebalance=0)
        last = int(vstate["lastRebalance"])
        now = int(datetime.utcnow().timestamp())
        since = now - last if last > 0 else 10**9
        if since < CTX.s.min_cooldown:
            await _reply(update, context,
                f"⏱️ Cooldown not passed. ~{CTX.s.min_cooldown - since}s remaining."
            )
            return

        # TWAP guard (use vault.twapOk())
        if not vstate["twapOk"]:
            await update.message.reply_text("📉 TWAP guard failed (twapOk=false).")
            return

        if not do_exec:
            await _reply(update, context,
                f"🧪 Dry-run OK.\nSuggested: lower={lower}, upper={upper}\n"
                "To execute: /rebalance <lower> <upper> exec"
            )
            return

        # ---- PRE-EXEC SNAPSHOT (para acumular fees após exec) ----
        pre_obs = CTX.observer.snapshot(twap_window=CTX.s.twap_window)
        pre_fees0 = int(pre_obs["uncollected_fees_token0"])
        pre_fees1 = int(pre_obs["uncollected_fees_token1"])
        pre_usdc_per_eth = float(CTX.observer.usd_snapshot().spot_price)
        meta = CTX.ch.pool_meta() 
        dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
            
        # Execution via wrapper (python -m bot.exec)
        cmd = f"python -m bot.exec --lower {lower} --upper {upper} --execute --vault {alias}"
        await _reply(update, context,f"🚀 Executing:\n<code>{escape(cmd)}</code>", parse_mode=ParseMode.HTML)

        proc = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            env=os.environ
        )

        if proc.returncode != 0:
            await _reply(update, context,
                f"❌ Execution failed:\n<pre><code>{escape(proc.stderr or proc.stdout)[:3500]}</code></pre>",
                parse_mode=ParseMode.HTML
            )

            return

        out = proc.stdout[-3000:]
        await _reply(update, context,
            f"✅ Execution complete.\n<pre><code>{escape(out)}</code></pre>",
            parse_mode=ParseMode.HTML
        )

        # ---- accumulate collected fees (off-chain) ----
        try:
            _add_collected_fees_to_state(pre_fees0, pre_fees1, pre_usdc_per_eth, dec0, dec1, alias)
        except Exception as e:
            log_warn(f"failed to persist fees_collected_cum: {e}")
            
        try:
            st = _load_bot_state_for(alias)
            history = st.get("exec_history", [])
            history.append({
                "ts": datetime.utcnow().isoformat() + "Z",
                "lower": lower,
                "upper": upper,
                "stdout_tail": out,
            })
            st["exec_history"] = history[-50:]
            _save_bot_state_for(alias, st)
        except Exception:
            pass

    except Exception as e:
        await _reply(update, context,f"⚠️ /rebalance error: {e}")


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /withdraw pool [exec]        -> exit position to vault (pool -> vault)
    /withdraw all [exec]         -> exit position and withdraw all to owner
    """
    if not _allowed_chat(update):
        return

    args = context.args or []
    if not args or args[0].lower() not in ("pool", "all"):
        await _reply(update, context, "Usage:\n/withdraw pool [exec]\n/withdraw all [exec]")
        return

    mode = args[0].lower()
    do_exec = (len(args) >= 2 and args[1].lower() in ("exec", "execute", "run"))

    try:
        # Dry-run summary
        alias = _resolve_alias_from_args(args)
        CTX = MVCTX.get_or_create(alias)
        
        ch = CTX.ch
        vs = ch.vault_state()
        token_id = int(vs.get("tokenId", 0) or 0)
        lower, upper, liq = int(vs["lower"]), int(vs["upper"]), int(vs["liq"])
        msg = [f"Mode={mode} | tokenId={token_id} | liq={liq} | ticks=[{lower},{upper}]"]

        if not do_exec:
            msg.insert(0, "🧪 Dry-run")
            await _reply(update, context, "\n".join(msg))
            return

        # Execute via existing wrapper (same pattern as /rebalance)
        if mode == "pool":
            cmd = "python -m bot.exec --vault-exit"
        else:
            cmd = "python -m bot.exec --vault-exit-withdraw"
        cmd += f" --vault {alias}"
        
        await _reply(update, context, f"🚀 Executing:\n<code>{escape(cmd)}</code>", parse_mode=ParseMode.HTML)
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, env=os.environ)

        if proc.returncode != 0:
            await _reply(update, context,
                f"❌ Execution failed:\n<pre><code>{escape(proc.stderr or proc.stdout)[:3500]}</code></pre>",
                parse_mode=ParseMode.HTML
            )
            return

        out = proc.stdout[-3000:]
        await _reply(update, context, f"✅ Done.\n<pre><code>{escape(out)}</code></pre>", parse_mode=ParseMode.HTML)

        # persists history per alias
        try:
            # try extract tx hash from stout
            txh = None
            m = re.search(r"transactionHash\s+(0x[0-9a-fA-F]{64})", proc.stdout or "")
            if m:
                txh = m.group(1)

            st = _state_load(alias)
            hist = st.get("exec_history", [])
            hist.append({
                "ts": datetime.utcnow().isoformat() + "Z",
                "mode": ("exit" if mode == "pool" else "exit_withdraw"),
                "lower": None,
                "upper": None,
                "tx": txh,
                "stdout_tail": out,
            })
            st["exec_history"] = hist[-50:]
            _state_save(alias, st)
        except Exception as _e:
            log_warn(f"failed to append withdraw history for @{alias}: {_e}")
    except Exception as e:
        await _reply(update, context, f"⚠️ /withdraw error: {e}")
        

async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /deposit <token> <amount> [exec] [@alias]

    Validates:
      - alias resolve
      - vault has pool set
      - token belongs to pool (token0 or token1)
    Dry-run prints metadata; exec runs python -m bot.exec --deposit --token ... --amount ... --vault @alias

    Notes:
      - Amount is human (e.g., 1000.5). On-chain raw units are computed in exec.py using token decimals.
      - If pool token1 is WETH, deposit WETH (not ETH). Wrapping ETH can be added later if desired.
    """
    if not _allowed_chat(update):
        return
    try:
        args = context.args or []
        if len(args) < 2:
            await _reply(update, context, "Usage: /deposit <token_addr> <amount> [exec] [@alias]")
            return

        token = args[0]
        amount = args[1]
        do_exec = False

        # capture optional flags and alias
        rest = args[2:]
        alias = _resolve_alias_from_args(rest) if rest else _resolve_alias_from_args([])
        if rest and len(rest) > 0 and rest[0].lower() in ("exec", "execute", "run"):
            do_exec = True

        CTX = MVCTX.get_or_create(alias)
        ch = CTX.ch

        # validate pool set
        pool_addr = CTX.s  # só pra deixar explícito no escopo
        vrow = vault_get(alias) or {}
        pool = vrow.get("pool")
        if not pool:
            await _reply(update, context, f"⚠️ Vault @{alias} has no pool set. Use /vault_setpool <alias> <pool>")
            return

        # validate token is part of pool
        t0 = ch.pool.functions.token0().call()
        t1 = ch.pool.functions.token1().call()
        if token.lower() not in (t0.lower(), t1.lower()):
            await _reply(update, context, "⚠️ Token is not part of the pool (must be token0 or token1).")
            return

        # read token metadata for UX
        c = ch.erc20(token)
        sym = c.functions.symbol().call()
        dec = int(c.functions.decimals().call())

        if not do_exec:
            await _reply(
                update,
                context,
                (
                    "🧪 Dry-run deposit\n"
                    f"• alias=@{alias}\n"
                    f"• token={token} ({sym}, {dec} dec)\n"
                    f"• amount={amount}\n\n"
                    "To execute: /deposit <token> <amount> exec [@alias]"
                )
            )
            return

        # Execute via existing wrapper (python -m bot.exec)
        cmd = f"python -m bot.exec --deposit --token {token} --amount {amount} --execute --vault @{alias}"
        await _reply(update, context, f"🚀 Executing:\n<code>{escape(cmd)}</code>", parse_mode=ParseMode.HTML)

        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, env=os.environ)
        if proc.returncode != 0:
            await _reply(
                update,
                context,
                f"❌ Execution failed:\n<pre><code>{escape(proc.stderr or proc.stdout)[:3500]}</code></pre>",
                parse_mode=ParseMode.HTML
            )
            return

        out = proc.stdout[-3000:]
        await _reply(update, context, f"✅ Deposit complete.\n<pre><code>{escape(out)}</code></pre>", parse_mode=ParseMode.HTML)

    except Exception as e:
        await _reply(update, context, f"⚠️ /deposit error: {e}")
        

async def collect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /collect [exec] [@alias]

    Dry-run:
      - Shows current uncollected fees (token0/token1 + USD estimate) for the active position.
    Exec:
      - Runs `python -m bot.exec --collect --vault @alias --execute`
      - Upon success, we add PRE-EXEC uncollected fees to off-chain cumulative counters,
        just like we do in /rebalance, and append a short entry to exec_history and collect_history.
    """
    if not _allowed_chat(update):
        return

    try:
        args = context.args or []
        alias = _resolve_alias_from_args(args)  # supports trailing @alias
        do_exec = False
        if args and len(args) > 0 and args[0].lower() in ("exec", "execute", "run"):
            do_exec = True

        CTX = MVCTX.get_or_create(alias)
        ch = CTX.ch

        # Snapshot for pre-exec fees and USD conversion
        obs = CTX.observer.snapshot(twap_window=CTX.s.twap_window)
        pre_fees0_raw = int(obs.get("uncollected_fees_token0", 0))
        pre_fees1_raw = int(obs.get("uncollected_fees_token1", 0))
        snap = CTX.observer.usd_snapshot()  # spot USDC/ETH
        usdc_per_eth = float(snap.spot_price)

        meta = ch.pool_meta()
        dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
        sym0, sym1 = meta["sym0"], meta["sym1"]

        # Humanize for dry-run
        pre_fees0 = pre_fees0_raw / (10 ** dec0)
        pre_fees1 = pre_fees1_raw / (10 ** dec1)
        pre_fees_usd = pre_fees0 + pre_fees1 * usdc_per_eth

        if not do_exec:
            msg = (
                "🧪 Dry-run collect\n"
                f"• alias=@{alias}\n"
                f"• uncollected: {pre_fees0:.6f} {sym0} + {pre_fees1:.6f} {sym1} (≈ ${pre_fees_usd:.4f})\n\n"
                "To execute: /collect exec [@alias]"
            )
            await _reply(update, context, msg)
            return

        # Execute collect via exec.py
        cmd = f"python -m bot.exec --collect --vault @{alias} --execute"
        await _reply(update, context, f"🚀 Executing:\n<code>{escape(cmd)}</code>", parse_mode=ParseMode.HTML)
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, env=os.environ)

        if proc.returncode != 0:
            await _reply(update, context,
                f"❌ Execution failed:\n<pre><code>{escape(proc.stderr or proc.stdout)[:3500]}</code></pre>",
                parse_mode=ParseMode.HTML
            )
            return

        out = proc.stdout[-3000:]
        await _reply(update, context, f"✅ Collect done.\n<pre><code>{escape(out)}</code></pre>", parse_mode=ParseMode.HTML)

        # Accumulate PRE-EXEC snapshot into off-chain counters (same rule as rebalance)
        try:
            _add_collected_fees_to_state(
                pre_exec_fees0_raw=pre_fees0_raw,
                pre_exec_fees1_raw=pre_fees1_raw,
                usdc_per_eth=usdc_per_eth,
                dec0=dec0,
                dec1=dec1,
                alias=alias
            )
        except Exception as e:
            log_warn(f"failed to persist fees_collected_cum after collect: {e}")

        # Append to history (exec_history is already updated by exec.py; we keep a small shadow here if desired)
        try:
            st = _load_bot_state_for(alias)
            col = st.get("collect_history", [])
            # try extract tx hash from stdout
            txh = None
            m = re.search(r"transactionHash\s+(0x[0-9a-fA-F]{64})", proc.stdout or "")
            if m:
                txh = m.group(1)

            col.append({
                "ts": datetime.utcnow().isoformat() + "Z",
                "fees0_raw": pre_fees0_raw,
                "fees1_raw": pre_fees1_raw,
                "fees_usd_est": pre_fees_usd,
                "tx": txh,
                "stdout_tail": out,
            })
            st["collect_history"] = col[-200:]
            _save_bot_state_for(alias, st)
        except Exception as _e:
            log_warn(f"failed to append collect history for @{alias}: {_e}")

    except Exception as e:
        await _reply(update, context, f"⚠️ /collect error: {e}")
        
  
async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Default handler for unrecognized messages.
    """
    if not _allowed_chat(update):
        return
    await _reply(
        update,
        context,
        (
            "👋 Uni Range Bot online.\n"
            "Comandos:\n"
            "• /status [@alias]\n"
            "• /balances [@alias]\n"
            "• /history [@alias]\n"
            "• /baseline <set|show> [@alias]\n"
            "• /propose [@alias]\n"
            "• /rebalance <lower> <upper> [exec] [@alias]\n"
            "• /deposit <token> <amount> [exec] [@alias]\n"
            "• /withdraw <pool|all> [exec] [@alias]\n"
            "• /reload\n"
            "\n"
            "Gestão de vaults:\n"
            "• /vault_create <alias> <nfpm> <pool> [rpc]   (deploy + registrar + tornar ativo)\n"
            "• /vault_add <alias> <vault> [pool] [nfpm] [rpc]\n"
            "• /vault_select <alias>\n"
            "• /vault_list\n"
            "• /vault_setpool <alias> <pool>\n"
            "\n"
            "Dica: acrescente @alias no fim do comando p/ agir em um vault específico.\n"
            "Ex.: /status @ethusdc | /rebalance 181800 182200 exec @ethusdc"
        )
    )


def _require_env(name: str) -> str:
    """
    Reads an env var and throws a runtime error when missing.
    """
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v


def main():
    """
    Entrypoint — builds the telegram application, registers handlers and starts polling.
    """
    token = _require_env("TELEGRAM_BOT_TOKEN")
    # Require at least one auth mechanism
    if not (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("ALLOWED_USER_IDS")):
        raise RuntimeError("Configure TELEGRAM_CHAT_ID or ALLOWED_USER_IDS")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("propose", propose_cmd))
    app.add_handler(CommandHandler("rebalance", rebalance_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("balances", balances_cmd))
    app.add_handler(CommandHandler("baseline", baseline_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("vault_list", vault_list_cmd))
    app.add_handler(CommandHandler("vault_add", vault_add_cmd))
    app.add_handler(CommandHandler("vault_select", vault_select_cmd))
    app.add_handler(CommandHandler("vault_setpool", vault_set_pool_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("collect", collect_cmd))
    app.add_handler(MessageHandler(filters.ALL, fallback))

    log_info("Telegram runner up. Listening for commands...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
