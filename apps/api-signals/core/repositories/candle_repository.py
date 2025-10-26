from abc import ABC, abstractmethod
from typing import Dict


class CandleRepository(ABC):
    """
    Repository interface for persisting and querying closed candles.
    """

    @abstractmethod
    async def ensure_indexes(self) -> None:
        """
        Ensure the collection has the proper indexes (e.g., unique compound index).
        """
        raise NotImplementedError

    @abstractmethod
    async def upsert_closed_candle(self, candle_doc: Dict) -> None:
        """
        Upsert a closed 1m candle using a unique key (symbol, interval, open_time).

        :param candle_doc: Document containing candle fields already normalized.
        """
        raise NotImplementedError
