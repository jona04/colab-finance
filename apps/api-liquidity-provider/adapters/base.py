from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple, Optional
from web3 import Web3

class DexAdapter(ABC):
    """
    Abstract adapter that normalizes read/write ops across DEXes.
    One concrete instance per-vault (it can capture rpc_url, pool, nfpm, vault).
    """

    def __init__(self, w3: Web3, pool: str, nfpm: Optional[str], vault: str, gauge: Optional[str] = None):
        self.w3 = w3
        self.pool = Web3.to_checksum_address(pool) if pool else None
        self.nfpm = Web3.to_checksum_address(nfpm) if nfpm else None
        self.vault = self.w3.eth.contract(address=Web3.to_checksum_address(vault), abi=self.vault_abi())
        self.gauge = Web3.to_checksum_address(gauge) if gauge else None

    # ---------- ABI providers ----------
    @abstractmethod
    def pool_abi(self) -> list: ...
    @abstractmethod
    def erc20_abi(self) -> list: ...
    @abstractmethod
    def nfpm_abi(self) -> list: ...
    @abstractmethod
    def vault_abi(self) -> list: ...

    # ---------- Read ----------
    @abstractmethod
    def pool_contract(self):
        ...

    @abstractmethod
    def nfpm_contract(self):
        ...

    def erc20(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=self.erc20_abi())

    @abstractmethod
    def slot0(self) -> Tuple[int, int]: ...

    @abstractmethod
    def observe_twap_tick(self, window: int) -> int: ...

    @abstractmethod
    def pool_meta(self) -> Dict[str, Any]: ...

    @abstractmethod
    def vault_state(self) -> Dict[str, Any]: ...

    @abstractmethod
    def amounts_in_position_now(self, lower: int, upper: int, liq: int) -> Tuple[int, int]: ...

    @abstractmethod
    def call_static_collect(self, token_id: int, recipient: str) -> Tuple[int, int]: ...

    # ---------- Write (build tx function calls) ----------
    @abstractmethod
    def fn_open(self, lower: int, upper: int):
        """Return a ContractFunction to open initial position."""
        ...

    @abstractmethod
    def fn_rebalance_caps(self, lower: int, upper: int, cap0_raw: Optional[int], cap1_raw: Optional[int]):
        """Return a ContractFunction to rebalance (with caps, no swaps)."""
        ...

    @abstractmethod
    def fn_exit(self):
        """Exit to vault (remove liquidity, keep funds in vault)."""
        ...

    @abstractmethod
    def fn_exit_withdraw(self):
        """Exit position and withdraw all to owner."""
        ...

    @abstractmethod
    def fn_collect(self):
        """Collect fees to vault."""
        ...

    @abstractmethod
    def fn_deposit_erc20(self, token: str, amount_raw: int):
        """Simple ERC20 transfer to vault or vault.deposit(token,amount) if available."""
        ...

    # @abstractmethod
    def fn_deploy_vault(self, nfpm: str):
        """If deployment is performed via a factory, implement here.
        Otherwise return NotImplementedError and keep route disabled."""
        ...

    def vault_constraints(self):
        """Return optional constraints if the vault implements them."""
        out = {}
        try: out["twapOk"] = bool(self.vault.functions.twapOk().call())
        except: pass
        try: out["minWidth"] = int(self.vault.functions.minWidth().call())
        except: pass
        try: out["maxWidth"] = int(self.vault.functions.maxWidth().call())
        except: pass
        try: out["minCooldown"] = int(self.vault.functions.minCooldown().call())
        except: pass
        try: out["lastRebalance"] = int(self.vault.functions.lastRebalance().call())
        except: pass
        try: out["owner"] = self.vault.functions.owner().call()
        except: pass
        try: out["tickSpacing"] = int(self.pool_contract().functions.tickSpacing().call())
        except: pass
        return out
    
    def vault_idle_balances(self):
        meta = self.pool_meta()
        e0 = self.erc20(meta["token0"]); e1 = self.erc20(meta["token1"])
        b0 = int(e0.functions.balanceOf(self.vault.address).call())
        b1 = int(e1.functions.balanceOf(self.vault.address).call())
        return b0, b1, meta