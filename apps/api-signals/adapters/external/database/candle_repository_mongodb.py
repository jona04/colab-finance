import time
from typing import Dict

from motor.motor_asyncio import AsyncIOMotorDatabase

from ....core.repositories.candle_repository import CandleRepository


class CandleRepositoryMongoDB(CandleRepository):
    """
    MongoDB implementation for CandleRepository using Motor.
    """

    COLLECTION_NAME = "candles_1m"

    def __init__(self, db: AsyncIOMotorDatabase):
        """
        :param db: Motor async database instance.
        """
        self._db = db
        self._collection = self._db[self.COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        """
        Create a unique compound index on (symbol, interval, open_time) for idempotency.
        """
        await self._collection.create_index(
            [("symbol", 1), ("interval", 1), ("open_time", 1)],
            unique=True,
            name="ux_symbol_interval_open_time",
        )

    async def upsert_closed_candle(self, candle_doc: Dict) -> None:
        """
        Upsert the closed candle. Uses (symbol, interval, open_time) as the unique key.

        Adds/refreshes updated_at; sets created_at on insert.
        """
        now_ms = int(time.time() * 1000)
        key = {
            "symbol": candle_doc["symbol"],
            "interval": candle_doc["interval"],
            "open_time": candle_doc["open_time"],
        }
        update = {
            "$set": {
                "close_time": candle_doc["close_time"],
                "open": candle_doc["open"],
                "high": candle_doc["high"],
                "low": candle_doc["low"],
                "close": candle_doc["close"],
                "volume": candle_doc["volume"],
                "trades": candle_doc["trades"],
                "is_closed": True,
                "updated_at": now_ms,
            },
            "$setOnInsert": {"created_at": now_ms},
        }
        await self._collection.update_one(key, update, upsert=True)
