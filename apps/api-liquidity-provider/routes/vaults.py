from decimal import Decimal
import json
import time
from pathlib import Path
from datetime import datetime
import token
from fastapi import APIRouter, HTTPException, Body
from web3 import Web3

from ..services.exceptions import TransactionRevertedError

from ..routes.utils import snapshot_status, tick_spacing_candidates

from ..domain.swap import SwapExactInRequest, SwapQuoteRequest
from ..config import get_settings
from ..domain.models import (
    DexName, VaultList, VaultRow, AddVaultRequest, SetPoolRequest,
    DeployVaultRequest, OpenRequest, RebalanceRequest, WithdrawRequest,
    DepositRequest, CollectRequest, BaselineRequest, StatusResponse, StatusCore
)
from ..services import state_repo, vault_repo
from ..services.tx_service import TxService
from ..services.chain_reader import USD_SYMBOLS, _value_usd, compute_status, price_to_tick, sqrtPriceX96_to_price_t1_per_t0
from ..adapters.uniswap_v3 import UniswapV3Adapter
from ..adapters.aerodrome import AerodromeAdapter
from ..domain.models import StakeRequest, UnstakeRequest, ClaimRewardsRequest

router = APIRouter(tags=["vaults"])

def _adapter_for(dex: str, pool: str, nfpm: str | None, vault: str, rpc_url: str | None):
    s = get_settings()
    w3 = Web3(Web3.HTTPProvider(rpc_url or s.RPC_URL_DEFAULT))
    if dex == "uniswap":
        return UniswapV3Adapter(w3, pool, nfpm, vault)
    if dex == "aerodrome":
        return AerodromeAdapter(w3, pool, nfpm, vault)  # stub raises NotImplemented
    raise HTTPException(400, "Unsupported DEX")

@router.get("/vaults/{dex}", response_model=VaultList)
def list_vaults(dex: DexName):
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

@router.post("/vaults/{dex}/{alias}/set-pool")
def set_pool(dex: str, alias: str, req: SetPoolRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")
    
    vault_repo.set_pool(dex, alias, req.pool)
    
    state_repo.ensure_state_initialized(dex, req.alias, vault_address=v["address"])
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
    if not v:
        raise HTTPException(404, "Unknown alias")
    if not v.get("pool"):
        raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    cons = ad.vault_constraints()
    meta = ad.pool_meta()
    dec0 = int(meta["dec0"])
    dec1 = int(meta["dec1"])
    spacing = int(meta.get("spacing") or cons.get("tickSpacing") or 0)

    # -------- owner check
    from_addr = TxService(v.get("rpc_url")).sender_address()
    if cons.get("owner") and from_addr and cons["owner"].lower() != from_addr.lower():
        raise HTTPException(
            400,
            f"Sender is not vault owner. owner={cons['owner']} sender={from_addr}"
        )

    # -------- twap / cooldown check
    if cons.get("twapOk") is False:
        raise HTTPException(400, "TWAP guard not satisfied (twapOk=false).")

    if cons.get("minCooldown") and cons.get("lastRebalance"):
        import time
        if time.time() < cons["lastRebalance"] + cons["minCooldown"]:
            raise HTTPException(400, "Cooldown not finished yet (minCooldown).")

    # -------- saldos idle (precisamos ter algo pra abrir)
    bal0_raw, bal1_raw, _vault_meta = ad.vault_idle_balances()
    if bal0_raw == 0 and bal1_raw == 0:
        raise HTTPException(
            400,
            "Vault has no idle balances to mint liquidity (both token balances are zero)."
        )

    # -------- resolver lower_tick / upper_tick
    lower_tick = req.lower_tick
    upper_tick = req.upper_tick

    if lower_tick is None or upper_tick is None:
        # tentar via preço p_t1_t0 (token1 per token0), igual rebalance
        if req.lower_price is None or req.upper_price is None:
            raise HTTPException(
                400,
                "You must provide either (lower_tick and upper_tick) OR (lower_price and upper_price)."
            )
        lower_tick = price_to_tick(float(req.lower_price), dec0, dec1)
        upper_tick = price_to_tick(float(req.upper_price), dec0, dec1)

    # garantir ordem asc (lower < upper)
    if lower_tick > upper_tick:
        tmp = lower_tick
        lower_tick = upper_tick
        upper_tick = tmp

    # alinhar pro múltiplo de spacing
    if spacing:
        if lower_tick % spacing != 0:
            lower_tick = int(round(lower_tick / spacing) * spacing)
        if upper_tick % spacing != 0:
            upper_tick = int(round(upper_tick / spacing) * spacing)

    # validar largura vs minWidth / maxWidth
    width = abs(int(upper_tick) - int(lower_tick))
    if cons.get("minWidth") and width < cons["minWidth"]:
        raise HTTPException(
            400,
            f"Width too small: {width} < minWidth={cons['minWidth']}."
        )
    if cons.get("maxWidth") and width > cons["maxWidth"]:
        raise HTTPException(
            400,
            f"Width too large: {width} > maxWidth={cons['maxWidth']}."
        )

    # -------- snapshot before
    before = snapshot_status(ad, dex, alias)

    # -------- montar tx openInitialPosition(lower, upper)
    # no contrato Solidity: vault.openInitialPosition(int24 lower, int24 upper)
    # no adapter python: ad.fn_open(lower, upper)
    fn = ad.fn_open(int(lower_tick), int(upper_tick))

    txs = TxService(v.get("rpc_url"))
    try:
        send_res = txs.send(
            fn,
            wait=True,
            gas_strategy="buffered"  # default já é "buffered", mas deixei explícito
        )
    except TransactionRevertedError as e:
        # aqui eu devolvo um 500 com payload útil
        # e.status code 500 força o caller a saber que NÃO foi sucesso
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or vault guard (cooldown/twap/allowance).",
            }
        )
        
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}
    gas_limit_used = send_res.get("gas_limit_used")
    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)

    gas_eth = None
    gas_usd = None
    if gas_used and eff_price_wei:
        # gas em ETH
        gas_eth = float(
            (Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18)
        )

        # tentar precificar ETH->USD (igual rebalance_swap logic)
        meta2 = ad.pool_meta()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sym0b, sym1b = str(meta2["sym0"]).upper(), str(meta2["sym1"]).upper()
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)

        if sym1b in USD_SYMBOLS and sym0b in {"WETH", "ETH"}:
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH", "ETH"}:
            gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0 / p_t1_t0b)

    # -------- snapshot after
    after = snapshot_status(ad, dex, alias)

    # -------- histórico
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "open_initial",
        "lower_tick": int(lower_tick),
        "upper_tick": int(upper_tick),
        "lower_price": float(req.lower_price) if req.lower_price is not None else None,
        "upper_price": float(req.upper_price) if req.upper_price is not None else None,
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })

    return {
        "tx": tx_hash,
        "range_used": {
            "lower_tick": int(lower_tick),
            "upper_tick": int(upper_tick),
            "width_ticks": width,
            "spacing": spacing,
            "lower_price": float(req.lower_price) if req.lower_price is not None else None,
            "upper_price": float(req.upper_price) if req.upper_price is not None else None,
        },
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "effective_gas_price_gwei": (float(eff_price_wei)/1e9 if eff_price_wei else None),
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "before": before,
        "after": after,
    }


