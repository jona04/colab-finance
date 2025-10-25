# apps/api/adapters/aerodrome.py
import os
import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from .base import DexAdapter
from bot.utils.math_univ3 import get_sqrt_ratio_at_tick, get_amounts_for_liquidity


ABI_ERC20 = [
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]
ABI_ADAPTER_MIN = [
    {"name":"minCooldown","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"lastRebalance","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"name":"tickSpacing","outputs":[{"type":"int24"}],"inputs":[],"stateMutability":"view","type":"function"},
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
]

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

    def pool_abi(self) -> list:         return _load_abi_json("PoolImplementation.json")
    def nfpm_abi(self) -> list:         return _load_abi_json("NonfungiblePositionManager.json")
    def factory_abi(self) -> list:      return _load_abi_json("PoolFactory.json")
    def gauge_impl_abi(self) -> list:   return _load_abi_json("GaugeImplementation.json")
    def erc20_abi(self) -> list:        return ABI_ERC20
    def vault_abi(self) -> list:        return ABI_VAULT
    
    # ---- contracts helpers ----
    def pool_contract(self):
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.pool), abi=self.pool_abi())

    def nfpm_contract(self):
        return self.w3.eth.contract(address=Web3.to_checksum_address(self.nfpm), abi=self.nfpm_abi()) if self.nfpm else None

    def factory_contract(self):
        addr = Web3.to_checksum_address(os.getenv("AERODROME_POOL_FACTORY", "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"))
        return self.w3.eth.contract(address=addr, abi=self.factory_abi())
    
    def gauge_address(self) -> Optional[str]:
        try:
            g = self.pool_contract().functions.gauge().call()
            if int(g, 16) == 0: return None
            return Web3.to_checksum_address(g)
        except Exception:
            return None

    def gauge_contract(self):
        g = self.gauge_address()
        return self.w3.eth.contract(address=g, abi=self.gauge_impl_abi()) if g else None

    def adapter_contract(self):
        # Lê o endereço do adapter via vault.adapter()
        try:
            adapter_addr = self.vault.functions.adapter().call()
            if int(adapter_addr, 16) == 0:
                return None
            return self.w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=ABI_ADAPTER_MIN)
        except Exception:
            return None
    
    
    # ---- sanity ----
    def assert_is_pool(self):
        try:
            ok = self.factory_contract().functions.isPool(Web3.to_checksum_address(self.pool)).call()
        except Exception:
            ok = False
        if not ok:
            raise ValueError("Provided address is not an Aerodrome Slipstream pool (factory.isPool == false).")

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
        self.assert_is_pool()
        pc = self.pool_contract()
        t0 = pc.functions.token0().call()
        t1 = pc.functions.token1().call()
        spacing = int(pc.functions.tickSpacing().call())

        e0 = self.erc20(t0); e1 = self.erc20(t1)
        try: sym0 = e0.functions.symbol().call()
        except: sym0 = "T0"
        try: sym1 = e1.functions.symbol().call()
        except: sym1 = "T1"
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
            try:
                token_id = int(self.vault.functions.positionTokenIdView().call())
            except Exception:
                token_id = 0

        lower = upper = 0
        liq = 0
        if token_id:
            nfpm = self.nfpm_contract()
            (_n, _op, _t0, _t1, _ts, l, u, L, *_rest) = nfpm.functions.positions(int(token_id)).call()
            lower, upper, liq = int(l), int(u), int(L)
        else:
            # sem posição: fixa bounds no tick atual
            _, spot_tick = self.slot0()
            lower = upper = int(spot_tick)
            liq = 0

        d = {"tokenId": token_id, "lower": lower, "upper": upper, "liq": liq}
        
        ad = self.adapter_contract()
        if ad:
            try:
                d["min_cd"] = int(ad.functions.minCooldown().call())
            except Exception:
                d["min_cd"] = 0
            try:
                d["lastRebalance"] = int(ad.functions.lastRebalance(self.vault.address).call())
            except Exception:
                d["lastRebalance"] = 0
        else:
            d["min_cd"] = 0
            d["lastRebalance"] = 0

        # twapOk: SlipstreamAdapter.sol não expõe; marcamos True por padrão
        d["twapOk"] = True
        return d

    def amounts_in_position_now(self, lower: int, upper: int, liq: int) -> Tuple[int, int]:
        """
        Quantidades hoje para a posição (mesma matemática do Uniswap v3).
        """
        sqrtP = self.slot0()[0]
        sqrtA = get_sqrt_ratio_at_tick(int(lower))
        sqrtB = get_sqrt_ratio_at_tick(int(upper))
        return get_amounts_for_liquidity(int(sqrtP), int(sqrtA), int(sqrtB), int(liq))

    def call_static_collect(self, token_id: int, recipient: str) -> Tuple[int, int]:
        """
        Preview do collect pela NFPM (static call).
        """
        if not self.nfpm:
            return (0, 0)
        if not token_id:
            return (0, 0)
        nfpm = self.nfpm_contract()
        a0, a1 = nfpm.functions.collect(
            (int(token_id), Web3.to_checksum_address(recipient), U128_MAX, U128_MAX)
        ).call()
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
        
        slot0 = self.pool_contract().functions.slot0().call()
        print("-----------------\n\n\n\n\n\n", slot0)
    
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

    def fn_exit_withdraw(self, to_address: str):
        if hasattr(self.vault.functions, "exitPositionAndWithdrawAll"):
            return self.vault.functions.exitPositionAndWithdrawAll(Web3.to_checksum_address(to_address))
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
            # tenta ler do vault
            try:
                token_id = int(self.vault.functions.positionTokenId().call())
            except Exception:
                token_id = int(self.vault.functions.positionTokenIdView().call())
        if not token_id:
            raise ValueError("No position tokenId to stake")
        return g.functions.deposit(int(token_id))

    def fn_gauge_unstake(self, token_id: Optional[int] = None):
        g = self.gauge_contract()
        if not g:
            raise NotImplementedError("Pool has no gauge()")
        if token_id is None:
            try:
                token_id = int(self.vault.functions.positionTokenId().call())
            except Exception:
                token_id = int(self.vault.functions.positionTokenIdView().call())
        if not token_id:
            raise ValueError("No position tokenId to unstake")
        return g.functions.withdraw(int(token_id))

    def fn_gauge_claim(self, token_id: Optional[int] = None):
        g = self.gauge_contract()
        if not g:
            raise NotImplementedError("Pool has no gauge()")
        if token_id is None:
            try:
                token_id = int(self.vault.functions.positionTokenId().call())
            except Exception:
                token_id = int(self.vault.functions.positionTokenIdView().call())
        if not token_id:
            raise ValueError("No position tokenId to claim")
        # Gauge tem 2 overloads; aqui usamos a do tokenId
        return g.functions.getReward(int(token_id))