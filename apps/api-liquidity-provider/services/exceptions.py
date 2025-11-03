
class TransactionRevertedError(Exception):
    """
    Raised when the tx was actually sent on-chain, mined, and status == 0.
    You ALREADY paid gas, the chain executed and reverted.
    """
    def __init__(self, tx_hash: str, receipt: dict, msg: str, budget_block: dict):
        super().__init__(msg)
        self.tx_hash = tx_hash
        self.receipt = receipt
        self.msg = msg
        self.budget_block = budget_block


class TransactionBudgetExceededError(Exception):
    """
    Raised BEFORE broadcasting the tx if the predicted max gas cost
    (gas_limit * gas_price * eth_usd) is above caller's budget.
    This means: nothing was sent on-chain yet.
    """
    def __init__(self, est_gas_limit: int, gas_price_wei: int, eth_usd: float, usd_estimated: float, usd_budget: float):
        super().__init__("Gas budget exceeded")
        self.est_gas_limit = est_gas_limit
        self.gas_price_wei = gas_price_wei
        self.eth_usd = eth_usd
        self.usd_estimated = usd_estimated
        self.usd_budget = usd_budget