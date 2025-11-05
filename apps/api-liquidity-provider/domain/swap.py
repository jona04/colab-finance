from typing import Optional
from pydantic import BaseModel

class SwapQuoteRequest(BaseModel):
    alias: str
    token_in: str             # address
    token_out: str            # address
    amount_in: float          # human
    fee: Optional[int] = None # 500/3000/10000
    sqrt_price_limit_x96: Optional[int] = 0
    pool_override: Optional[str] = None

class SwapExactInRequest(BaseModel):
    token_in: str
    token_out: str
    amount_in: Optional[float] = None          # valor no token_in (ex.: WETH)
    amount_in_usd: Optional[float] = None      # alternativo: valor em USD/USDC
    fee: Optional[int] = None
    sqrt_price_limit_x96: Optional[int] = None
    slippage_bps: int = 50
    max_budget_usd: Optional[float] = None
    pool_override: Optional[str] = None
    convert_gauge_to_usdc: bool = False