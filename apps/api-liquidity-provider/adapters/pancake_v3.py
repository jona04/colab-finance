# apps/api/adapters/pancake_v3.py
import os
from typing import Dict, Any, Tuple, Optional
from web3 import Web3
from .base import DexAdapter
from bot.utils.math_univ3 import get_sqrt_ratio_at_tick, get_amounts_for_liquidity
from ..config import get_settings

ABI_POOL = [
    {"name":"slot0","outputs":[
        {"type":"uint160","name":"sqrtPriceX96"},
        {"type":"int24","name":"tick"},
        {"type":"uint16"},{"type":"uint16"},{"type":"uint16"},{"type":"uint8"},{"type":"bool"}],
     "inputs":[],"stateMutability":"view","type":"function"},
    {"name":"observe","outputs":[
        {"type":"int56[]","name":"tickCumulatives"},
        {"type":"uint160[]","name":"secondsPerLiquidityCumulativeX128"}],
     "inputs":[{"type":"uint32[]","name":"secondsAgos"}],
     "stateMutability":"view","type":"function"},
    {"name":"token0","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"token1","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"fee","outputs":[{"type":"uint24"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"tickSpacing","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
]

ABI_ERC20 = [
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]

ABI_VAULT = [
    {"name":"adapter","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"positionTokenId","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"positionTokenIdView","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"openInitialPosition","outputs":[],"inputs":[{"type":"int24"},{"type":"int24"}],"stateMutability":"nonpayable","type":"function"},
    {"name":"rebalanceWithCaps","outputs":[{"type":"uint128"}],"inputs":[{"type":"int24"},{"type":"int24"},{"type":"uint256"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"name":"exitPositionToVault","outputs":[],"inputs":[],"stateMutability":"nonpayable","type":"function"},
    {"name":"exitPositionAndWithdrawAll","outputs":[],"inputs":[{"type":"address"}],"stateMutability":"nonpayable","type":"function"},
    {"name":"collectToVault","outputs":[{"type":"uint256"},{"type":"uint256"}],"inputs":[],"stateMutability":"nonpayable","type":"function"},
    {"name":"stake", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"unstake", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"claimRewards", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"swapExactInPancake","outputs":[{"type":"uint256"}],"inputs":[
        {"type":"address","name":"router"},
        {"type":"address","name":"tokenIn"},
        {"type":"address","name":"tokenOut"},
        {"type":"uint24","name":"fee"},
        {"type":"uint256","name":"amountIn"},
        {"type":"uint256","name":"amountOutMinimum"},
        {"type":"uint160","name":"sqrtPriceLimitX96"}
    ],"stateMutability":"nonpayable","type":"function"}
]

U128_MAX = (1<<128) - 1

import json
from pathlib import Path
ABI_DIR = Path("libs/abi/pancake")
def _load_abi_json(name: str) -> list:
    p = ABI_DIR / name
    return json.loads(p.read_text(encoding="utf-8"))

class PancakeV3Adapter(DexAdapter):
    """Adapter para PancakeSwap v3 (Uniswap v3-like) + MasterChefV3 farms."""

    def pool_abi(self) -> list:         return _load_abi_json("Pool.json")
    def nfpm_abi(self) -> list:         return _load_abi_json("NonfungiblePositionManager.json")
    def erc20_abi(self) -> list:   return ABI_ERC20
    def vault_abi(self) -> list:   return ABI_VAULT
    def quoter_abi(self) -> list:  return _load_abi_json("QuoterV2.json")
    def masterchef_abi(self) -> list: return _load_abi_json("MasterChefV3.json")

    # ---------- contratos ----------
    def pool_contract(self):
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.pool), abi=self.pool_abi())

    def nfpm_contract(self):
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.nfpm), abi=self.nfpm_abi()) if self.nfpm else None

    def quoter(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=self.quoter_abi())

    def masterchef(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=self.masterchef_abi())

    # --- helpers de gauge (MasterChefV3) ---
    def gauge_contract(self):
        ga = self.gauge
        return self.masterchef(ga) if ga else None

    def adapter_address(self) -> str:
        # endereço do adapter não é usado p/ MasterChef, mas deixamos por consistência
        return self.pool  # ou outro endereço do adapter caso você tenha

    # ---------- leituras ----------
    def slot0(self) -> Tuple[int,int]:
        s = self.pool_contract().functions.slot0().call()
        return int(s[0]), int(s[1])

    def observe_twap_tick(self, window: int) -> int:
        tick_cums, _ = self.pool_contract().functions.observe([window, 0]).call()
        return (int(tick_cums[1]) - int(tick_cums[0])) // int(window)

    def pool_meta(self) -> Dict[str, Any]:
        pc = self.pool_contract()
        t0 = pc.functions.token0().call()
        t1 = pc.functions.token1().call()
        e0 = self.erc20(t0); e1 = self.erc20(t1)
        try: sym0 = e0.functions.symbol().call()
        except: sym0 = "T0"
        try: sym1 = e1.functions.symbol().call()
        except: sym1 = "T1"
        dec0 = int(e0.functions.decimals().call())
        dec1 = int(e1.functions.decimals().call())
        # fee() existe no Pancake v3 pool (Uniswap-like)
        fee = int(pc.functions.fee().call())
        tickSpacing = int(pc.functions.tickSpacing().call())
        return {"token0": t0, "token1": t1, "sym0": sym0, "sym1": sym1, "dec0": dec0, "dec1": dec1, "fee": fee, "spacing": tickSpacing}

    def vault_state(self) -> Dict[str, Any]:
        token_id = 0
        try:
            token_id = int(self.vault.functions.positionTokenId().call())
        except Exception:
            try:
                token_id = int(self.vault.functions.positionTokenIdView().call())
            except Exception:
                token_id = 0

        lower = upper = 0
        liq = 0
        if token_id:
            nfpm = self.nfpm_contract()
            (_n, _op, _t0, _t1, _fee, l, u, L, *_rest) = nfpm.functions.positions(int(token_id)).call()
            lower, upper, liq = int(l), int(u), int(L)
        else:
            _, spot_tick = self.slot0()
            lower = upper = int(spot_tick)
            liq = 0

        mcv3_addr = self.gauge
        staked = False
        if token_id and mcv3_addr:
            try:
                owner = self.nfpm_contract().functions.ownerOf(int(token_id)).call()
                staked = owner.lower() == mcv3_addr.lower()
            except Exception:
                staked = False

        return {
            "tokenId": token_id,
            "lower": lower,
            "upper": upper,
            "liq": liq,
            "staked": staked,
            "gauge": (mcv3_addr if mcv3_addr else None),
            "twapOk": True
        }
        
    def amounts_in_position_now(self, lower: int, upper: int, liq: int) -> Tuple[int,int]:
        sqrtP = self.slot0()[0]
        sqrtA = get_sqrt_ratio_at_tick(lower)
        sqrtB = get_sqrt_ratio_at_tick(upper)
        return get_amounts_for_liquidity(sqrtP, sqrtA, sqrtB, liq)

    def call_static_collect(self, token_id: int, recipient: str) -> Tuple[int, int]:
        if not self.nfpm or not token_id:
            return (0, 0)
        nfpm = self.nfpm_contract()
        a0, a1 = nfpm.functions.collect((int(token_id), Web3.to_checksum_address(recipient), U128_MAX, U128_MAX)).call()
        return int(a0), int(a1)

    # ---------- helpers MasterChef ----------
    def masterchef_pid_for_pool(self, masterchef_addr: str, pool_addr: str) -> Optional[int]:
        try:
            mc = self.masterchef(masterchef_addr)
            pid = int(mc.functions.v3PoolAddressPid(Web3.to_checksum_address(pool_addr)).call())
            return pid if pid != 0 else None
        except Exception:
            return None

    def masterchef_pending(self, masterchef_addr: str, token_id: int) -> int:
        try:
            mc = self.masterchef(masterchef_addr)
            return int(mc.functions.pendingCake(int(token_id)).call())
        except Exception:
            return 0

    # ---------- writes (vault mutations) ----------
    def fn_open(self, lower: int, upper: int):
        if hasattr(self.vault.functions, "openInitialPosition"):
            return self.vault.functions.openInitialPosition(int(lower), int(upper))
        raise NotImplementedError("Vault missing openInitialPosition")

    def fn_rebalance_caps(self, lower: int, upper: int, cap0_raw: Optional[int], cap1_raw: Optional[int]):
        cap0_raw = int(cap0_raw or 0); cap1_raw = int(cap1_raw or 0)
        if hasattr(self.vault.functions, "rebalanceWithCaps"):
            return self.vault.functions.rebalanceWithCaps(int(lower), int(upper), cap0_raw, cap1_raw)
        raise NotImplementedError("Vault missing rebalanceWithCaps")

    def fn_exit(self):
        if hasattr(self.vault.functions, "exitPositionToVault"):
            return self.vault.functions.exitPositionToVault()
        raise NotImplementedError("Vault missing exitPositionToVault")

    def fn_exit_withdraw(self, to_addr: str):
        if hasattr(self.vault.functions, "exitPositionAndWithdrawAll"):
            return self.vault.functions.exitPositionAndWithdrawAll(Web3.to_checksum_address(to_addr))
        raise NotImplementedError("Vault missing exitPositionAndWithdrawAll")

    def fn_collect(self):
        if hasattr(self.vault.functions, "collectToVault"):
            return self.vault.functions.collectToVault()
        raise NotImplementedError("Vault missing collectToVault")

    def fn_deposit_erc20(self, token: str, amount_raw: int):
        c = self.erc20(token)
        return c.functions.transfer(self.vault.address, int(amount_raw))

    # ---------- swaps no Vault (Pancake v3 ≈ Uniswap v3) ----------
    def fn_vault_swap_exact_in(self, router: str, token_in: str, token_out: str,
                               fee: int, amount_in_raw: int, min_out_raw: int,
                               sqrt_price_limit_x96: int = 0):
        # reusa a mesma função do VaultV2 (idêntica ao Uniswap)
        return self.vault.functions.swapExactInPancake(
            Web3.to_checksum_address(router),
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(fee),
            int(amount_in_raw),
            int(min_out_raw),
            int(sqrt_price_limit_x96 or 0)
        )

    # ---------- farms (MasterChefV3) ----------
    def fn_stake(self):
        """Stake via Vault.stake() -> adapter.stakePosition(gauge)."""
        if hasattr(self.vault.functions, "stake"):
            return self.vault.functions.stake()
        raise NotImplementedError("Vault must implement stake().")

    def fn_unstake(self):
        """Unstake via Vault.unstake() -> adapter.unstakePosition(gauge)."""
        if hasattr(self.vault.functions, "unstake"):
            return self.vault.functions.unstake()
        raise NotImplementedError("Vault must implement unstake().")

    def fn_harvest(self):
        """Harvest via Vault.claimRewards() -> adapter.claimRewards(gauge)."""
        if hasattr(self.vault.functions, "claimRewards"):
            return self.vault.functions.claimRewards()
        raise NotImplementedError("Vault must implement claimRewards().")