@router.post("/vaults/{dex}/{alias}/rebalance")
def rebalance_caps(dex: str, alias: str, req: RebalanceRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    
    cons = ad.vault_constraints()
    meta = ad.pool_meta()
    dec0 = int(meta["dec0"])
    dec1 = int(meta["dec1"])
    spacing = int(meta["spacing"])
    
    from_addr = TxService(v.get("rpc_url")).sender_address()
    if cons.get("owner") and from_addr and cons["owner"].lower() != from_addr.lower():
        raise HTTPException(
            400,
            f"Sender is not vault owner. owner={cons['owner']} sender={from_addr}"
        )
    
    if cons.get("twapOk") is False:
        raise HTTPException(400, "TWAP guard not satisfied (twapOk=false).")
    if cons.get("minCooldown") and cons.get("lastRebalance"):
        if time.time() < cons["lastRebalance"] + cons["minCooldown"]:
            raise HTTPException(400, "Cooldown not finished yet (minCooldown).")
     
    # ---- resolver LOWER / UPPER em ticks
    lower_tick = req.lower_tick
    upper_tick = req.upper_tick

    if lower_tick is None or upper_tick is None:
        # tentar via preço
        if req.lower_price is None or req.upper_price is None:
            raise HTTPException(
                400,
                "You must provide either (lower_tick and upper_tick) OR (lower_price and upper_price)."
            )
        lower_tick = price_to_tick(float(req.lower_price), dec0, dec1)
        upper_tick = price_to_tick(float(req.upper_price), dec0, dec1)

    # garantir ordem (lower < upper)
    if lower_tick > upper_tick:
        # se o user mandou invertido, a gente troca
        tmp = lower_tick
        lower_tick = upper_tick
        upper_tick = tmp

    # alinhar para múltiplo de spacing
    if lower_tick % spacing != 0:
        # arredonda pro múltiplo mais próximo
        lower_tick = int(round(lower_tick / spacing) * spacing)
    if upper_tick % spacing != 0:
        upper_tick = int(round(upper_tick / spacing) * spacing)

    # width sanity (igual /open faz)
    width = abs(int(upper_tick) - int(lower_tick))
    if cons.get("minWidth") and width < cons["minWidth"]:
        raise HTTPException(
            400,
            f"Width too small: {width} < minWidth={cons['minWidth']}."
        )
    if cons.get("maxWidth") and width > cons["maxWidth"]:
        raise HTTPException(
            400,
            f"Width too large: {width} > maxWidth={cons['maxWidth']}."
        )

    # ---- converter caps humanos -> raw
    cap0_raw = cap1_raw = None
    if req.cap0 is not None:
        cap0_raw = int(float(req.cap0) * (10 ** dec0))
    if req.cap1 is not None:
        cap1_raw = int(float(req.cap1) * (10 ** dec1))

    before = snapshot_status(ad, dex, alias)
    
    # ---- montar tx rebalanceWithCaps(lower_tick, upper_tick, cap0_raw, cap1_raw)
    fn = ad.fn_rebalance_caps(lower_tick, upper_tick, cap0_raw, cap1_raw)
    txs = TxService(v.get("rpc_url"))
    try:
        send_res = txs.send(fn, wait=True, gas_strategy="buffered")
    except TransactionRevertedError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or slippage/guard.",
            }
        )
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}

    gas_limit_used = send_res.get("gas_limit_used")
    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)
    gas_eth = gas_usd = None
    if gas_used and eff_price_wei:
        gas_eth = float((Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18))
        meta2 = ad.pool_meta()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sym0b, sym1b = str(meta2["sym0"]).upper(), str(meta2["sym1"]).upper()
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)
        if sym1b in USD_SYMBOLS and sym0b in {"WETH","ETH"}:
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH","ETH"}:
            gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0/p_t1_t0b)

    after = snapshot_status(ad, dex, alias)

    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "rebalance_caps",
        "lower_tick": lower_tick,
        "upper_tick": upper_tick,
        "lower_price": float(req.lower_price) if req.lower_price is not None else None,
        "upper_price": float(req.upper_price) if req.upper_price is not None else None,
        "cap0": req.cap0,
        "cap1": req.cap1,
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })

    return {
        "tx": tx_hash,
        "range_used": {
            "lower_tick": lower_tick,
            "upper_tick": upper_tick,
            "width_ticks": width,
            "spacing": spacing,
            "lower_price": float(req.lower_price) if req.lower_price is not None else None,
            "upper_price": float(req.upper_price) if req.upper_price is not None else None,
        },
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "before": before,
        "after": after,
    }

