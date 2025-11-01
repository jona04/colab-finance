from typing import Optional, Sequence, Any, Literal
from decimal import Decimal
from web3 import Web3
from web3.contract.contract import ContractFunction
from eth_account import Account
from ..config import get_settings
from .exceptions import TransactionRevertedError


GasStrategy = Literal["default", "buffered", "aggressive"]
# "default": usa estimateGas cru (fallback pro nó)
# "buffered": estimateGas *1.25 + 10k
# "aggressive": estimateGas *1.5 + 25k  (pra retries manuais de cima)


class TxService:
    def __init__(self, rpc_url: str | None = None):
        s = get_settings()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url or s.RPC_URL_DEFAULT))
        self.pk = s.PRIVATE_KEY
        self.account = Account.from_key(self.pk)

    def sender_address(self) -> str:
        return self.account.address

    # ---------------- internal helpers ----------------

    def _next_nonce(self) -> int:
        return self.w3.eth.get_transaction_count(self.account.address)

    def _estimate_with_strategy(
        self,
        tx: dict,
        strategy: GasStrategy
    ) -> int:
        """
        Faz w3.eth.estimate_gas(tx), aplica buffer dependendo da estratégia.
        Se o estimate falhar, tenta um fallback fixo (300k).
        """
        try:
            base_estimate = int(self.w3.eth.estimate_gas(tx))
        except Exception:
            # fallback besta pra não travar completamente
            base_estimate = 300_000

        if strategy == "default":
            return base_estimate

        if strategy == "buffered":
            # 25% a mais + 10k
            return int(base_estimate * 1.25) + 10_000

        if strategy == "aggressive":
            # 50% a mais + 25k
            return int(base_estimate * 1.5) + 25_000

        # fallback caso chegue algo fora do Literal
        return base_estimate

    def _finalize_fee_fields(self, tx: dict) -> dict:
        """
        Se o caller não setou maxFeePerGas / gasPrice etc,
        setamos gasPrice simples (Base é EIP-1559-ish mas aceita gasPrice legacy).
        """
        if "maxFeePerGas" in tx or "maxPriorityFeePerGas" in tx:
            # já está em EIP-1559 mode
            return tx

        if "gasPrice" not in tx:
            tx["gasPrice"] = self.w3.eth.gas_price
        return tx

    def _build_tx_dict(
        self,
        fn: ContractFunction,
        value_wei: int,
    ) -> dict:
        """
        Monta tx básica com from/nonce/value (sem gas ainda).
        """
        base_tx = {
            "from":  self.account.address,
            "nonce": self._next_nonce(),
            "value": int(value_wei or 0),
        }
        return fn.build_transaction(base_tx)

    def _sign_and_send(self, tx: dict) -> str:
        signed = self.w3.eth.account.sign_transaction(tx, self.pk)
        txh = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return txh.hex()

    def _wait_receipt(self, tx_hash: str) -> dict:
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        # cast pra dict simples p/ serializar no retorno
        return dict(rcpt)

    # ---------------- public API ----------------

    def send(
        self,
        fn: ContractFunction,
        *,
        wait: bool = False,
        value: int = 0,
        gas_limit: Optional[int] = None,
        gas_strategy: GasStrategy = "buffered",
    ) -> dict:
        """
        Envia uma tx on-chain chamando uma função do contrato.

        Args:
            fn: função web3 ContractFunction já parametrizada.
            wait: se True, aguarda receipt e valida status.
            value: ETH (wei) a enviar junto.
            gas_limit: se quiser forçar um gas fixo manual.
            gas_strategy:
                - "default": usa estimateGas cru
                - "buffered": estimateGas *1.25 + 10k  (recomendado)
                - "aggressive": estimateGas *1.5 + 25k (retry manual)

        Returns (sempre):
            {
                "tx_hash": "0x...",
                "receipt": {...} | None,
                "gas_limit_used": <int>,
                "status": <None|0|1>  # se wait=True
            }

        Raises:
            TransactionRevertedError se wait=True e receipt.status == 0
        """
        # 1) montar tx base (sem gas)
        tx = self._build_tx_dict(fn, value)

        # 2) determinar gas
        if gas_limit is not None:
            tx["gas"] = int(gas_limit)
        else:
            tx["gas"] = self._estimate_with_strategy(tx, gas_strategy)

        # 3) fee fields (gasPrice / maxFeePerGas / etc)
        tx = self._finalize_fee_fields(tx)

        # 4) assina e manda
        tx_hash = self._sign_and_send(tx)

        if not wait:
            return {
                "tx_hash": tx_hash,
                "receipt": None,
                "gas_limit_used": int(tx["gas"]),
                "status": None,
            }

        # 5) esperar receipt
        rcpt = self._wait_receipt(tx_hash)
        status = int(rcpt.get("status", 0))

        # 6) se revert on-chain => lança exceção específica
        if status == 0:
            # Aqui não escondemos nada: já queimou gas, mas revert.
            raise TransactionRevertedError(
                tx_hash=tx_hash,
                receipt=rcpt,
                msg="Transaction reverted (status=0). Possibly out-of-gas or require() failed"
            )

        # sucesso
        return {
            "tx_hash": tx_hash,
            "receipt": rcpt,
            "gas_limit_used": int(tx["gas"]),
            "status": status,
        }

    def deploy(
        self,
        *,
        abi: list,
        bytecode: str,
        ctor_args: Sequence[Any] = (),
        wait: bool = True,
        gas_limit: Optional[int] = None,
        gas_strategy: GasStrategy = "buffered",
        value: int = 0,
    ) -> dict:
        """
        Faz deploy de um contrato.
        Mesmo modelo de gas_strategy e de erro de revert.
        """
        ContractFactory = self.w3.eth.contract(abi=abi, bytecode=bytecode)

        # build sem gas
        build_tx = ContractFactory.constructor(*list(ctor_args)).build_transaction({
            "from":  self.account.address,
            "nonce": self._next_nonce(),
            "value": int(value or 0),
        })

        # gas
        if gas_limit is not None:
            build_tx["gas"] = int(gas_limit)
        else:
            # reusar mesma lógica de buffer
            try:
                base_estimate = int(self.w3.eth.estimate_gas(build_tx))
            except Exception:
                base_estimate = 500_000
            if gas_strategy == "default":
                build_tx["gas"] = base_estimate
            elif gas_strategy == "buffered":
                build_tx["gas"] = int(base_estimate * 1.25) + 10_000
            else:
                build_tx["gas"] = int(base_estimate * 1.5) + 25_000

        # fee fields
        if "gasPrice" not in build_tx and "maxFeePerGas" not in build_tx:
            build_tx["gasPrice"] = self.w3.eth.gas_price

        # send
        signed = self.w3.eth.account.sign_transaction(build_tx, self.pk)
        txh = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash = txh.hex()

        if not wait:
            return {"tx": tx_hash, "address": None, "gas_limit_used": int(build_tx["gas"]), "status": None}

        rcpt = dict(self.w3.eth.wait_for_transaction_receipt(txh))
        status = int(rcpt.get("status", 0))

        if status == 0:
            raise TransactionRevertedError(
                tx_hash=tx_hash,
                receipt=rcpt,
                msg="Deploy reverted (status=0)"
            )

        return {
            "tx": tx_hash,
            "address": rcpt["contractAddress"],
            "gas_limit_used": int(build_tx["gas"]),
            "status": status,
        }
