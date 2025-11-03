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
    {"name":"stake", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"unstake", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"claimRewards", "outputs":[], "inputs":[], "stateMutability": "nonpayable","type":"function"},
    {"name":"swapExactInAero","outputs":[{"type":"uint256"}],"inputs":[
        {"type":"address","name":"router"},
        {"type":"address","name":"tokenIn"},
        {"type":"address","name":"tokenOut"},
        {"type":"int24","name":"tickSpacing"},
        {"type":"uint256","name":"amountIn"},
        {"type":"uint256","name":"amountOutMinimum"},
        {"type":"uint160","name":"sqrtPriceLimitX96"}
    ],"stateMutability":"nonpayable","type":"function"},
]

ABI_AERO_QUOTER = [
    {"inputs":[{"internalType":"address","name":"_factory","type":"address"},{"internalType":"address","name":"_WETH9","type":"address"}],"stateMutability":"nonpayable","type":"constructor"},
    {"inputs":[],"name":"WETH9","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"factory","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"components":[
        {"internalType":"address","name":"tokenIn","type":"address"},
        {"internalType":"address","name":"tokenOut","type":"address"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"},
        {"internalType":"int24","name":"tickSpacing","type":"int24"},
        {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],
      "internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],
     "name":"quoteExactInputSingle",
     "outputs":[
        {"internalType":"uint256","name":"amountOut","type":"uint256"},
        {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
        {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
        {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
     "stateMutability":"nonpayable","type":"function"}
]

ABI_AERO_ROUTER = [
    {"inputs":[{"internalType":"address","name":"_factory","type":"address"},{"internalType":"address","name":"_WETH9","type":"address"}],"stateMutability":"nonpayable","type":"constructor"},
    {"inputs":[{"components":[
        {"internalType":"address","name":"tokenIn","type":"address"},
        {"internalType":"address","name":"tokenOut","type":"address"},
        {"internalType":"int24","name":"tickSpacing","type":"int24"},
        {"internalType":"address","name":"recipient","type":"address"},
        {"internalType":"uint256","name":"deadline","type":"uint256"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"},
        {"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},
        {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],
      "internalType":"struct ISwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],
     "name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],
     "stateMutability":"payable","type":"function"}
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

    def erc20_contract(self):
        reward_token_addr = self.gauge_contract().functions.rewardToken().call()
        return self.w3.eth.contract(address=Web3.to_checksum_address(reward_token_addr), abi=self.erc20_abi())
        
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
    
    def adapter_address(self) -> Optional[str]:
        """Endereço do adapter configurado no Vault V2 (vault.adapter())."""
        try:
            addr = self.vault.functions.adapter().call()
            if int(addr, 16) == 0:
                return None
            return Web3.to_checksum_address(addr)
        except Exception:
            return None
    
    def aerodrome_quoter(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI_AERO_QUOTER)

    def aerodrome_router(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI_AERO_ROUTER)

    
    
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
        Unifica leitura como no Uniswap e acrescenta gauge/staked:
          - tokenId, lower, upper, liq
          - twapOk (sempre True aqui)
          - lastRebalance, min_cd (adapter)
          - gauge (addr), hasGauge (bool), staked (bool), adapter (addr)
        """
        # --- tokenId no Vault (V2 mantém espelho), fallback para view
        token_id = 0
        try:
            token_id = int(self.vault.functions.positionTokenId().call())
        except Exception:
            try:
                token_id = int(self.vault.functions.positionTokenIdView().call())
            except Exception:
                token_id = 0

        # --- bounds/liquidity (NFPM.positions) ou spot-tick se não há posição
        lower = upper = 0
        liq = 0
        if token_id:
            nfpm = self.nfpm_contract()
            (_n, _op, _t0, _t1, _ts, l, u, L, *_rest) = nfpm.functions.positions(int(token_id)).call()
            lower, upper, liq = int(l), int(u), int(L)
        else:
            _, spot_tick = self.slot0()
            lower = upper = int(spot_tick)
            liq = 0

        # --- adapter infos
        ad_addr = self.adapter_address()
        ad = self.adapter_contract()
        min_cd = 0
        last_reb = 0
        if ad:
            try:
                min_cd = int(ad.functions.minCooldown().call())
            except Exception:
                pass
            try:
                last_reb = int(ad.functions.lastRebalance(self.vault.address).call())
            except Exception:
                pass

        # --- gauge & staked
        g_addr = self.gauge_address()
        has_gauge = bool(g_addr)
        staked = False
        if has_gauge and token_id:
            try:
                g = self.gauge_contract()
                # o depositante é o ADAPTER (owner do NFT). fallback: tentar o vault.
                depositor = ad_addr if ad_addr else self.vault.address
                staked = bool(g.functions.stakedContains(Web3.to_checksum_address(depositor), int(token_id)).call())
            except Exception:
                staked = False

        return {
            "tokenId": token_id,
            "lower": lower,
            "upper": upper,
            "liq": liq,
            "twapOk": True,                 # SlipstreamAdapter não expõe twapOk()
            "lastRebalance": last_reb,
            "min_cd": min_cd,
            "gauge": g_addr,
            "hasGauge": has_gauge,
            "staked": staked,
            "adapter": ad_addr,
        }

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
    def fn_stake_nft(self):
        """
        Chama Vault.stake() -> Adapter.stakePosition(vault) -> Gauge.deposit(tokenId).
        Use este método no endpoint (NÃO chame o gauge direto).
        """
        if hasattr(self.vault.functions, "stake"):
            return self.vault.functions.stake()
        raise NotImplementedError("Use vault.stake() via fn_stake_nft()")
    
    def fn_unstake_nft(self):
        """
        Chama Vault.unstake() -> Adapter.unstakePosition(vault) -> Gauge.withdraw(tokenId).
        """
        if hasattr(self.vault.functions, "unstake"):
            return self.vault.functions.unstake()
        raise NotImplementedError("Use vault.unstake() via fn_unstake_nft()")
    
    def fn_claim_rewards(self):
        """
        Chama Vault.claimRewards() -> Adapter.claimRewards(vault) -> Gauge.getReward(...).
        """
        if hasattr(self.vault.functions, "claimRewards"):
            return self.vault.functions.claimRewards()
        raise NotImplementedError("Use vault.claimRewards() via fn_claim_rewards()")
    
    def fn_vault_swap_exact_in_aero(
        self,
        router: str,
        token_in: str,
        token_out: str,
        tick_spacing: int,
        amount_in_raw: int,
        min_out_raw: int,
        sqrt_price_limit_x96: int = 0
    ):
        """
        Chama o método específico do Vault V2 (swapExactInAero) para Aerodrome.
        O Vault faz approve JIT para o router e executa o swap.
        """
        if not hasattr(self.vault.functions, "swapExactInAero"):
            raise NotImplementedError("Vault V2 precisa expor swapExactInAero(...) para Aerodrome.")
        return self.vault.functions.swapExactInAero(
            Web3.to_checksum_address(router),
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(tick_spacing),
            int(amount_in_raw),
            int(min_out_raw),
            int(sqrt_price_limit_x96 or 0)
        )