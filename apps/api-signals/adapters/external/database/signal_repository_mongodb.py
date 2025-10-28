import time
from datetime import datetime, timezone
from typing import Dict

from motor.motor_asyncio import AsyncIOMotorDatabase

from ....core.repositories.signal_repository import SignalRepository  # sua interface existente


class SignalRepositoryMongoDB(SignalRepository):
    """
    Mongo implementation for signal logs (PENDING -> SENT).
    """

    COLLECTION = "signals"

    def __init__(self, db: AsyncIOMotorDatabase):
        self._col = db[self.COLLECTION]

    async def ensure_indexes(self) -> None:
        await self._col.create_index(
            [("strategy_id", 1), ("ts", 1), ("signal_type", 1)],
            unique=True,
            name="ux_strategy_ts_type",
        )
        await self._col.create_index([("status", 1), ("created_at", -1)], name="ix_status_created_at")

    async def upsert_signal(self, doc: Dict) -> None:
        now_ms = int(time.time() * 1000)
        now_iso = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        key = {"strategy_id": doc["strategy_id"], "ts": doc["ts"], "signal_type": doc["signal_type"]}
        update = {
            "$set": {**doc, "updated_at": now_ms},
            "$setOnInsert": {"created_at": now_ms, "created_at_iso": now_iso},
        }
        await self._col.update_one(key, update, upsert=True)
