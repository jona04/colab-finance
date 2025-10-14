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

class DeployVaultRequest(BaseModel):
    alias: str
    nfpm: str
    pool: Optional[str] = None
    rpc_url: Optional[str] = None
    dex: DexName = "uniswap"

class OpenRequest(BaseModel):
    alias: str
    lower: int
    upper: int

class RebalanceRequest(BaseModel):
    alias: str
    lower: int
    upper: int
    # when provided, service will convert human -> raw using pool decimals
    cap0: Optional[float] = None
    cap1: Optional[float] = None

class WithdrawRequest(BaseModel):
    alias: str
    mode: Literal["pool", "all"]

class DepositRequest(BaseModel):
    alias: str
    token: str
    amount: float  # human

class CollectRequest(BaseModel):
    alias: str

class BaselineRequest(BaseModel):
    alias: str
    action: Literal["set", "show"] = "show"

class StatusResponse(BaseModel):
    alias: str
    vault: str
    pool: Optional[str]
    tick: int
    lower: int
    upper: int
    spacing: int
    prices: Dict[str, Any]
    fees_uncollected: Dict[str, Any]
    out_of_range: bool
    pct_outside_tick: float
    usd_panel: Dict[str, float]
    range_side: Literal["inside", "below", "above"]

class StakeRequest(BaseModel):
    """Stake the current or a specific position tokenId into the gauge."""
    token_id: Optional[int] = Field(default=None, description="If omitted, uses vault.positionTokenId()")

class UnstakeRequest(BaseModel):
    """Unstake a specific (or the current) position tokenId from the gauge."""
    token_id: Optional[int] = Field(default=None, description="If omitted, uses vault.positionTokenId()")

class ClaimRewardsRequest(BaseModel):
    """
    Claim gauge rewards.
    mode='tokenId' will call getReward(tokenId) (default). 
    mode='account' will call getReward(account).
    """
    mode: Literal["tokenId", "account"] = "tokenId"
    token_id: Optional[int] = None
    account: Optional[str] = None