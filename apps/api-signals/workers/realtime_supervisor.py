import asyncio
import logging
import os

from motor.motor_asyncio import AsyncIOMotorClient

from ..adapters.external.binance.binance_websocket_client import BinanceWebsocketClient
from ..adapters.external.database.candle_repository_mongodb import CandleRepositoryMongoDB
from ..adapters.external.database.processing_offset_repository_mongodb import ProcessingOffsetRepositoryMongoDB
from ..core.usecases.start_realtime_ingestion_use_case import StartRealtimeIngestionUseCase


class RealtimeSupervisor:
    """
    Bootstrapper for realtime ingestion: wires dependencies, ensures indexes,
    starts the websocket, and keeps the task alive.
    """

    def __init__(self):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._mongo_client: AsyncIOMotorClient | None = None
        self._db = None
        self._ws_client: BinanceWebsocketClient | None = None
        self._ingestion_use_case: StartRealtimeIngestionUseCase | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        """
        Create connections, ensure indexes, and start realtime ingestion.
        """
        mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        mongodb_db_name = os.getenv("MONGODB_DB_NAME", "signals_db")
        symbol = os.getenv("BINANCE_STREAM_SYMBOL", "ethusdt")
        interval = os.getenv("BINANCE_STREAM_INTERVAL", "1m")

        # Mongo
        self._mongo_client = AsyncIOMotorClient(mongodb_uri)
        self._db = self._mongo_client[mongodb_db_name]

        # Repositories
        candle_repo = CandleRepositoryMongoDB(self._db)
        offset_repo = ProcessingOffsetRepositoryMongoDB(self._db)
        await candle_repo.ensure_indexes()
        await offset_repo.ensure_indexes()

        # WebSocket client
        self._ws_client = BinanceWebsocketClient()

        # Use case
        self._ingestion_use_case = StartRealtimeIngestionUseCase(
            symbol=symbol,
            interval=interval,
            websocket_client=self._ws_client,
            candle_repository=candle_repo,
            processing_offset_repository=offset_repo,
        )

        # Start ingestion (non-blocking; underlying client runs in background)
        await self._ingestion_use_case.execute()
        self._logger.info("Realtime ingestion started for %s@%s", symbol, interval)

        # Keep supervisor alive (optional heartbeat)
        self._task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self):
        """
        Lightweight heartbeat to keep the supervisor task alive and log periodically.
        """
        while True:
            await asyncio.sleep(60)
            self._logger.debug("RealtimeSupervisor heartbeat OK.")

    async def stop(self):
        """
        Gracefully stop resources.
        """
        if self._ws_client:
            await self._ws_client.close()
        if self._mongo_client:
            self._mongo_client.close()
