"""
Tx service: sign & send transactions directly via web3.py.
Replaces forge-based execution.
"""

from typing import Optional, Sequence, Any
from web3 import Web3
from web3.contract.contract import ContractFunction
from eth_account import Account
from ..config import get_settings

class TxService:
    def __init__(self, rpc_url: str | None = None):
        s = get_settings()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url or s.RPC_URL_DEFAULT))
        self.pk = s.PRIVATE_KEY
        self.account = Account.from_key(self.pk)
        
    def _base_tx(self) -> dict:
        addr = self.account.address
        nonce = self.w3.eth.get_transaction_count(addr)
        return {"from": addr, "nonce": nonce}

    def _finalize_gas(self, tx: dict) -> dict:
        # Try to estimate gas; otherwise rely on node defaults
        if "gas" not in tx:
            try:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            except Exception:
                pass
        if "maxFeePerGas" not in tx and "gasPrice" not in tx:
            tx["gasPrice"] = self.w3.eth.gas_price
        return tx

    def sender_address(self) -> str:
        return self.account.address
    
    def send(
        self,
        fn: ContractFunction,
        gas: Optional[int] = None,
        value: int = 0,
        wait: bool = False,
    ) -> dict:
        """
        Execute contract function.
        Returns:
          {
            "tx_hash": "0x...",
            "receipt": {...} OR None if wait=False
          }
        """
        tx = fn.build_transaction({**self._base_tx(), "value": value})
        if gas:
            tx["gas"] = gas
        tx = self._finalize_gas(tx)

        signed = self.w3.eth.account.sign_transaction(tx, self.pk)
        txh = self.w3.eth.send_raw_transaction(signed.raw_transaction)

        if not wait:
            return {
                "tx_hash": txh.hex(),
                "receipt": None,
            }

        rcpt = self.w3.eth.wait_for_transaction_receipt(txh)
        # rcpt tem gasUsed, status, etc.
        return {
            "tx_hash": txh.hex(),
            "receipt": dict(rcpt),
        }

    def deploy(self, abi: list, bytecode: str, ctor_args: Sequence[Any] = (), gas: Optional[int]=None, wait: bool=True) -> dict:
        """
        Deploy a contract using ABI + bytecode.
        Returns {"tx": <hash>, "address": <deployed_address>} (when wait=True).
        """
        ContractFactory = self.w3.eth.contract(abi=abi, bytecode=bytecode)
        build_tx = ContractFactory.constructor(*list(ctor_args)).build_transaction(self._base_tx())
        if gas: build_tx["gas"] = gas
        build_tx = self._finalize_gas(build_tx)
        signed = self.w3.eth.account.sign_transaction(build_tx, self.pk)
        txh = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        if not wait:
            return {"tx": txh.hex(), "address": None}
        rcpt = self.w3.eth.wait_for_transaction_receipt(txh)
        return {"tx": txh.hex(), "address": rcpt.contractAddress}
    