@router.post("/vaults/{dex}/{alias}/withdraw")
def withdraw(dex: str, alias: str, req: WithdrawRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")
    
    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    txs = TxService(v.get("rpc_url"))
    
    before = snapshot_status(ad, dex, alias)
    
    if req.mode == "pool":
        fn = ad.fn_exit()
    else:
        to_addr = txs.sender_address()
        fn = ad.fn_exit_withdraw(to_addr)
        
    try:
        send_res = txs.send(fn, wait=True, gas_strategy="buffered")
    except TransactionRevertedError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or slippage/guard.",
            }
        )
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}

    gas_limit_used = send_res.get("gas_limit_used")
    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)
    gas_eth = gas_usd = None
    if gas_used and eff_price_wei:
        gas_eth = float((Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18))
        # tentar converter pra USD com mesma heurística
        meta2 = ad.pool_meta()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sym0b, sym1b = str(meta2["sym0"]).upper(), str(meta2["sym1"]).upper()
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)
        if sym1b in USD_SYMBOLS and sym0b in {"WETH","ETH"}:
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH","ETH"}:
            gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0/p_t1_t0b)

    after = snapshot_status(ad, dex, alias)

    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": ("exit_pool" if req.mode == "pool" else "exit_all"),
        "to": txs.sender_address() if req.mode != "pool" else None,
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })

    return {
        "tx": tx_hash,
        "mode": ("exit" if req.mode == "pool" else "exit_withdraw"),
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "before": before,
        "after": after,
    }

