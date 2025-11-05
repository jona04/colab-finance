from pydantic import BaseModel, Field, validator
from typing import Optional, Literal, List, Dict, Any

DexName = Literal["uniswap", "aerodrome"]

class VaultRow(BaseModel):
    alias: str
    address: str
    pool: Optional[str] = None
    nfpm: Optional[str] = None
    rpc_url: Optional[str] = None
    dex: DexName = "uniswap"

class VaultList(BaseModel):
    active: Optional[str] = None
    vaults: List[VaultRow] = []

class AddVaultRequest(BaseModel):
    alias: str
    address: str
    dex: DexName = "uniswap"
    pool: Optional[str] = None
    nfpm: Optional[str] = None
    rpc_url: Optional[str] = None

class SetPoolRequest(BaseModel):
    alias: str
    pool: str

class SwapPoolRef(BaseModel):
    dex: Literal["uniswap", "aerodrome", "pancake"]
    pool: str
    
class DeployVaultRequest(BaseModel):
    alias: str
    nfpm: str
    pool: str
    rpc_url: Optional[str] = None
    dex: Literal["uniswap", "aerodrome", "pancake"]
    version: Literal["v1","v2"] = "v2"
    owner: Optional[str] = None            # se None, usamos SENDER_FROM_ENV do TxService
    gauge: Optional[str] = None           
    swap_pools: Optional[Dict[str, SwapPoolRef]] = None
    
class OpenRequest(BaseModel):
    # modo ticks
    lower_tick: Optional[int] = None
    upper_tick: Optional[int] = None

    # modo preço direto (token1 per token0)
    lower_price: Optional[float] = None  # alvo p_t1_t0 para bound inferior
    upper_price: Optional[float] = None  # alvo p_t1_t0 para bound superior

    max_budget_usd: Optional[float] = None

class RebalanceRequest(BaseModel):
    # modo ticks
    lower_tick: Optional[int] = None
    upper_tick: Optional[int] = None

    # modo preço direto (token1 per token0)
    lower_price: Optional[float] = None  # alvo p_t1_t0 para bound inferior
    upper_price: Optional[float] = None  # alvo p_t1_t0 para bound superior

    # caps (human units)
    cap0: Optional[float] = None
    cap1: Optional[float] = None

class WithdrawRequest(BaseModel):
    alias: str
    mode: Literal["pool", "all"]
    max_budget_usd: Optional[float] = None
    
class DepositRequest(BaseModel):
    alias: str
    token: str
    amount: float  # human

class CollectRequest(BaseModel):
    alias: str
    max_budget_usd: Optional[float] = None

class BaselineRequest(BaseModel):
    alias: str
    action: Literal["set", "show"] = "show"








# Vault status


class PricesBlock(BaseModel):
    tick: int
    p_t1_t0: float
    p_t0_t1: float
    
class PricesPanel(BaseModel):
    current: PricesBlock
    lower: PricesBlock
    upper: PricesBlock

class UsdPanelModel(BaseModel):
    usd_value: float
    delta_usd: float
    baseline_usd: float

class HoldingsSide(BaseModel):
    token0: float
    token1: float
    usd: float

class HoldingsMeta(BaseModel):
    token0: int
    token1: int
    
class HoldingsBlock(BaseModel):
    vault_idle: HoldingsSide
    in_position: HoldingsSide
    totals: HoldingsSide
    decimals: HoldingsMeta
    symbols: Dict[str, str]     # {"token0": "WETH", "token1": "USDC"}
    addresses: Dict[str, str]   # {"token0": "0x...", "token1": "0x..."}

class FeesCollectedCum(BaseModel):
    """Cumulative fees that were already collected historically (persisted off-chain)."""
    token0_raw: int
    token1_raw: int
    token0: float
    token1: float
    usd: float
    
class FeesUncollected(BaseModel):
    token0: float
    token1: float
    usd: float
    sym0: str
    sym1: str
    

class RewardsCollectedCum(BaseModel):
    usdc_raw: int
    usdc: float


class StatusCore(BaseModel):
    tick: int
    lower: int
    upper: int
    spacing: int
    twap_ok: bool
    last_rebalance: int
    cooldown_remaining_seconds: int
    cooldown_active: bool
    prices: PricesPanel
    gauge_rewards: Optional[dict]
    gauge_reward_balances: Optional[dict] = None
    rewards_collected_cum: RewardsCollectedCum = RewardsCollectedCum(usdc_raw=0, usdc=0.0)
    fees_uncollected: FeesUncollected
    fees_collected_cum: FeesCollectedCum
    out_of_range: bool
    pct_outside_tick: float
    usd_panel: UsdPanelModel
    range_side: Literal["inside","below","above"]
    sym0: str
    sym1: str
    holdings: HoldingsBlock

    has_gauge: bool = False
    gauge: Optional[str] = None
    staked: Optional[bool] = None
    position_location: Literal["none", "pool", "gauge"] = "none"


class StatusResponse(StatusCore):
    alias: str
    vault: str
    pool: str

class StakeRequest(BaseModel):
    """Stake the current or a specific position tokenId into the gauge."""
    token_id: Optional[int] = Field(default=None, description="If omitted, uses vault.positionTokenId()")
    max_budget_usd: Optional[float] = None

class UnstakeRequest(BaseModel):
    """Unstake a specific (or the current) position tokenId from the gauge."""
    token_id: Optional[int] = Field(default=None, description="If omitted, uses vault.positionTokenId()")
    max_budget_usd: Optional[float] = None

class ClaimRewardsRequest(BaseModel):
    """
    Claim gauge rewards.
    mode='tokenId' will call getReward(tokenId) (default). 
    mode='account' will call getReward(account).
    """
    mode: Literal["tokenId", "account"] = "tokenId"
    token_id: Optional[int] = None
    account: Optional[str] = None
    max_budget_usd: Optional[float] = None