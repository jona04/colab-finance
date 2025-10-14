import json
import os
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException, Body
from web3 import Web3
from ..config import get_settings
from ..domain.models import (
    VaultList, VaultRow, AddVaultRequest, SetPoolRequest,
    DeployVaultRequest, OpenRequest, RebalanceRequest, WithdrawRequest,
    DepositRequest, CollectRequest, BaselineRequest, StatusResponse
)
from ..services import state_repo, vault_repo
from ..services.tx_service import TxService
from ..services.chain_reader import compute_status
from ..adapters.uniswap_v3 import UniswapV3Adapter
from ..adapters.aerodrome import AerodromeAdapter
from ..domain.models import StakeRequest, UnstakeRequest, ClaimRewardsRequest

router = APIRouter()

def _adapter_for(dex: str, pool: str, nfpm: str | None, vault: str, rpc_url: str | None):
    s = get_settings()
    w3 = Web3(Web3.HTTPProvider(rpc_url or s.RPC_URL_DEFAULT))
    if dex == "uniswap":
        return UniswapV3Adapter(w3, pool, nfpm, vault)
    if dex == "aerodrome":
        return AerodromeAdapter(w3, pool, nfpm, vault)  # stub raises NotImplemented
    raise HTTPException(400, "Unsupported DEX")

@router.get("/vaults/{dex}", response_model=VaultList)
def list_vaults(dex: str):
    d = vault_repo.list_vaults(dex)
    rows = []
    for alias, v in d.get("vaults", {}).items():
        rows.append(VaultRow(alias=alias, dex=dex, **v))
    return {"active": d.get("active"), "vaults": rows}

@router.post("/vaults/{dex}/add")
def add_vault(dex: str, req: AddVaultRequest):
    vault_repo.ensure_dirs(dex)
    row = {"address": req.address, "pool": req.pool, "nfpm": req.nfpm, "rpc_url": req.rpc_url}
    vault_repo.add_vault(dex, req.alias, row)
    
    state_repo.ensure_state_initialized(
        dex, req.alias,
        vault_address=req.address,
        nfpm=req.nfpm,
        pool=req.pool
    )
    state_repo.append_history(dex, req.alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "registry_add",
        "vault": req.address,
        "pool": req.pool,
        "nfpm": req.nfpm,
        "tx": None
    })
    
    return {"ok": True}

@router.post("/vaults/{dex}/set-pool")
def set_pool(dex: str, req: SetPoolRequest):
    vault_repo.set_pool(dex, req.alias, req.pool)
    
    state_repo.ensure_state_initialized(dex, req.alias)
    state_repo.update_state(dex, req.alias, {"pool": req.pool})
    state_repo.append_history(dex, req.alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "set_pool",
        "pool": req.pool,
        "tx": None
    })
    return {"ok": True}

@router.get("/vaults/{dex}/{alias}/status", response_model=StatusResponse)
def status(dex: str, alias: str):
    v = vault_repo.get_vault(dex, alias)
    if not v:
        raise HTTPException(404, "Unknown alias")
    if not v.get("pool"):
        raise HTTPException(400, "Vault has no pool set")
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    # nova validação (somente aerodrome)
    if dex == "aerodrome":
        try:
            ad.assert_is_pool()
        except Exception as e:
            raise HTTPException(400, f"Invalid Slipstream pool address: {e}")

    st = compute_status(ad, dex, alias)   # note: compute_status já aceita qualquer DexAdapter
    return {
        "alias": alias,
        "vault": v["address"],
        "pool": v.get("pool"),
        **st
    }