@router.post("/vaults/{dex}/{alias}/collect")
def collect(dex: str, alias: str, _req: CollectRequest):
    """
    Collect unclaimed fees to the vault.

    How it works:
    - We read the *current* tokenId from the adapter's `vault_state()`.
    - If tokenId == 0 => there's no active position to collect (HTTP 400).
    - We preview fees using a `callStatic` collect on NFPM to get RAW amounts.
    - We convert fee preview to human units and to USD/USDC using the same
      rule as status:
        * if token1 is USD-like: USD = f0 * (t1/t0) + f1
        * elif token0 is USD-like: USD = f1 * (t0/t1) + f0
        * else: fallback USD ~= f0 * (t1/t0) + f1
    - Then we call the vault's `collectToVault()` via adapter.fn_collect().
    - We persist a snapshot of collected fees (raw + estimated USD) and history.

    Notes:
    - `compute_status()` returns a Pydantic model (StatusCore), not a dict.
    - We use `adapter.call_static_collect(tokenId, vault)` for a precise preview.
    """
    # --- basic guards
    v = vault_repo.get_vault(dex, alias)
    if not v:
        raise HTTPException(404, "Unknown alias")
    if not v.get("pool"):
        raise HTTPException(400, "Vault has no pool set")

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    # snapshot BEFORE
    before = snapshot_status(ad, dex, alias)
    
    # --- fetch meta + live status (for price conversion)
    snap: StatusCore = compute_status(ad, dex, alias)  # Pydantic model
    meta = ad.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = meta["sym0"], meta["sym1"]

    # prices for conversion
    p_t1_t0 = float(snap.prices.current.p_t1_t0)  # token1 per token0
    p_t0_t1 = float(snap.prices.current.p_t0_t1)  # token0 per token1

    # --- get current tokenId to know if there is a position and preview fees
    vstate = ad.vault_state()
    token_id = int(vstate.get("tokenId", 0) or 0)
    if token_id == 0:
        # nothing to collect from NFPM (positionless)
        raise HTTPException(400, "No active position to collect fees from.")

    # callStatic collect to preview raw fees
    fees0_raw, fees1_raw = ad.call_static_collect(token_id, ad.vault.address)

    # human amounts
    pre_fees0 = float(fees0_raw) / (10 ** dec0)
    pre_fees1 = float(fees1_raw) / (10 ** dec1)

    # --- USD/USDC conversion rule (same as compute_status)
    def _is_usd_symbol(sym: str) -> bool:
        try:
            return sym.upper() in {"USDC", "USDBC", "USDCE", "USDT", "DAI", "USDD", "USDP", "BUSD"}
        except Exception:
            return False

    if _is_usd_symbol(sym1):
        pre_fees_usd = pre_fees0 * p_t1_t0 + pre_fees1
    elif _is_usd_symbol(sym0):
        pre_fees_usd = pre_fees1 * p_t0_t1 + pre_fees0
    else:
        # fallback: treat token1 as quote
        pre_fees_usd = pre_fees0 * p_t1_t0 + pre_fees1

    # execute tx (collectToVault)
    txs = TxService(v.get("rpc_url"))
    fn = ad.fn_collect()
    try:
        send_res = txs.send(fn, wait=True, gas_strategy="buffered")
    except TransactionRevertedError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or slippage/guard.",
            }
        )
        
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}

    gas_limit_used = send_res.get("gas_limit_used")

    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)

    gas_eth = None
    gas_usd = None
    if gas_used and eff_price_wei:
        gas_eth = float((Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18))
        # tentar converter ETH→USD usando mesma heurística que swap usa
        meta2 = ad.pool_meta()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sym0b, sym1b = str(meta2["sym0"]).upper(), str(meta2["sym1"]).upper()
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)
        # se par é [ETH,USDC] ou [USDC,ETH] a gente estima gas_usd
        if sym1b in USD_SYMBOLS and sym0b in {"WETH","ETH"}:
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH","ETH"}:
            gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0/p_t1_t0b)
            
    # --- persist snapshot and history (using *preview* values)
    state_repo.add_collected_fees_snapshot(
        dex, alias,
        fees0_raw=int(fees0_raw),
        fees1_raw=int(fees1_raw),
        fees_usd_est=float(pre_fees_usd)
    )
    state_repo.append_history(dex, alias, "collect_history", {
        "ts": datetime.utcnow().isoformat(),
        "fees0_raw": int(fees0_raw),
        "fees1_raw": int(fees1_raw),
        "fees_usd_est": float(pre_fees_usd),
        "tx": tx_hash
    })
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "collect",
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })

    after = snapshot_status(ad, dex, alias)

    return {
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "collected_preview": {
            "token0": pre_fees0,
            "token1": pre_fees1,
            "usd_est": float(pre_fees_usd),
        },
        "before": before,
        "after": after,
    }

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
    
    before = snapshot_status(ad, dex, alias)

    txs = TxService(v.get("rpc_url"))
    fn = ad.fn_deposit_erc20(tok, amount_raw)
    try:
        send_res = txs.send(fn, wait=True, gas_strategy="buffered")
    except TransactionRevertedError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or slippage/guard.",
            }
        )
        
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}

    gas_limit_used = send_res.get("gas_limit_used")

    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)
    gas_eth = gas_usd = None
    if gas_used and eff_price_wei:
        gas_eth = float((Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18))
        meta2 = ad.pool_meta()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sym0b, sym1b = str(meta2["sym0"]).upper(), str(meta2["sym1"]).upper()
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)
        if sym1b in USD_SYMBOLS and sym0b in {"WETH","ETH"}:
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH","ETH"}:
            gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0/p_t1_t0b)

    after = snapshot_status(ad, dex, alias)

    state_repo.append_history(dex, alias, "deposit_history", {
        "ts": datetime.utcnow().isoformat(),
        "token": tok,
        "amount_human": float(req.amount),
        "amount_raw": int(amount_raw),
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "deposit",
        "token": tok,
        "amount_human": float(req.amount),
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
    })

    return {
        "tx": tx_hash,
        "token": tok,
        "amount_human": float(req.amount),
        "amount_raw": int(amount_raw),
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "before": before,
        "after": after,
    }

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
    st = state_repo.load_state(dex, alias)
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
        ctor = [Web3.to_checksum_address(req.pool), Web3.to_checksum_address(req.nfpm)]
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

    # chama o fluxo correto via Vault -> Adapter -> Gauge
    txh = TxService(v.get("rpc_url")).send(ad.fn_stake_nft())
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "stake_gauge",
        "tx": txh
    })
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/unstake")
def unstake_nft(dex: str, alias: str, req: UnstakeRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    txh = TxService(v.get("rpc_url")).send(ad.fn_unstake_nft())
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "unstake_gauge",
        "tx": txh
    })
    return {"tx": txh}

@router.post("/vaults/{dex}/{alias}/claim")
def claim_rewards(dex: str, alias: str, req: ClaimRewardsRequest):
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")
    if not v.get("pool"): raise HTTPException(400, "Vault has no pool set")

    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    # sempre via Vault/Adapter; o Adapter tenta ambas variantes de getReward
    txh = TxService(v.get("rpc_url")).send(ad.fn_claim_rewards())
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "claim_rewards",
        "by": "adapter",
        "tx": txh
    })
    return {"tx": txh, "mode": "adapter"}

