# apps/api/adapters/aerodrome.py
import os
import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from .base import DexAdapter
from bot.utils.math_univ3 import get_sqrt_ratio_at_tick, get_amounts_for_liquidity


# ABI mínimo para Slipstream CL pool (tenta slot0(); fallback para globalState())
ABI_POOL = [
    # Uniswap v3-style
    {"name":"slot0","outputs":[
        {"type":"uint160","name":"sqrtPriceX96"},
        {"type":"int24","name":"tick"},
        {"type":"uint16","name":"observationIndex"},
        {"type":"uint16","name":"observationCardinality"},
        {"type":"uint16","name":"observationCardinalityNext"},
        {"type":"uint8","name":"feeProtocol"},
        {"type":"bool","name":"unlocked"}],
     "inputs":[],"stateMutability":"view","type":"function"},

    # Algebra/Slipstream-style (fallback)
    {"name":"globalState","outputs":[
        {"type":"uint160","name":"price"},
        {"type":"int24","name":"tick"},
        {"type":"uint16","name":"lastFee"},
        {"type":"uint8","name":"pluginConfig"},
        {"type":"bool","name":"unlocked"}],
     "inputs":[],"stateMutability":"view","type":"function"},

    {"name":"observe","outputs":[
        {"type":"int56[]","name":"tickCumulatives"},
        {"type":"uint160[]","name":"secondsPerLiquidityCumulativeX128"}],
     "inputs":[{"type":"uint32[]","name":"secondsAgos"}],
     "stateMutability":"view","type":"function"},

    {"name":"token0","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"token1","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},

    {"name":"tickSpacing","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"fee","outputs":[{"type":"uint24"}],"inputs":[],"stateMutability":"view","type":"function"}
]
ABI_ERC20 = [
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]
ABI_NFPM = [
    {"name":"positions","outputs":[
        {"type":"uint96"}, {"type":"address"}, {"type":"address"}, {"type":"address"},
        {"type":"uint24"}, {"type":"int24"}, {"type":"int24"}, {"type":"uint128"},
        {"type":"uint256"}, {"type":"uint256"}, {"type":"uint128"}, {"type":"uint128"}],
     "inputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"name":"collect","outputs":[{"type":"uint256"},{"type":"uint256"}],
     "inputs":[{"components":[
        {"type":"uint256","name":"tokenId"},
        {"type":"address","name":"recipient"},
        {"type":"uint128","name":"amount0Max"},
        {"type":"uint128","name":"amount1Max"}],
       "type":"tuple","name":"params"}],
     "stateMutability":"nonpayable","type":"function"},
]
# minimal vault ABI (adapt names if your contract differs)
ABI_VAULT = [
    {"name":"pool","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"positionTokenId","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"currentRange","outputs":[{"type":"int24"},{"type":"int24"},{"type":"uint128"}],
     "inputs":[],"stateMutability":"view","type":"function"},
    {"name":"twapOk","outputs":[{"type":"bool"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"lastRebalance","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"minWidth","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"maxWidth","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"minCooldown","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"twapWindow","outputs":[{"type":"uint32"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"maxTwapDeviationTicks","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
    # mutations (adjust names if needed)
    {"name":"openInitialPosition","outputs":[],"inputs":[{"type":"int24"},{"type":"int24"}],"stateMutability":"nonpayable","type":"function"},
    {"name":"rebalanceWithCaps","outputs":[],"inputs":[{"type":"int24"},{"type":"int24"},{"type":"uint256"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"name":"exitPositionToVault","outputs":[],"inputs":[],"stateMutability":"nonpayable","type":"function"},
    {"name":"exitPositionAndWithdrawAll","outputs":[],"inputs":[],"stateMutability":"nonpayable","type":"function"},
    {"name":"collectToVault","outputs":[],"inputs":[],"stateMutability":"nonpayable","type":"function"},
]

# ABI_FACTORY = [{"inputs":[{"internalType":"address","name":"_voter","type":"address"},{"internalType":"address","name":"_poolImplementation","type":"address"}],"stateMutability":"nonpayable","type":"constructor"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"uint24","name":"oldUnstakedFee","type":"uint24"},{"indexed":True,"internalType":"uint24","name":"newUnstakedFee","type":"uint24"}],"name":"DefaultUnstakedFeeChanged","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"oldOwner","type":"address"},{"indexed":True,"internalType":"address","name":"newOwner","type":"address"}],"name":"OwnerChanged","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"token0","type":"address"},{"indexed":True,"internalType":"address","name":"token1","type":"address"},{"indexed":True,"internalType":"int24","name":"tickSpacing","type":"int24"},{"indexed":False,"internalType":"address","name":"pool","type":"address"}],"name":"PoolCreated","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"oldFeeManager","type":"address"},{"indexed":True,"internalType":"address","name":"newFeeManager","type":"address"}],"name":"SwapFeeManagerChanged","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"oldFeeModule","type":"address"},{"indexed":True,"internalType":"address","name":"newFeeModule","type":"address"}],"name":"SwapFeeModuleChanged","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"int24","name":"tickSpacing","type":"int24"},{"indexed":True,"internalType":"uint24","name":"fee","type":"uint24"}],"name":"TickSpacingEnabled","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"oldFeeManager","type":"address"},{"indexed":True,"internalType":"address","name":"newFeeManager","type":"address"}],"name":"UnstakedFeeManagerChanged","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"oldFeeModule","type":"address"},{"indexed":True,"internalType":"address","name":"newFeeModule","type":"address"}],"name":"UnstakedFeeModuleChanged","type":"event"},{"inputs":[{"internalType":"uint256","name":"","type":"uint256"}],"name":"allPools","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"allPoolsLength","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"int24","name":"tickSpacing","type":"int24"},{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"}],"name":"createPool","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"defaultUnstakedFee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"tickSpacing","type":"int24"},{"internalType":"uint24","name":"fee","type":"uint24"}],"name":"enableTickSpacing","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"factoryRegistry","outputs":[{"internalType":"contract IFactoryRegistry","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"","type":"address"},{"internalType":"address","name":"","type":"address"},{"internalType":"int24","name":"","type":"int24"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"pool","type":"address"}],"name":"getSwapFee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"pool","type":"address"}],"name":"getUnstakedFee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"pool","type":"address"}],"name":"isPool","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"owner","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"poolImplementation","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"uint24","name":"_defaultUnstakedFee","type":"uint24"}],"name":"setDefaultUnstakedFee","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_owner","type":"address"}],"name":"setOwner","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_swapFeeManager","type":"address"}],"name":"setSwapFeeManager","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_swapFeeModule","type":"address"}],"name":"setSwapFeeModule","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_unstakedFeeManager","type":"address"}],"name":"setUnstakedFeeManager","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_unstakedFeeModule","type":"address"}],"name":"setUnstakedFeeModule","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"swapFeeManager","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"swapFeeModule","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"","type":"int24"}],"name":"tickSpacingToFee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"tickSpacings","outputs":[{"internalType":"int24[]","name":"","type":"int24[]"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"unstakedFeeManager","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"unstakedFeeModule","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"voter","outputs":[{"internalType":"contract IVoter","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
AERODROME_POOL_FACTORY = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A" 
U128_MAX = (1<<128) - 1

ABI_DIR = Path("libs/abi/aerodrome")

def _load_abi_json(name: str) -> list:
    p = ABI_DIR / name
    return json.loads(p.read_text(encoding="utf-8"))

class AerodromeAdapter(DexAdapter):
    """
    Slipstream (Aerodrome v3-like) adapter.
    Uses the same v3 math/ABIs pattern as UniswapV3Adapter.
    """

    def pool_abi(self) -> list: return _load_abi_json("PoolImplementation.json") 
    def erc20_abi(self) -> list: return ABI_ERC20
    def nfpm_abi(self) -> list:  return _load_abi_json("INonfungiblePositionManager.json") 
    def vault_abi(self) -> list: return ABI_VAULT
    def abi_factory(self) -> list: return _load_abi_json("IUniswapV3Pool.json")
    def gauge_impl_abi(self) -> list: return _load_abi_json("GaugeImplementation.json")
    
    def factory_contract(self):
        addr = Web3.to_checksum_address(
            os.getenv("AERODROME_POOL_FACTORY", AERODROME_POOL_FACTORY)
        )
        return self.w3.eth.contract(address=addr, abi=self.abi_factory())

    def assert_is_pool(self):
        try:
            is_pool = self.factory_contract().functions.isPool(
                Web3.to_checksum_address(self.pool)
            ).call()
        except Exception:
            is_pool = False
        if not is_pool:
            raise ValueError("Provided address is not a Slipstream pool (factory.isPool == false).")

    # sobrescreva pool_contract para validar
    def pool_contract(self):
        self.assert_is_pool()
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.pool), abi=self.pool_abi())

    def nfpm_contract(self):
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.nfpm), abi=self.nfpm_abi()) if self.nfpm else None

    def gauge_address(self) -> Optional[str]:
        """Resolve gauge from pool; returns None if pool has no gauge."""
        try:
            return self.pool_contract().functions.gauge().call()
        except Exception:
            return None

    def gauge_contract(self):
        g = self.gauge_address()
        return self.w3.eth.contract(address=Web3.to_checksum_address(g), abi=self.gauge_impl_abi()) if g and int(g, 16) != 0 else None


    # ---------- reads ----------
    def slot0(self) -> Tuple[int,int]:
        """
        Return (sqrtPriceX96, tick) from Slipstream pool.
        """
        s = self.pool_contract().functions.slot0().call()
        # (sqrtPriceX96, tick, observationIndex, observationCardinality, observationCardinalityNext, unlocked)
        return int(s[0]), int(s[1])

    def observe_twap_tick(self, window: int) -> int:
        """
        Return TWAP tick over `window` seconds using pool.observe([window,0]).
        """
        tick_cums, _ = self.pool_contract().functions.observe([int(window), 0]).call()
        return (int(tick_cums[1]) - int(tick_cums[0])) // int(window)


    def pool_meta(self) -> Dict[str, Any]:
        """
        Fetch token addresses/symbols/decimals and tickSpacing from pool.
        """
        pc = self.pool_contract()
        t0 = pc.functions.token0().call()
        t1 = pc.functions.token1().call()
        spacing = int(pc.functions.tickSpacing().call())
        e0 = self.erc20(t0)
        e1 = self.erc20(t1)
        sym0 = e0.functions.symbol().call()
        sym1 = e1.functions.symbol().call()
        dec0 = int(e0.functions.decimals().call())
        dec1 = int(e1.functions.decimals().call())
        return {"token0": t0, "token1": t1, "spacing": spacing, "sym0": sym0, "sym1": sym1, "dec0": dec0, "dec1": dec1}

    def vault_state(self) -> Dict[str, Any]:
        """
        Try to mirror the Uniswap adapter: we assume the vault exposes
        - positionTokenId()  (uint256)
        - currentRange() -> (int24 lower, int24 upper, uint128 liq) — optional
        - twapOk(), lastRebalance() — optional
        """
        token_id = 0
        try:
            token_id = int(self.vault.functions.positionTokenId().call())
        except Exception:
            pass

        lower = upper = 0
        liq = 0
        try:
            lower, upper, liq = self.vault.functions.currentRange().call()
            lower, upper, liq = int(lower), int(upper), int(liq)
        except Exception:
            # fallback to spot tick as both bounds when no position
            _, spot_tick = self.slot0()
            lower = upper = int(spot_tick); liq = 0

        d = {
            "pool": self.vault.functions.pool().call(),
            "tokenId": token_id,
            "lower": lower, "upper": upper, "liq": liq,
        }
        try:
            d["twapOk"] = bool(self.vault.functions.twapOk().call())
        except Exception:
            d["twapOk"] = True
        try:
            d["lastRebalance"] = int(self.vault.functions.lastRebalance().call())
        except Exception:
            d["lastRebalance"] = 0
        return d

    def amounts_in_position_now(self, lower: int, upper: int, liq: int) -> Tuple[int,int]:
        """
        Compute amounts for a Uniswap v3-like position under current price.
        """
        sqrtP = self.slot0()[0]
        sqrtA = get_sqrt_ratio_at_tick(lower)
        sqrtB = get_sqrt_ratio_at_tick(upper)
        return get_amounts_for_liquidity(sqrtP, sqrtA, sqrtB, liq)

    def call_static_collect(self, token_id: int, recipient: str) -> Tuple[int, int]:
        """
        Preview collect via NFPM.collect() static call (payable in ABI; .call() is fine).
        """
        if not self.nfpm:
            return (0, 0)
        nfpm = self.nfpm_contract()
        a0, a1 = nfpm.functions.collect((
            int(token_id),
            Web3.to_checksum_address(recipient),
            U128_MAX, U128_MAX
        )).call()
        return int(a0), int(a1)

        # ---------- gauge reads ----------
    def gauge_preview_earned(self, account: str, token_id: int) -> int:
        """
        Read claimable rewards for (account, tokenId). Returns 0 if no gauge.
        """
        g = self.gauge_contract()
        if not g:
            return 0
        try:
            return int(g.functions.earned(Web3.to_checksum_address(account), int(token_id)).call())
        except Exception:
            return 0

    # ---------- writes (return ContractFunctions) ----------
    def fn_open(self, lower: int, upper: int):
        """
        Vault mutation: openInitialPosition(lower, upper).
        """
        if hasattr(self.vault.functions, "openInitialPosition"):
            return self.vault.functions.openInitialPosition(int(lower), int(upper))
        raise NotImplementedError("Vault missing openInitialPosition")

    def fn_rebalance_caps(self, lower: int, upper: int, cap0_raw: Optional[int], cap1_raw: Optional[int]):
        """
        Vault mutation: rebalanceWithCaps(lower, upper, cap0_raw, cap1_raw).
        """
        cap0_raw = int(cap0_raw or 0)
        cap1_raw = int(cap1_raw or 0)
        if hasattr(self.vault.functions, "rebalanceWithCaps"):
            return self.vault.functions.rebalanceWithCaps(int(lower), int(upper), cap0_raw, cap1_raw)
        raise NotImplementedError("Vault missing rebalanceWithCaps")

    def fn_exit(self):
        if hasattr(self.vault.functions, "exitPositionToVault"):
            return self.vault.functions.exitPositionToVault()
        raise NotImplementedError("Vault missing exitPositionToVault")

    def fn_exit_withdraw(self):
        if hasattr(self.vault.functions, "exitPositionAndWithdrawAll"):
            return self.vault.functions.exitPositionAndWithdrawAll()
        raise NotImplementedError("Vault missing exitPositionAndWithdrawAll")

    def fn_collect(self):
        if hasattr(self.vault.functions, "collectToVault"):
            return self.vault.functions.collectToVault()
        raise NotImplementedError("Vault missing collectToVault")

    def fn_deposit_erc20(self, token: str, amount_raw: int):
        """
        Default path: simple transfer(token, vault, amountRaw).
        """
        c = self.erc20(token)
        return c.functions.transfer(self.vault.address, int(amount_raw))

    # ---------- gauge writes ----------
    def fn_gauge_stake(self, token_id: Optional[int] = None):
        """
        Stake position tokenId into pool's gauge. If token_id is None, will attempt to read
        from vault.positionTokenId().
        """
        g = self.gauge_contract()
        if not g:
            raise NotImplementedError("Pool has no gauge()")
        if token_id is None:
            token_id = int(self.vault.functions.positionTokenId().call())
        if not token_id:
            raise ValueError("No position tokenId to stake")
        return g.functions.deposit(int(token_id))

    def fn_gauge_unstake(self, token_id: Optional[int] = None):
        g = self.gauge_contract()
        if not g:
            raise NotImplementedError("Pool has no gauge()")
        if token_id is None:
            token_id = int(self.vault.functions.positionTokenId().call())
        if not token_id:
            raise ValueError("No position tokenId to unstake")
        return g.functions.withdraw(int(token_id))

    def fn_gauge_claim(self, token_id: Optional[int] = None):
        g = self.gauge_contract()
        if not g:
            raise NotImplementedError("Pool has no gauge()")
        if token_id is None:
            token_id = int(self.vault.functions.positionTokenId().call())
        if not token_id:
            raise ValueError("No position tokenId to claim")
        return g.functions.getReward(int(token_id))