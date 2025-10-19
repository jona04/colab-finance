
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List

class StrategyParams(BaseModel):
    """User-facing parameters for a strategy."""
    minimum_minutes_out_of_range: Optional[int] = 10
    min_ticks_from_price_on_near_side: Optional[int] = 1
    breakeven_buffer_pct: Optional[float] = 0.0
    upper_gap_usdc_per_eth_pct: Optional[float] = 0.01
    max_opposite_side_expansions: Optional[int] = 100
    max_near_side_expansions: Optional[int] = 600

class StrategyConfig(BaseModel):
    """Single strategy configuration item."""
    id: str
    name: Optional[str] = None
    active: bool = True
    description: Optional[str] = None
    notes: Optional[str] = None
    params: StrategyParams = Field(default_factory=StrategyParams)

class StrategiesConfig(BaseModel):
    """List wrapper for strategies of a vault."""
    strategies: List[StrategyConfig] = Field(default_factory=list)

class StrategyDetails(BaseModel):
    """Opaque details blob returned by handlers (safe to pass-through to UI)."""
    ticks: Optional[Dict[str, int]] = None
    prices: Optional[Dict[str, Any]] = None
    breakeven: Optional[Dict[str, Any]] = None

class StrategyProposal(BaseModel):
    """Normalized output from the engine/handlers."""
    trigger: bool
    reason: Optional[str] = None
    action: Optional[str] = None  # e.g., "reallocate"
    id: Optional[str] = None      # strategy id (handler key)
    name: Optional[str] = None
    lower: Optional[int] = None
    upper: Optional[int] = None
    range_side: Optional[str] = None
    details: Optional[StrategyDetails] = None

class ProposalsResponse(BaseModel):
    alias: str
    vault: str
    pool: str
    proposals: List[StrategyProposal]

class StrategyExecuteRequest(BaseModel):
    """Call to execute a previously suggested plan (or “force plan”)."""
    id: str = Field(..., description="Strategy id to execute (handler key).")
    lower: int
    upper: int
    cap0: Optional[float] = None
    cap1: Optional[float] = None
    dry_run: Optional[bool] = False