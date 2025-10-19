# apps/api/routes/strategies.py

from fastapi import APIRouter, HTTPException
from datetime import datetime

from ..services import vault_repo, state_repo
from ..services.strategy_repo import load_strategies, save_strategies
from ..strategy.engine import evaluate_strategies
from ..domain.strategy_models import StrategiesConfig, ProposalsResponse, StrategyExecuteRequest
from ..services.tx_service import TxService
from ..adapters import uniswap_v3
from ..routes.vaults import _adapter_for  # reuse factory

router = APIRouter(tags=["strategies"])

@router.get("/strategies/{dex}/{alias}/config", response_model=StrategiesConfig)
def strategies_get_config(dex: str, alias: str):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    return load_strategies()

@router.put("/strategies/{dex}/{alias}/config", response_model=StrategiesConfig)
def strategies_put_config(dex: str, alias: str, cfg: StrategiesConfig):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    save_strategies(cfg)
    return cfg

@router.get("/strategies/{dex}/{alias}/proposals", response_model=ProposalsResponse)
def strategies_proposals(dex: str, alias: str):
    v = vault_repo.get_vault(dex, alias)
    if not v:
        raise HTTPException(404, "Unknown alias")
    if not v.get("pool"):
        raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    # extra ctx if you need (empty for now)
    props = evaluate_strategies(dex, alias, ad, {})
    return {"alias": alias, "vault": v["address"], "pool": v.get("pool"), "proposals": props}

@router.post("/strategies/{dex}/{alias}/execute")
def strategies_execute(dex: str, alias: str, req: StrategyExecuteRequest):
    """
    Execute a given proposal by calling vault.rebalanceWithCaps(lower, upper, caps).
    Caps are optional; when None -> 0 (use all available in vault).
    """
    v = vault_repo.get_vault(dex, alias)
    if not v:
        raise HTTPException(404, "Unknown alias")
    if not v.get("pool"):
        raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    meta = ad.pool_meta()

    # convert human caps to raw
    cap0_raw = cap1_raw = None
    if req.cap0 is not None:
        cap0_raw = int(float(req.cap0) * (10 ** int(meta["dec0"])))
    if req.cap1 is not None:
        cap1_raw = int(float(req.cap1) * (10 ** int(meta["dec1"])))

    # optional dry-run: just return the tx payload shape (no send)
    if req.dry_run:
        fn = ad.fn_rebalance_caps(req.lower, req.upper, cap0_raw, cap1_raw)
        tx = fn.build_transaction({"from": TxService(v.get("rpc_url")).account.address})  # no sign/send
        return {"dry_run": True, "tx_preview": tx}

    fn = ad.fn_rebalance_caps(req.lower, req.upper, cap0_raw, cap1_raw)
    txh = TxService(v.get("rpc_url")).send(fn)

    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "strategy_execute",
        "strategy_id": req.id,
        "lower": req.lower,
        "upper": req.upper,
        "cap0": req.cap0,
        "cap1": req.cap1,
        "tx": txh
    })
    return {"tx": txh}