@router.post("/vaults/{dex}/{alias}/open")
def open_position(dex: str, alias: str, req: OpenRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias)
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    
    fn = ad.fn_open(req.lower, req.upper)
    txh = TxService(v.get("rpc_url")).send(fn)
    
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "open",
        "lower": req.lower,
        "upper": req.upper,
        "tx": txh
    })
    
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/rebalance")
def rebalance_caps(dex: str, alias: str, req: RebalanceRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias)
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    meta = ad.pool_meta()
    # convert human caps -> raw if provided
    cap0_raw = cap1_raw = None
    if req.cap0 is not None:
        cap0_raw = int(float(req.cap0) * (10 ** int(meta["dec0"])))
    if req.cap1 is not None:
        cap1_raw = int(float(req.cap1) * (10 ** int(meta["dec1"])))

    fn = ad.fn_rebalance_caps(req.lower, req.upper, cap0_raw, cap1_raw)
    txh = TxService(v.get("rpc_url")).send(fn)
    
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "rebalance_caps",
        "lower": req.lower,
        "upper": req.upper,
        "cap0": req.cap0,
        "cap1": req.cap1,
        "tx": txh
    })
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/withdraw")
def withdraw(dex: str, alias: str, req: WithdrawRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")
    
    state_repo.ensure_state_initialized(dex, alias)
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    
    fn = ad.fn_exit() if req.mode == "pool" else ad.fn_exit_withdraw()
    txh = TxService(v.get("rpc_url")).send(fn)
    
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": ("exit" if req.mode == "pool" else "exit_withdraw"),
        "tx": txh
    })
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/collect")
def collect(dex: str, alias: str, _req: CollectRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")
    
    state_repo.ensure_state_initialized(dex, alias)
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    snap = compute_status(ad, alias)
    
    pre_fees0_raw = int(snap.get("fees", {}).get("uncollected_token0_raw", 0))
    pre_fees1_raw = int(snap.get("fees", {}).get("uncollected_token1_raw", 0))
    usdc_per_eth = float(snap.get("prices", {}).get("current", {}).get("p_t0_t1", 0.0))  # USDC/ETH
    meta = ad.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    
    pre_fees0 = pre_fees0_raw / (10 ** dec0)
    pre_fees1 = pre_fees1_raw / (10 ** dec1)
    pre_fees_usd = pre_fees0 + pre_fees1 * usdc_per_eth
    
    fn = ad.fn_collect()
    txh = TxService(v.get("rpc_url")).send(fn)
    
    state_repo.add_collected_fees_snapshot(
        dex, alias,
        fees0_raw=pre_fees0_raw,
        fees1_raw=pre_fees1_raw,
        fees_usd_est=float(pre_fees_usd)
    )

    state_repo.append_history(dex, alias, "collect_history", {
        "ts": datetime.utcnow().isoformat(),
        "fees0_raw": pre_fees0_raw,
        "fees1_raw": pre_fees1_raw,
        "fees_usd_est": float(pre_fees_usd),
        "tx": txh
    })
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "collect",
        "tx": txh
    })
    
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/deposit")
def deposit(dex: str, alias: str, req: DepositRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")
    
    state_repo.ensure_state_initialized(dex, alias)
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    tok = Web3.to_checksum_address(req.token)
    dec = ad.erc20(tok).functions.decimals().call()
    amount_raw = int(float(req.amount) * (10 ** int(dec)))
    
    fn = ad.fn_deposit_erc20(tok, amount_raw)
    txh = TxService(v.get("rpc_url")).send(fn)
    
    state_repo.append_history(dex, alias, "deposit_history", {
        "ts": datetime.utcnow().isoformat(),
        "token": tok,
        "amount_human": float(req.amount),
        "amount_raw": int(amount_raw),
        "tx": txh
    })
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "deposit",
        "token": tok,
        "amount_human": float(req.amount),
        "tx": txh
    })
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/baseline")
def baseline(dex: str, alias: str, req: BaselineRequest):
    state_repo.ensure_state_initialized(dex, alias)
    st = state_repo.load_state(dex, alias)
    
    if req.action == "set":
        # recompute USD using status to keep one source of truth
        v = vault_repo.get_vault(dex, alias)
        if not v or not v.get("pool"):
            raise HTTPException(400, "Vault has no pool set")
        ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
        s = compute_status(ad, dex, alias)
        baseline_usd = float(s["usd_panel"]["usd_value"])
        st["vault_initial_usd"] = baseline_usd
        st["baseline_set_ts"] = datetime.utcnow().isoformat()
        state_repo.save_state(dex, alias, st)
        
        state_repo.append_history(dex, alias, "exec_history", {
            "ts": datetime.utcnow().isoformat(),
            "mode": "baseline_set",
            "baseline_usd": baseline_usd,
            "tx": None
        })
        
        return {"ok": True, "baseline_usd": st["vault_initial_usd"]}
    # show
    return {"baseline_usd": float(st.get("vault_initial_usd", 0.0) or 0.0)}

