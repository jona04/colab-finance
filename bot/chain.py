from web3 import Web3
from hexbytes import HexBytes
from typing import Tuple, Dict, Any, Optional
from bot.utils.math_univ3 import get_sqrt_ratio_at_tick, get_amounts_for_liquidity

# ABIs mínimos (fragmentos) — somente o que é usado
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

ABI_VAULT = [
  {"name":"pool","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
  {"name":"positionTokenId","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
  {"name":"currentRange","outputs":[{"type":"int24"},{"type":"int24"},{"type":"uint128"}],"inputs":[],"stateMutability":"view","type":"function"},
  {"name":"twapOk","outputs":[{"type":"bool"}],"inputs":[],"stateMutability":"view","type":"function"},
  {"name":"lastRebalance","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
]

U128_MAX = (1<<128) - 1

class Chain:
    def __init__(self, rpc_url: str, pool_addr: str, nfpm_addr: str, vault_addr: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.pool = self.w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=ABI_POOL)
        self.nfpm = self.w3.eth.contract(address=Web3.to_checksum_address(nfpm_addr), abi=ABI_NFPM)
        self.vault = self.w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=ABI_VAULT)

    def erc20(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI_ERC20)

    # -------- basic reads --------

    def slot0(self) -> Tuple[int,int]:
        s = self.pool.functions.slot0().call()
        sqrtP, tick = int(s[0]), int(s[1])
        return sqrtP, tick

    def observe_twap_tick(self, window: int) -> int:
        if window <= 0:
            raise ValueError("window must be > 0")
        tick_cums, _ = self.pool.functions.observe([window, 0]).call()
        # tick_twap = (tickCumulative[1] - tickCumulative[0]) / window
        twap = (int(tick_cums[1]) - int(tick_cums[0])) // int(window)
        return int(twap)

    def pool_meta(self) -> Dict[str, Any]:
        t0 = self.pool.functions.token0().call()
        t1 = self.pool.functions.token1().call()
        fee = self.pool.functions.fee().call()
        spacing = self.pool.functions.tickSpacing().call()
        e0 = self.erc20(t0)
        e1 = self.erc20(t1)
        sym0 = e0.functions.symbol().call()
        sym1 = e1.functions.symbol().call()
        dec0 = e0.functions.decimals().call()
        dec1 = e1.functions.decimals().call()
        return {"token0": t0, "token1": t1, "fee": fee, "spacing": spacing, "sym0": sym0, "sym1": sym1, "dec0": dec0, "dec1": dec1}

    def vault_state(self) -> Dict[str, Any]:
        pool_addr = self.vault.functions.pool().call()
        token_id = self.vault.functions.positionTokenId().call()
        last_reb = self.vault.functions.lastRebalance().call()
        twap_ok = self.vault.functions.twapOk().call()
        lower, upper, liq = self.vault.functions.currentRange().call()
        return {"pool": pool_addr, "tokenId": token_id, "lower": lower, "upper": upper, "liq": int(liq), "twapOk": bool(twap_ok), "lastRebalance": int(last_reb)}

    def positions(self, token_id: int):
        return self.nfpm.functions.positions(token_id).call()

    def call_static_collect(self, token_id: int, recipient: str) -> Tuple[int,int]:
        # eth_call no collect para prever fees
        (a0, a1) = self.nfpm.functions.collect((token_id, Web3.to_checksum_address(recipient), U128_MAX, U128_MAX)).call()
        return int(a0), int(a1)

    # -------- derived --------

    def amounts_in_position_now(self, lower: int, upper: int, liq: int) -> Tuple[int,int]:
        sqrtP, _ = self.slot0()
        sqrtA = get_sqrt_ratio_at_tick(lower)
        sqrtB = get_sqrt_ratio_at_tick(upper)
        return get_amounts_for_liquidity(sqrtP, sqrtA, sqrtB, liq)
