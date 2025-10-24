from typing import Optional
from pydantic import BaseModel

class SwapQuoteRequest(BaseModel):
    alias: str
    token_in: str             # address
    token_out: str            # address
    amount_in: float          # human
    fee: Optional[int] = None # 500/3000/10000
    sqrt_price_limit_x96: Optional[int] = 0

class SwapExactInRequest(SwapQuoteRequest):
    slippage_bps: int = 50    # 0.50% default