@router.post("/vaults/{dex}/deploy")
def deploy_vault(dex: str, req: DeployVaultRequest):
    """
    Two modes:
      A) Factory mode (preferred): set VAULT_FACTORY_ADDRESS in .env, we call factory.create(nfpm).
      B) Artifact mode: load ABI+bytecode from contracts/out/SingleUserVault.sol/SingleUserVault.json and deploy.
         - Assumes constructor args: (nfpm) by default.
         - You can override by supplying ?args=["0xNFPM","0xOwner"] via query/body in a custom model,
           or adapt below if your constructor differs.
    After deployment, optionally set pool in registry and select active.
    """
    s = get_settings()
    rpc = req.rpc_url or s.RPC_URL_DEFAULT
    txs = TxService(rpc)

    factory_addr = Web3.to_checksum_address(os.environ.get("VAULT_FACTORY_ADDRESS", "")) if os.environ.get("VAULT_FACTORY_ADDRESS") else None

    # --- MODE A: Factory (if present) ---
    if factory_addr:
        # Example minimal factory ABI:
        factory_abi = json.loads(os.environ.get("VAULT_FACTORY_ABI_JSON", "[]"))
        if not factory_abi:
            # fallback minimal ABI signature:
            factory_abi = [
                {"name":"create","type":"function","stateMutability":"nonpayable",
                 "inputs":[{"name":"nfpm","type":"address"}],"outputs":[{"name":"vault","type":"address"}]},
                {"name":"setPoolOnce","type":"function","stateMutability":"nonpayable",
                 "inputs":[{"name":"vault","type":"address"},{"name":"pool","type":"address"}],"outputs":[]}
            ]
        w3 = txs.w3
        factory = w3.eth.contract(address=factory_addr, abi=factory_abi)
        # create vault
        fn = factory.functions.create(Web3.to_checksum_address(req.nfpm))
        txh = txs.send(fn, wait=True)

        # try to get deployed address from logs/return (depends on factory)
        # if factory returns address (staticcall) we need logs; for simplicity, require UI to pass it or factory to emit event.
        # As a practical approach, add an event VaultCreated(address vault) and parse here.
        # For now, ask user to provide the new address OR have factory expose a view to list last.
        # We'll try a naive approach: call(view) lastCreated() if abi has it.
        vault_addr = None
        try:
            if hasattr(factory.functions, "lastCreated"):
                vault_addr = factory.functions.lastCreated().call()
        except Exception:
            pass
        if not vault_addr:
            raise HTTPException(500, "Deployed, but could not resolve vault address from factory. Add an event/view or return value.")

        # setPoolOnce if provided
        if req.pool:
            try:
                fn_set = factory.functions.setPoolOnce(Web3.to_checksum_address(vault_addr), Web3.to_checksum_address(req.pool))
                txs.send(fn_set, wait=True)
            except Exception:
                # ignore if factory doesn't support this helper; user can set via a script if needed
                pass

        # registry
        vault_repo.add_vault(dex, req.alias, {
            "address": vault_addr, "pool": req.pool, "nfpm": req.nfpm, "rpc_url": req.rpc_url
        })
        state_repo.ensure_state_initialized(dex, req.alias, vault_address=vault_addr, nfpm=req.nfpm, pool=req.pool)
        vault_repo.set_active(dex, req.alias)
        
        state_repo.append_history(dex, req.alias, "exec_history", {
            "ts": datetime.utcnow().isoformat(),
            "mode": "deploy_vault",
            "vault": vault_addr,
            "pool": req.pool,
            "nfpm": req.nfpm,
            "tx": txh
        })
        
        return {"tx": txh, "address": vault_addr, "alias": req.alias, "dex": dex}

    # --- MODE B: Artifact deploy ---
    # Foundry artifact default path
    artifact_path = Path("contracts/out/SingleUserVault.sol/SingleUserVault.json")
    if not artifact_path.exists():
        raise HTTPException(501, "Artifact not found. Provide VAULT_FACTORY or ship contracts/out/SingleUserVault.sol/SingleUserVault.json")

    art = json.loads(artifact_path.read_text(encoding="utf-8"))
    abi = art.get("abi")
    
    bytecode = art.get("bytecode")
    if isinstance(bytecode, dict):
        bytecode = bytecode.get("object")

    if not bytecode:
        # tenta deployedBytecode também
        bytecode = art.get("deployedBytecode")
        if isinstance(bytecode, dict):
            bytecode = bytecode.get("object")

    if not abi or not bytecode:
        raise HTTPException(500, "Invalid artifact (missing abi/bytecode)")

    # constructor args — default assumes (address nfpm). Adjust if yours differs.
    ctor_args = [Web3.to_checksum_address(req.nfpm)]

    res = txs.deploy(abi=abi, bytecode=bytecode, ctor_args=ctor_args, wait=True)
    vault_addr = res["address"]

    # Optionally call setPoolOnce on the new vault if available
    if req.pool:
        w3 = txs.w3
        vault = w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=abi)
        try:
            if hasattr(vault.functions, "setPoolOnce"):
                txs.send(vault.functions.setPoolOnce(Web3.to_checksum_address(req.pool)), wait=True)
        except Exception:
            # ignore if your vault uses another initializer; user can set via another route/tx
            pass

    # registry
    vault_repo.add_vault(dex, req.alias, {
        "address": vault_addr, "pool": req.pool, "nfpm": req.nfpm, "rpc_url": req.rpc_url
    })
    state_repo.ensure_state_initialized(dex, req.alias, vault_address=vault_addr, nfpm=req.nfpm, pool=req.pool)
    vault_repo.set_active(dex, req.alias)

    state_repo.append_history(dex, req.alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "deploy_vault",
        "vault": vault_addr,
        "pool": req.pool,
        "nfpm": req.nfpm,
        "tx": res["tx"]
    })
    return {"tx": res["tx"], "address": vault_addr, "alias": req.alias, "dex": dex}