@router.post("/vaults/uniswap/{alias}/swap/quote")
def swap_quote(alias: str, req: SwapQuoteRequest):
    dex = "uniswap"
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")

    s = get_settings()
    if not s.UNI_V3_QUOTER:
        raise HTTPException(500, "UNI_V3_QUOTER not configured")

    ad  = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    
    dec_in = int(ad.erc20(req.token_in).functions.decimals().call())
    dec_out = int(ad.erc20(req.token_out).functions.decimals().call())
    amount_in_raw = int(float(req.amount_in) * (10 ** dec_in))
    
    if amount_in_raw <= 0:
        raise HTTPException(400, "amount_in must be > 0")

    quoter = ad.quoter(s.UNI_V3_QUOTER)
    
    fee_candidates = [500, 3000, 10000] if not req.fee else [int(req.fee)]
    best = None
        
    for fee in fee_candidates:
        params = {
            "tokenIn": req.token_in,
            "tokenOut": req.token_out,
            "amountIn": amount_in_raw,
            "fee": fee,
            "sqrtPriceLimitX96": int(req.sqrt_price_limit_x96 or 0),
        }
        try:
            amount_out_raw, sqrt_after, ticks_crossed, gas_est = quoter.functions.quoteExactInputSingle(params).call()
            if amount_out_raw > 0:
                if not best or amount_out_raw > best["amount_out_raw"]:
                    best = dict(
                        fee=fee,
                        amount_out_raw=amount_out_raw,
                        sqrt_after=sqrt_after,
                        ticks_crossed=ticks_crossed,
                        gas_est=gas_est,
                    )
        except:
            continue
    
    if not best:
        raise HTTPException(400, "No route available (all fee tiers reverted)")
    
    # --- Gas to ETH & USD (same logic as before)
    gas_price_wei = int(ad.w3.eth.gas_price)
    gas_eth = float((Decimal(best["gas_est"]) * Decimal(gas_price_wei)) / Decimal(10**18))

    meta = ad.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = str(meta["sym0"]).upper(), str(meta["sym1"]).upper()
    t0, t1 = meta["token0"], meta["token1"]

    sqrtP, _ = ad.slot0()
    p_t1_t0 = sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)

    def _is_usdc(s): return s in USD_SYMBOLS
    def _is_eth(s): return s in {"WETH","ETH"}

    usdc_per_eth = None
    if _is_usdc(sym1) and _is_eth(sym0): usdc_per_eth = p_t1_t0
    elif _is_usdc(sym0) and _is_eth(sym1): usdc_per_eth = (0 if p_t1_t0==0 else 1/p_t1_t0)

    gas_usd = (gas_eth * float(usdc_per_eth)) if usdc_per_eth else None

    # --- Valoração pós swap (valor percebido "final")
    amount_out_human = float(best["amount_out_raw"]) / (10 ** dec_out)
    value_at_sqrt_after_usd = _value_usd(
        0, amount_out_human, p_t1_t0, 1/p_t1_t0, sym0, sym1, t0, t1
    )

    return {
        "best_fee": best["fee"],
        "amount_in_raw": amount_in_raw,
        "amount_out_raw": best["amount_out_raw"],
        "amount_in": float(req.amount_in),
        "amount_out": amount_out_human,
        "sqrtPriceX96_after": int(best["sqrt_after"]),
        "initialized_ticks_crossed": int(best["ticks_crossed"]),
        "gas_estimate": int(best["gas_est"]),
        "gas_price_wei": gas_price_wei,
        "gas_price_gwei": float(Decimal(gas_price_wei) / Decimal(10**9)),
        "gas_eth": float(gas_eth),
        "gas_usd": float(gas_usd) if gas_usd else None,
        "value_at_sqrt_after_usd": float(value_at_sqrt_after_usd),
    }

