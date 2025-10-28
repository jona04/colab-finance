# apps/api-signals/adapters/entry/http/admin_router.py
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from motor.motor_asyncio import AsyncIOMotorDatabase

from ....adapters.entry.http.deps import get_db

from ...external.database.indicator_set_repository_mongodb import IndicatorSetRepositoryMongoDB
from ...external.database.strategy_repository_mongodb import StrategyRepositoryMongoDB

router = APIRouter(prefix="/admin", tags=["admin"])


class StrategyCreateDTO(BaseModel):
    name: str
    symbol: str
    ema_fast: int
    ema_slow: int
    atr_window: int
    params: Dict

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.upper()


@router.post("/strategies")
async def create_strategy(dto: StrategyCreateDTO, db: AsyncIOMotorDatabase = Depends(get_db)):
    """
    Create or update a Strategy and its Indicator Set.

    Uses app.state.db (set in lifespan) to access Mongo.
    Upserts the indicator_set (dedup by tuple) and then the strategy.
    """

    indset_repo = IndicatorSetRepositoryMongoDB(db)
    set_doc = await indset_repo.upsert_active({
        "symbol": dto.symbol,
        "ema_fast": dto.ema_fast,
        "ema_slow": dto.ema_slow,
        "atr_window": dto.atr_window,
        "status": "ACTIVE",
    })
    if not set_doc:
        raise HTTPException(status_code=500, detail="Failed to upsert indicator set")

    strat_repo = StrategyRepositoryMongoDB(db)
    stored = await strat_repo.upsert({
        "name": dto.name,
        "symbol": dto.symbol,
        "status": "ACTIVE",
        "indicator_set_id": set_doc["cfg_hash"],  # usando cfg_hash como id lógico do set
        "cfg_hash": set_doc["cfg_hash"],
        "params": dto.params,
    })
    if not stored:
        raise HTTPException(status_code=500, detail="Failed to upsert strategy")

    return {"ok": True, "strategy": stored, "indicator_set": set_doc}