@router.post("/vaults/{dex}/{alias}/stake")
def stake_nft(dex: str, alias: str, req: StakeRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    # opcional: deixar por config; ou se salvou no registry, passe via extra:
    # ad.extra = {"voter": v.get("voter")}  # se você salvar no vault_repo

    token_id = int(req.token_id or ad.vault.functions.positionTokenId().call())
    if token_id == 0:
        raise HTTPException(400, "No active position tokenId")

    txh = TxService(v.get("rpc_url")).send(ad.fn_stake_nft(token_id))
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "stake_gauge",
        "tokenId": token_id,
        "tx": txh
    })
    return {"tx": txh, "tokenId": token_id}


@router.post("/vaults/{dex}/{alias}/unstake")
def unstake_nft(dex: str, alias: str, req: UnstakeRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    token_id = int(req.token_id or ad.vault.functions.positionTokenId().call())
    if token_id == 0:
        raise HTTPException(400, "No active position tokenId")

    txh = TxService(v.get("rpc_url")).send(ad.fn_unstake_nft(token_id))
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "unstake_gauge",
        "tokenId": token_id,
        "tx": txh
    })
    return {"tx": txh, "tokenId": token_id}

@router.post("/vaults/{dex}/{alias}/claim")
def claim_rewards(dex: str, alias: str, req: ClaimRewardsRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    if req.mode == "account":
        if not req.account:
            raise HTTPException(400, "account is required when mode='account'")
        fn = ad.fn_claim_rewards_by_account(req.account)
    else:
        # default: via tokenId
        token_id = int(req.token_id or ad.vault.functions.positionTokenId().call())
        if token_id == 0:
            raise HTTPException(400, "No active position tokenId")
        fn = ad.fn_claim_rewards_by_token(token_id)

    txh = TxService(v.get("rpc_url")).send(fn)
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "claim_rewards",
        "by": req.mode,
        "tokenId": req.token_id,
        "account": req.account,
        "tx": txh
    })
    return {"tx": txh, "mode": req.mode}
