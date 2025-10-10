import json
from pathlib import Path
from typing import Optional, Dict, Any, List

_VAULTS_PATH = Path("bot/vaults.json")

_SCHEMA_HINT = {
    "active": None,                  # alias ativo
    "vaults": {
        # "alias": {"address": "0x...", "pool": "0x...", "nfpm": "0x...", "rpc_url": "..."}
    }
}

def _load() -> Dict[str, Any]:
    if not _VAULTS_PATH.exists():
        return {"active": None, "vaults": {}}
    try:
        return json.loads(_VAULTS_PATH.read_text())
    except Exception:
        return {"active": None, "vaults": {}}

def _save(d: Dict[str, Any]) -> None:
    _VAULTS_PATH.write_text(json.dumps(d, indent=2))

def list_vaults() -> List[Dict[str, Any]]:
    d = _load()
    out = []
    for alias, v in d.get("vaults", {}).items():
        out.append({"alias": alias, **v})
    return out

def get(alias: str) -> Optional[Dict[str, Any]]:
    return _load().get("vaults", {}).get(alias)

def add(alias: str, address: str, pool: Optional[str]=None, nfpm: Optional[str]=None, rpc_url: Optional[str]=None):
    d = _load()
    if alias in d["vaults"]:
        raise ValueError("alias already exists")
    d["vaults"][alias] = {
        "address": address,
        "pool": pool,
        "nfpm": nfpm,
        "rpc_url": rpc_url,
    }
    if d.get("active") is None:
        d["active"] = alias
    _save(d)

def set_active(alias: str):
    d = _load()
    if alias not in d.get("vaults", {}):
        raise ValueError("unknown alias")
    d["active"] = alias
    _save(d)

def active_alias() -> Optional[str]:
    return _load().get("active")

def active_vault() -> Optional[Dict[str, Any]]:
    d = _load()
    a = d.get("active")
    if not a: return None
    return d.get("vaults", {}).get(a)

def set_pool(alias: str, pool_addr: str):
    d = _load()
    if alias not in d["vaults"]:
        raise ValueError("unknown alias")
    d["vaults"][alias]["pool"] = pool_addr
    _save(d)
