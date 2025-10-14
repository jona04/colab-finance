from web3 import Web3
from typing import Tuple, Dict, Any, Optional
from .base import DexAdapter

class AerodromeAdapter(DexAdapter):
    """
    Placeholder for Aerodrome v3/v2 style.
    Implement ABIs and the same surface used in UniswapV3Adapter.
    """

    def pool_abi(self) -> list: 
        # TODO: add Aerodrome pool ABI
        return []

    def erc20_abi(self) -> list:
        # reuse standard ERC20 (same as Uniswap adapter if you prefer)
        return [{"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
                {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
                {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
                {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]

    def nfpm_abi(self) -> list:
        # likely not used (different position manager model)
        return []

    def vault_abi(self) -> list:
        # TODO: mirror SingleUserVault equivalent for Aerodrome or your new contract
        return []

    def pool_contract(self):
        # TODO
        raise NotImplementedError

    def nfpm_contract(self):
        return None

    def slot0(self) -> Tuple[int, int]:
        # TODO: implement price/tick equiv or adapter mapping
        raise NotImplementedError

    def observe_twap_tick(self, window: int) -> int:
        # TODO
        raise NotImplementedError

    def pool_meta(self) -> Dict[str, Any]:
        # TODO
        raise NotImplementedError

    def vault_state(self) -> Dict[str, Any]:
        # TODO
        raise NotImplementedError

    def amounts_in_position_now(self, lower: int, upper: int, liq: int):
        # TODO
        raise NotImplementedError

    def call_static_collect(self, token_id: int, recipient: str):
        # TODO
        raise NotImplementedError

    # writes
    def fn_open(self, lower: int, upper: int):
        # TODO
        raise NotImplementedError

    def fn_rebalance_caps(self, lower: int, upper: int, cap0_raw: Optional[int], cap1_raw: Optional[int]):
        # TODO
        raise NotImplementedError

    def fn_exit(self):
        # TODO
        raise NotImplementedError

    def fn_exit_withdraw(self):
        # TODO
        raise NotImplementedError

    def fn_collect(self):
        # TODO
        raise NotImplementedError

    def fn_deposit_erc20(self, token: str, amount_raw: int):
        # TODO
        raise NotImplementedError

    def fn_deploy_vault(self, nfpm: str):
        # TODO
        raise NotImplementedError
