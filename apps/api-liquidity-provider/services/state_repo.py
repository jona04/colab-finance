"""
Per-alias state repository (state/<alias>.json).
Used to track position, liquidity, ticks, history, etc.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from ..config import get_settings

def _dex_root(dex: str) -> Path:
    s = get_settings()
    return Path(s.DATA_ROOT) / dex

def _state_dir(dex: str) -> Path:
    return _dex_root(dex) / "state"

def _state_path(dex: str, alias: str) -> Path:
    return _state_dir(dex) / f"{alias}.json"

def ensure_dirs(dex: str):
    _state_dir(dex).mkdir(parents=True, exist_ok=True)

def load_state(dex: str, alias: str) -> Dict[str, Any]:
    ensure_dirs(dex)
    p = _state_path(dex, alias)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def save_state(dex: str, alias: str, data: Dict[str, Any]):
    ensure_dirs(dex)
    _state_path(dex, alias).write_text(json.dumps(data, indent=2))

def update_state(dex: str, alias: str, updates: Dict[str, Any]):
    """Merge partial updates into the current state."""
    cur = load_state(dex, alias)
    cur.update(updates)
    save_state(dex, alias, cur)

def ensure_state_initialized(
    dex: str,
    alias: str,
    *,
    vault_address: str,
    nfpm: Optional[str] = None,
    pool: Optional[str] = None,
    gauge: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Ensure a minimal state document exists.
    Returns the up-to-date state.
    """
    st = load_state(dex, alias)
    changed = False
    if not st:
        st = {
            "vault_address": vault_address,
            "nfpm": nfpm,
            "pool": pool,
            "gauge": gauge,
            "created_at": datetime.utcnow().isoformat(),
            "positions": [],
            # running totals and histories
            "fees_collected_cum": {"token0_raw": 0, "token1_raw": 0},
            "fees_cum_usd": 0.0,
            "rewards_usdc_cum": {"usdc_raw": 0, "usdc_human": 0.0},
            "rewards_collect_history": [], 
            "exec_history": [],
            "collect_history": [],
            "deposit_history": [],
        }
        changed = True
    else:
        # garante chaves importantes existirem (idempotente)
        if "vault_address" not in st:
            st["vault_address"] = vault_address; changed = True
        if "nfpm" not in st and nfpm is not None:
            st["nfpm"] = nfpm; changed = True
        if "pool" not in st and pool is not None:
            st["pool"] = pool; changed = True
        if "gauge" not in st and gauge is not None:
            st["gauge"] = gauge; changed = True
        if "positions" not in st:
            st["positions"] = []; changed = True
        if "fees_collected_cum" not in st:
            st["fees_collected_cum"] = {"token0_raw": 0, "token1_raw": 0}; changed = True
        if "exec_history" not in st:
            st["exec_history"] = []; changed = True
        if "error_history" not in st:
            st["error_history"] = []; changed = True
        if "fees_cum_usd" not in st:
            st["fees_cum_usd"] = 0.0; changed = True
        if "rewards_usdc_cum" not in st:
            st["rewards_usdc_cum"] = {"usdc_raw": 0, "usdc_human": 0.0}; changed = True
        if "rewards_collect_history" not in st:
            st["rewards_collect_history"] = []; changed = True
        if "collect_history" not in st:
            st["collect_history"] = []; changed = True
        if "deposit_history" not in st:
            st["deposit_history"] = []; changed = True
        if "vault_initial_usd" not in st:
            # opcional: setar baseline na primeira leitura do status
            pass
            
    if extra:
        # merge raso, sem sobrescrever se já houver chave
        for k, v in extra.items():
            if k not in st:
                st[k] = v
                changed = True

    if changed:
        save_state(dex, alias, st)
    return st

def append_history(dex: str, alias: str, key: str, entry: Dict[str, Any], limit: int = 200):
    """
    Append an entry to a history array (e.g., exec_history, collect_history),
    trimming to the most recent `limit` items.
    """
    st = load_state(dex, alias)
    arr = st.get(key, [])
    arr.append(entry)
    st[key] = arr[-limit:]
    save_state(dex, alias, st)

def add_collected_fees_snapshot(
    dex: str,
    alias: str,
    *,
    fees0_raw: int,
    fees1_raw: int,
    fees_usd_est: float
):
    """
    Add a pre-exec fee snapshot to cumulative counters — same policy as the bot:
    you add the *pre-collect* values if the tx succeeds.
    """
    st = load_state(dex, alias)
    cum = st.get("fees_collected_cum", {"token0_raw": 0, "token1_raw": 0})
    cum["token0_raw"] = int(cum.get("token0_raw", 0)) + int(fees0_raw or 0)
    cum["token1_raw"] = int(cum.get("token1_raw", 0)) + int(fees1_raw or 0)
    st["fees_collected_cum"] = cum
    st["fees_cum_usd"] = float(st.get("fees_cum_usd", 0.0)) + float(fees_usd_est or 0.0)
    st["last_fees_update_ts"] = datetime.utcnow().isoformat()
    save_state(dex, alias, st)

def add_rewards_usdc_snapshot(
    dex: str,
    alias: str,
    *,
    usdc_raw: int,
    usdc_human: float,
    meta: Optional[Dict[str, Any]] = None
):
    """
    Soma ao acumulado de 'rewards' (já convertidos para USDC).
    Use isso após um swap AERO->USDC bem-sucedido.
    """
    st = load_state(dex, alias)
    cum = st.get("rewards_usdc_cum", {"usdc_raw": 0, "usdc_human": 0.0})
    cum["usdc_raw"]   = int(cum.get("usdc_raw", 0)) + int(usdc_raw or 0)
    cum["usdc_human"] = float(cum.get("usdc_human", 0.0)) + float(usdc_human or 0.0)
    st["rewards_usdc_cum"] = cum

    hist = st.get("rewards_collect_history", [])
    hist.append({
        "ts": datetime.utcnow().isoformat(),
        "usdc_raw": int(usdc_raw),
        "usdc_human": float(usdc_human),
        "meta": (meta or {}),
    })
    st["rewards_collect_history"] = hist[-200:]  # mantém curto

    save_state(dex, alias, st)