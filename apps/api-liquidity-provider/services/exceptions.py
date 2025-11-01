class TransactionRevertedError(RuntimeError):
    """
    Raised when the transaction was submitted on-chain,
    mined, but `status == 0` (revert / out-of-gas / require failed).
    Includes tx hash and optional receipt info.
    """
    def __init__(self, tx_hash: str, receipt: dict | None, msg: str = "Transaction reverted on-chain"):
        self.tx_hash = tx_hash
        self.receipt = receipt or {}
        super().__init__(f"{msg}. tx={tx_hash}")