@router.post("/vaults/uniswap/{alias}/swap/exact-in")
def swap_exact_in(alias: str, req: SwapExactInRequest):
    dex = "uniswap"
    v = vault_repo.get_vault(dex, alias)
    if not v: raise HTTPException(404, "Unknown alias")

    s = get_settings()
    if not s.UNI_V3_ROUTER or not s.UNI_V3_QUOTER:
        raise HTTPException(500, "UNI_V3_ROUTER/UNI_V3_QUOTER not configured")

    state_repo.ensure_state_initialized(dex, alias, vault_address=v["address"])
    ad = _adapter_for(dex, v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    # decimals
    dec_in = int(ad.erc20(req.token_in).functions.decimals().call())
    dec_out = int(ad.erc20(req.token_out).functions.decimals().call())
    
    # --- resolver amount_in_raw a partir de amount_in (token) OU amount_in_usd
    def _is_usdc(sym: str) -> bool: return sym.upper() in USD_SYMBOLS
    def _is_eth(sym: str)  -> bool: return sym.upper() in {"WETH","ETH"}

    amount_in_raw = None
    resolved_mode = None  # "token" ou "usd"
    
    if req.amount_in is not None:
        amount_in_raw = int(float(req.amount_in) * (10 ** dec_in))
        resolved_mode = "token"
    elif req.amount_in_usd is not None:
        # Só convertemos USD -> token quando token_in é WETH/ETH ou USDC
        # Pegamos USDC/ETH do pool do vault (se for USDC↔(W)ETH); caso contrário, 400.
        meta = ad.pool_meta()
        sym0, sym1 = meta["sym0"], meta["sym1"]
        dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
        sqrtP, _ = ad.slot0()
        p_t1_t0 = sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)  # token1/token0

        usdc_per_eth = None
        if _is_usdc(sym1) and _is_eth(sym0):
            usdc_per_eth = p_t1_t0
        elif _is_usdc(sym0) and _is_eth(sym1):
            usdc_per_eth = (0.0 if p_t1_t0 == 0 else 1.0 / p_t1_t0)

        # determinar se token_in é WETH/ETH ou USDC
        in_sym = ad.erc20(req.token_in).functions.symbol().call()
        if _is_eth(in_sym):
            if not usdc_per_eth:
                raise HTTPException(400, "Não foi possível obter USDC/ETH a partir do pool do vault.")
            # USD -> WETH:  amount_eth = USD / (USDC/ETH)
            amount_in_token = float(req.amount_in_usd) / float(usdc_per_eth)
            amount_in_raw = int(amount_in_token * (10 ** dec_in))
            resolved_mode = "usd"
        elif _is_usdc(in_sym):
            # USD -> USDC é 1:1
            amount_in_raw = int(float(req.amount_in_usd) * (10 ** dec_in))
            resolved_mode = "usd"
        else:
            raise HTTPException(400, "amount_in_usd só é suportado quando token_in é WETH/ETH ou USDC.")
    else:
        raise HTTPException(400, "Informe amount_in (token) ou amount_in_usd.")

    if amount_in_raw <= 0:
        raise HTTPException(400, "amount_in deve ser > 0")


    # sanity: vault balance
    bal_in = int(ad.erc20(req.token_in).functions.balanceOf(v["address"]).call())
    if bal_in < amount_in_raw:
        raise HTTPException(400, f"insufficient vault balance: have {bal_in}, need {amount_in_raw}")

    # First call the quote endpoint function to auto-pick fee
    fee = int(req.fee) if req.fee is not None else None
    quote = swap_quote(dex, alias, SwapQuoteRequest(
        alias=alias,
        token_in=req.token_in,
        token_out=req.token_out,
        amount_in=(float(req.amount_in) if resolved_mode=="token" else float(amount_in_raw) / (10 ** dec_in)),
        fee=fee,
        sqrt_price_limit_x96=req.sqrt_price_limit_x96
    ))
    fee_used = int(quote["best_fee"])
    amount_out_raw = int(quote["amount_out_raw"])
    if amount_out_raw <= 0:
        raise HTTPException(400, "quoter returned 0")
    
    # slippage
    bps = max(0, int(req.slippage_bps))
    min_out_raw = amount_out_raw * (10_000 - bps) // 10_000

    fn = ad.fn_vault_swap_exact_in(
        router=s.UNI_V3_ROUTER,
        token_in=req.token_in,
        token_out=req.token_out,
        fee=fee_used,
        amount_in_raw=amount_in_raw,
        min_out_raw=min_out_raw,
        sqrt_price_limit_x96=int(req.sqrt_price_limit_x96 or 0),
    )

    txs = TxService(v.get("rpc_url"))
    txh = txs.send(fn)

    # Real gas spent
    gas_used = eff_price_wei = gas_eth = gas_usd = None
    try:
        rcpt = ad.w3.eth.wait_for_transaction_receipt(txh)
        gas_used = int(rcpt["gasUsed"])
        eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)
        if eff_price_wei and gas_used:
            gas_eth = float((Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18))
            meta2 = ad.pool_meta()
            sym0b, sym1b = meta2["sym0"].upper(), meta2["sym1"].upper()
            dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
            sqrtPb, _ = ad.slot0()
            p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)
            if _is_usdc(sym1b) and _is_eth(sym0b):
                gas_usd = gas_eth * p_t1_t0b
            elif _is_usdc(sym0b) and _is_eth(sym1b):
                gas_usd = gas_eth * (0 if p_t1_t0b == 0 else 1.0 / p_t1_t0b)
    except Exception:
        pass

    # history
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "swap_exact_in",
        "token_in": req.token_in,
        "token_out": req.token_out,
        "resolved_amount_mode": resolved_mode,  # "token" ou "usd"
        "amount_in_raw": amount_in_raw,
        "min_out_raw": min_out_raw,
        "fee_used": fee_used,
        "slippage_bps": bps,
        "tx": txh,
        "gas_used": gas_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "value_at_sqrt_after_usd": quote["value_at_sqrt_after_usd"],
    })

    return {
        "tx": txh,
        "fee_used": fee_used,
        "resolved_amount_mode": resolved_mode,
        "amount_in_raw": amount_in_raw,
        "quoted_out_raw": amount_out_raw,
        "min_out_raw": min_out_raw,
        "gas_used": gas_used,
        "effective_gas_price_wei": eff_price_wei,
        "effective_gas_price_gwei": (float(eff_price_wei)/1e9 if eff_price_wei else None),
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "value_at_sqrt_after_usd": quote["value_at_sqrt_after_usd"],
    }

