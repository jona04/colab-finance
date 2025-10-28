from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class SignalRepository(ABC):

    @abstractmethod
    async def ensure_indexes(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def upsert_signal(self, doc: Dict) -> None:
        raise NotImplementedError
