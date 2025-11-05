"""
Vault registry (vaults.json) – manages vault metadata and active alias.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
from ..config import get_settings

def _dex_root(dex: str) -> Path:
    s = get_settings()
    return Path(s.DATA_ROOT) / dex

def _vaults_path(dex: str) -> Path:
    return _dex_root(dex) / "vaults.json"

def ensure_dirs(dex: str):
    _dex_root(dex).mkdir(parents=True, exist_ok=True)
    if not _vaults_path(dex).exists():
        _vaults_path(dex).write_text(json.dumps({"active": None, "vaults": {}}, indent=2))

# registry ops
def list_vaults(dex: str) -> Dict[str, Any]:
    ensure_dirs(dex)
    try:
        return json.loads(_vaults_path(dex).read_text())
    except Exception:
        return {"active": None, "vaults": {}}

def add_vault(dex: str, alias: str, row: Dict[str, Any]):
    d = list_vaults(dex)
    if alias in d["vaults"]:
        raise ValueError("alias already exists")
    d["vaults"][alias] = row
    if d.get("active") is None:
        d["active"] = alias
    _vaults_path(dex).write_text(json.dumps(d, indent=2))

def set_active(dex: str, alias: str):
    d = list_vaults(dex)
    if alias not in d["vaults"]:
        raise ValueError("unknown alias")
    d["active"] = alias
    _vaults_path(dex).write_text(json.dumps(d, indent=2))

def get_vault(dex: str, alias: str) -> Optional[Dict[str, Any]]:
    return list_vaults(dex).get("vaults", {}).get(alias)

def set_pool(dex: str, alias: str, pool_addr: str):
    d = list_vaults(dex)
    if alias not in d["vaults"]:
        raise ValueError("unknown alias")
    d["vaults"][alias]["pool"] = pool_addr
    _vaults_path(dex).write_text(json.dumps(d, indent=2))

def get_vault_any(alias: str):
    v = get_vault("uniswap", alias)
    if v:
        return "uniswap", v
    v = get_vault("aerodrome", alias)
    if v:
        return "aerodrome", v
    v = get_vault("pancake", alias)
    if v:
        return "pancake", v
    return None, None