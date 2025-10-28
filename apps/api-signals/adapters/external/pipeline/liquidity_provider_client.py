import logging
from typing import Any, Dict, Optional

import httpx


class LiquidityProviderClient:
    """
    Minimal HTTP client for the api-liquidity-provider endpoints used by reconciliation.
    """

    def __init__(self, base_url: str, timeout_sec: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._logger = logging.getLogger(self.__class__.__name__)

    async def get_status(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """
        Query LP for current pool/position status related to this strategy (or mapping).
        Adjust the endpoint/path to your api-liquidity-provider.
        """
        url = f"{self._base_url}/strategies/{strategy_id}/status"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json()
                return None
        except Exception as exc:
            self._logger.warning("LP get_status failed: %s", exc)
            return None
