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
    DepositRequest, CollectRequest, BaselineRequest, StatusResponse,
    PricesBlock, PricesPanel, UsdPanelModel,
    HoldingsSide, HoldingsMeta, HoldingsBlock,
    FeesUncollected, StatusCore
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

    core = compute_status(ad, dex, alias)  # StatusCore
    return StatusResponse(
        alias=alias,
        vault=v["address"],
        pool=v.get("pool"),
        **core.model_dump()
    )

@router.post("/vaults/{dex}/{alias}/open")
def open_position(dex: str, alias: str, req: OpenRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    
    cons = ad.vault_constraints()
    b0, b1, meta = ad.vault_idle_balances()
    width = int(req.lower) - int(req.upper) if int(req.lower) > int(req.upper) else int(req.upper) - int(req.lower)

    # 1) owner
    from_addr = TxService(v.get("rpc_url")).sender_address()
    if cons.get("owner") and from_addr and cons["owner"].lower() != from_addr.lower():
        raise HTTPException(400, f"Sender is not vault owner. owner={cons['owner']} sender={from_addr}")

    # 2) twap/cooldown
    if cons.get("twapOk") is False:
        raise HTTPException(400, "TWAP guard not satisfied (twapOk=false).")
    if cons.get("minCooldown") and cons.get("lastRebalance"):
        import time
        if time.time() < cons["lastRebalance"] + cons["minCooldown"]:
            raise HTTPException(400, "Cooldown not finished yet (minCooldown).")

    # 3) width vs spacing/min/max
    spacing = cons.get("tickSpacing") or meta["spacing"]
    if req.lower % spacing != 0 or req.upper % spacing != 0:
        raise HTTPException(400, f"Ticks must be multiples of spacing={spacing}.")
    if cons.get("minWidth") and width < cons["minWidth"]:
        raise HTTPException(400, f"Width too small: {width} < minWidth={cons['minWidth']}.")
    if cons.get("maxWidth") and width > cons["maxWidth"]:
        raise HTTPException(400, f"Width too large: {width} > maxWidth={cons['maxWidth']}.")

    # 4) saldos
    if b0 == 0 and b1 == 0:
        raise HTTPException(400, "Vault has no idle balances to mint liquidity (both token balances are zero).")


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

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
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
    
    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    txs = TxService(v.get("rpc_url"))
    
    if req.mode == "pool":
        fn = ad.fn_exit()
    else:
        to_addr = txs.sender_address()
        fn = ad.fn_exit_withdraw(to_addr)
        
    txh = txs.send(fn)
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": ("exit" if req.mode == "pool" else "exit_withdraw"),
        "to": txs.sender_address() if req.mode != "pool" else None,
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
    
    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
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
    if req.action == "set":
        # recompute USD using status to keep one source of truth
        v = vault_repo.get_vault(dex, alias)

        state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
        st = state_repo.load_state(dex, alias)
    
        if not v or not v.get("pool"):
            raise HTTPException(400, "Vault has no pool set")
        ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
        s: StatusCore = compute_status(ad, dex, alias)
        baseline_usd = float(s.usd_panel.usd_value)
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
    Deploy flow (artifact mode, default):
      1) Deploy the DEX-specific on-chain Adapter
      2) Deploy SingleUserVaultV2(owner)
      3) vault.setPoolOnce(adapter)
      4) Save registry/state
    Back-compat: if req.version == "v1", keep the old artifact path & constructor.
    """
    s = get_settings()
    rpc = req.rpc_url or s.RPC_URL_DEFAULT
    txs = TxService(rpc)
    w3 = txs.w3

    # -------- owner/source account --------
    owner = Web3.to_checksum_address(req.owner) if req.owner else txs.sender_address()

    # -------- V1 BACK-COMPAT (opcional) --------
    if req.version == "v1":
        # seu bloco V1 atual (SingleUserVault.sol com ctor(nfpm))
        artifact_path = Path("contracts/out/SingleUserVault.sol/SingleUserVault.json")
        if not artifact_path.exists():
            raise HTTPException(501, "V1 artifact not found")
        art = json.loads(artifact_path.read_text())
        abi = art["abi"]; bytecode = art["bytecode"]["object"] if isinstance(art["bytecode"], dict) else art["bytecode"]
        res = txs.deploy(abi=abi, bytecode=bytecode, ctor_args=[Web3.to_checksum_address(req.nfpm)], wait=True)
        vault_addr = res["address"]
        # setPoolOnce(pool) se existir
        vault = w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=abi)
        try:
            if hasattr(vault.functions, "setPoolOnce"):
                txs.send(vault.functions.setPoolOnce(Web3.to_checksum_address(req.pool)), wait=True)
        except Exception:
            pass

        # registry/state
        vault_repo.add_vault(dex, req.alias, {
            "address": vault_addr,
            "adapter": None,
            "pool": req.pool, "nfpm": req.nfpm, "gauge": req.gauge,
            "rpc_url": req.rpc_url, "version": "v1"
        })
        state_repo.ensure_state_initialized(dex, req.alias,
            vault_address=vault_addr, nfpm=req.nfpm, pool=req.pool, gauge=req.gauge, adapter=None)
        vault_repo.set_active(dex, req.alias)
        state_repo.append_history(dex, req.alias, "exec_history", {
            "ts": datetime.utcnow().isoformat(), "mode": "deploy_vault_v1",
            "vault": vault_addr, "pool": req.pool, "nfpm": req.nfpm, "tx": res["tx"]
        })
        return {"tx": res["tx"], "address": vault_addr, "alias": req.alias, "dex": dex, "version": "v1"}

    # -------- V2 (default) --------
    # 1) Deploy adapter conforme DEX
    if dex == "uniswap":
        adapter_art_path = Path("contracts/out/UniV3Adapter.sol/UniV3Adapter.json")
        if not adapter_art_path.exists():
            raise HTTPException(501, "Adapter artifact (Uniswap) not found")
        aart = json.loads(adapter_art_path.read_text())
        aabi = aart["abi"]; abyte = aart["bytecode"]["object"] if isinstance(aart["bytecode"], dict) else aart["bytecode"]
        # ctor(nfpm, pool) OU seu construtor atual (ajuste conforme o seu adapter .sol)
        adapter_res = txs.deploy(
            abi=aabi, bytecode=abyte,
            ctor_args=[Web3.to_checksum_address(req.nfpm), Web3.to_checksum_address(req.pool)],
            wait=True
        )
        adapter_addr = adapter_res["address"]

    elif dex == "aerodrome":
        adapter_art_path = Path("contracts/out/SlipstreamAdapter.sol/SlipstreamAdapter.json")
        if not adapter_art_path.exists():
            raise HTTPException(501, "Adapter artifact (Aerodrome) not found")
        aart = json.loads(adapter_art_path.read_text())
        aabi = aart["abi"]; abyte = aart["bytecode"]["object"] if isinstance(aart["bytecode"], dict) else aart["bytecode"]
        # ctor(nfpm, pool, gauge?) — ajuste exatamente ao seu SlipstreamAdapter.sol
        ctor = [Web3.to_checksum_address(req.nfpm), Web3.to_checksum_address(req.pool)]
        if req.gauge:
            ctor.append(Web3.to_checksum_address(req.gauge))
        adapter_res = txs.deploy(abi=aabi, bytecode=abyte, ctor_args=ctor, wait=True)
        adapter_addr = adapter_res["address"]

    else:
        raise HTTPException(400, "Unsupported dex for V2")

    # 2) Deploy SingleUserVaultV2(owner)
    v2_path = Path("contracts/out/SingleUserVaultV2.sol/SingleUserVaultV2.json")
    if not v2_path.exists():
        raise HTTPException(501, "Vault V2 artifact not found")
    vart = json.loads(v2_path.read_text())
    vabi = vart["abi"]; vbyte = vart["bytecode"]["object"] if isinstance(vart["bytecode"], dict) else vart["bytecode"]

    vres = txs.deploy(abi=vabi, bytecode=vbyte, ctor_args=[owner], wait=True)
    vault_addr = vres["address"]
    vault = w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=vabi)

    # 3) setPoolOnce(adapter)
    try:
        txs.send(vault.functions.setPoolOnce(Web3.to_checksum_address(adapter_addr)), wait=True)
    except Exception as e:
        raise HTTPException(500, f"setPoolOnce failed: {e}")

    # 4) registry/state
    vault_repo.add_vault(dex, req.alias, {
        "address": vault_addr,
        "adapter": adapter_addr,
        "pool": req.pool,
        "nfpm": req.nfpm,
        "gauge": req.gauge,
        "rpc_url": req.rpc_url,
        "version": "v2"
    })
    state_repo.ensure_state_initialized(
        dex, req.alias,
        vault_address=vault_addr,
        nfpm=req.nfpm,
        pool=req.pool,
        gauge=req.gauge
    )
    vault_repo.set_active(dex, req.alias)

    state_repo.append_history(dex, req.alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "deploy_vault_v2",
        "vault": vault_addr,
        "adapter": adapter_addr,
        "pool": req.pool,
        "nfpm": req.nfpm,
        "gauge": req.gauge,
        "tx_adapter": adapter_res["tx"],
        "tx_vault": vres["tx"]
    })

    return {
        "tx_vault": vres["tx"],
        "tx_adapter": adapter_res["tx"],
        "vault": vault_addr,
        "adapter": adapter_addr,
        "alias": req.alias,
        "dex": dex,
        "version": "v2",
        "owner": owner
    }

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