@router.post("/vaults/aerodrome/{alias}/swap/quote")
def aero_swap_quote(alias: str, req: SwapQuoteRequest):
    v = vault_repo.get_vault("aerodrome", alias)
    if not v: raise HTTPException(404, "Unknown alias (aerodrome)")
    s = get_settings()
    if not s.AERO_QUOTER:
        raise HTTPException(500, "AERO_QUOTER not configured")

    ad = _adapter_for("aerodrome", v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))
    quoter = ad.aerodrome_quoter(s.AERO_QUOTER)

    dec_in  = int(ad.erc20(req.token_in).functions.decimals().call())
    dec_out = int(ad.erc20(req.token_out).functions.decimals().call())
    amount_in_raw = int(float(req.amount_in) * (10 ** dec_in))
    if amount_in_raw <= 0:
        raise HTTPException(400, "amount_in must be > 0")

    # candidatos de tickSpacing (equivalente ao "fee" do Uniswap)
    candidates = [int(req.fee)] if req.fee is not None else tick_spacing_candidates(ad)

    best = None
    for ts in candidates:
        params = {
            "tokenIn": Web3.to_checksum_address(req.token_in),
            "tokenOut": Web3.to_checksum_address(req.token_out),
            "amountIn": int(amount_in_raw),
            "tickSpacing": int(ts),
            "sqrtPriceLimitX96": int(req.sqrt_price_limit_x96 or 0),
        }
        try:
            amount_out_raw, sqrt_after, ticks_crossed, gas_est = quoter.functions.quoteExactInputSingle(params).call()
            if amount_out_raw > 0 and (not best or amount_out_raw > best["amount_out_raw"]):
                best = dict(
                    tick_spacing=ts,
                    amount_out_raw=int(amount_out_raw),
                    sqrt_after=int(sqrt_after),
                    ticks_crossed=int(ticks_crossed),
                    gas_est=int(gas_est),
                )
        except Exception:
            continue

    if not best:
        raise HTTPException(400, "No route available (all tickSpacings reverted)")

    # gas -> ETH/USDC (mesma lógica do Uniswap)
    gas_price_wei = int(ad.w3.eth.gas_price)
    gas_eth = float(Decimal(best["gas_est"]) * Decimal(gas_price_wei) / Decimal(10**18))

    meta = ad.pool_meta()
    dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
    sym0, sym1 = str(meta["sym0"]).upper(), str(meta["sym1"]).upper()
    t0, t1 = meta["token0"], meta["token1"]

    sqrtP, _ = ad.slot0()
    p_t1_t0 = sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)

    def _is_usdc(s): return s in USD_SYMBOLS
    def _is_eth(s):  return s in {"WETH","ETH"}

    usdc_per_eth = None
    if _is_usdc(sym1) and _is_eth(sym0): usdc_per_eth = p_t1_t0
    elif _is_usdc(sym0) and _is_eth(sym1): usdc_per_eth = (0 if p_t1_t0==0 else 1/p_t1_t0)

    gas_usd = (gas_eth * float(usdc_per_eth)) if usdc_per_eth else None

    amount_out_human = float(best["amount_out_raw"]) / (10 ** dec_out)
    value_at_sqrt_after_usd = _value_usd(
        0, amount_out_human, p_t1_t0, 1/p_t1_t0, sym0, sym1, t0, t1
    )

    return {
        "best_tick_spacing": int(best["tick_spacing"]),
        "amount_in_raw": amount_in_raw,
        "amount_out_raw": int(best["amount_out_raw"]),
        "amount_in": float(req.amount_in),
        "amount_out": amount_out_human,
        "sqrtPriceX96_after": int(best["sqrt_after"]),
        "initialized_ticks_crossed": int(best["ticks_crossed"]),
        "gas_estimate": int(best["gas_est"]),
        "gas_price_wei": gas_price_wei,
        "gas_price_gwei": float(Decimal(gas_price_wei) / Decimal(10**9)),
        "gas_eth": float(gas_eth),
        "gas_usd": float(gas_usd) if gas_usd else None,
        "value_at_sqrt_after_usd": float(value_at_sqrt_after_usd),
    }

@router.post("/vaults/aerodrome/{alias}/swap/exact-in")
def aero_swap_exact_in(alias: str, req: SwapExactInRequest):
    dex = "aerodrome"
    
    v = vault_repo.get_vault("aerodrome", alias)
    if not v: raise HTTPException(404, "Unknown alias (aerodrome)")
    s = get_settings()
    if not s.AERO_ROUTER or not s.AERO_QUOTER:
        raise HTTPException(500, "AERO_ROUTER/AERO_QUOTER not configured")

    state_repo.ensure_state_initialized("aerodrome", alias, vault_address=v["address"])
    ad = _adapter_for("aerodrome", v["pool"], v.get("nfpm"), v["address"], v.get("rpc_url"))

    before = snapshot_status(ad, dex, alias)
    
    # ---- decimals e resolução de amount_in_raw (igual Uniswap)
    dec_in  = int(ad.erc20(req.token_in).functions.decimals().call())
    dec_out = int(ad.erc20(req.token_out).functions.decimals().call())

    def _is_usdc(sym: str) -> bool: return sym.upper() in USD_SYMBOLS
    def _is_eth(sym: str)  -> bool: return sym.upper() in {"WETH","ETH"}

    if req.amount_in is not None:
        amount_in_raw = int(float(req.amount_in) * (10 ** dec_in))
        resolved_mode = "token"
    elif req.amount_in_usd is not None:
        meta = ad.pool_meta()
        sym0, sym1 = meta["sym0"], meta["sym1"]
        dec0, dec1 = int(meta["dec0"]), int(meta["dec1"])
        sqrtP, _ = ad.slot0()
        p_t1_t0 = sqrtPriceX96_to_price_t1_per_t0(sqrtP, dec0, dec1)

        usdc_per_eth = None
        if _is_usdc(sym1) and _is_eth(sym0): usdc_per_eth = p_t1_t0
        elif _is_usdc(sym0) and _is_eth(sym1): usdc_per_eth = (0.0 if p_t1_t0==0 else 1.0/p_t1_t0)

        in_sym = ad.erc20(req.token_in).functions.symbol().call()
        if _is_eth(in_sym):
            if not usdc_per_eth:
                raise HTTPException(400, "Não foi possível obter USDC/ETH a partir do pool do vault.")
            amount_in_token = float(req.amount_in_usd) / float(usdc_per_eth)
            amount_in_raw = int(amount_in_token * (10 ** dec_in))
            resolved_mode = "usd"
        elif _is_usdc(in_sym):
            amount_in_raw = int(float(req.amount_in_usd) * (10 ** dec_in))
            resolved_mode = "usd"
        else:
            raise HTTPException(400, "amount_in_usd só é suportado quando token_in é WETH/ETH ou USDC.")
    else:
        raise HTTPException(400, "Informe amount_in (token) ou amount_in_usd.")

    if amount_in_raw <= 0:
        raise HTTPException(400, "amount_in deve ser > 0")

    # saldo do vault
    bal_in = int(ad.erc20(req.token_in).functions.balanceOf(v["address"]).call())
    if bal_in < amount_in_raw:
        raise HTTPException(400, f"insufficient vault balance: have {bal_in}, need {amount_in_raw}")

    # Quote Aerodrome (reuso do endpoint acima para auto-escolher tickSpacing)
    fee = int(req.fee) if req.fee is not None else None
    quote = aero_swap_quote(alias, SwapQuoteRequest(
        alias=alias,
        token_in=req.token_in,
        token_out=req.token_out,
        amount_in=(float(req.amount_in) if resolved_mode=="token" else float(amount_in_raw) / (10 ** dec_in)),
        fee=fee,
        sqrt_price_limit_x96=req.sqrt_price_limit_x96
    ))
    ts_used = int(quote["best_tick_spacing"])
    amount_out_raw = int(quote["amount_out_raw"])
    if amount_out_raw <= 0:
        raise HTTPException(400, "quoter returned 0")

    # slippage
    bps = max(0, int(req.slippage_bps))
    min_out_raw = amount_out_raw * (10_000 - bps) // 10_000

    # tx: vault -> router (vault faz approve + swap)
    fn = ad.fn_vault_swap_exact_in_aero(
        router=s.AERO_ROUTER,
        token_in=req.token_in,
        token_out=req.token_out,
        tick_spacing=ts_used,
        amount_in_raw=amount_in_raw,
        min_out_raw=min_out_raw,
        sqrt_price_limit_x96=int(req.sqrt_price_limit_x96 or 0),
    )

    txs = TxService(v.get("rpc_url"))
    try:
        send_res = txs.send(fn, wait=True, gas_strategy="buffered")
    except TransactionRevertedError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reverted_on_chain",
                "tx": e.tx_hash,
                "receipt": e.receipt,
                "hint": "Likely out-of-gas or slippage/guard.",
            }
        )
        
    tx_hash = send_res["tx_hash"]
    rcpt = send_res["receipt"] or {}

    gas_limit_used = send_res.get("gas_limit_used")

    gas_used = int(rcpt.get("gasUsed") or 0)
    eff_price_wei = int(rcpt.get("effectiveGasPrice") or 0)
    
    gas_eth = None
    gas_usd = None
    if gas_used and eff_price_wei:
        gas_eth = float(
            (Decimal(gas_used) * Decimal(eff_price_wei)) / Decimal(10**18)
        )
        meta2 = ad.pool_meta()
        sym0b = str(meta2["sym0"]).upper()
        sym1b = str(meta2["sym1"]).upper()
        dec0b, dec1b = int(meta2["dec0"]), int(meta2["dec1"])
        sqrtPb, _ = ad.slot0()
        p_t1_t0b = sqrtPriceX96_to_price_t1_per_t0(sqrtPb, dec0b, dec1b)

        if sym1b in USD_SYMBOLS and sym0b in {"WETH", "ETH"}:
            # token0 é ETH, token1 é USD-ish -> p_t1_t0b = USDC per ETH
            gas_usd = gas_eth * p_t1_t0b
        elif sym0b in USD_SYMBOLS and sym1b in {"WETH", "ETH"}:
            # token0 é USD-ish, token1 é ETH -> invertido
            gas_usd = gas_eth * (
                0 if p_t1_t0b == 0 else 1.0 / p_t1_t0b
            )

    # snapshot AFTER
    after = snapshot_status(ad, dex, alias)

    # histórico
    state_repo.append_history(dex, alias, "exec_history", {
        "ts": datetime.utcnow().isoformat(),
        "mode": "swap_exact_in_aero",
        "token_in": req.token_in,
        "token_out": req.token_out,
        "resolved_amount_mode": resolved_mode,
        "amount_in_raw": amount_in_raw,
        "min_out_raw": min_out_raw,
        "tick_spacing_used": ts_used,
        "slippage_bps": bps,
        "tx": tx_hash,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "value_at_sqrt_after_usd": quote["value_at_sqrt_after_usd"],
    })

    return {
        "tx": tx_hash,
        "tick_spacing_used": ts_used,
        "resolved_amount_mode": resolved_mode,   # "token" ou "usd"
        "amount_in_raw": amount_in_raw,
        "quoted_out_raw": amount_out_raw,
        "min_out_raw": min_out_raw,
        "gas_used": gas_used,
        "gas_limit_used": gas_limit_used,
        "effective_gas_price_wei": eff_price_wei,
        "effective_gas_price_gwei": (
            float(eff_price_wei) / 1e9 if eff_price_wei else None
        ),
        "gas_eth": gas_eth,
        "gas_usd": gas_usd,
        "value_at_sqrt_after_usd": quote["value_at_sqrt_after_usd"],
        "before": before,
        "after": after,
    }
