import logging
from typing import Any, Dict, Optional

import httpx


class PipelineHttpClient:
    """
    Thin async HTTP wrapper around the vault endpoints exposed by api-liquidity-provider.

    All URLs are:
      {base_url}/api/vaults/{dex}/{alias}/...

    This client does *no* strategy logic, only raw HTTP.
    """

    def __init__(self, base_url: str, timeout_sec: float = 25.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._logger = logging.getLogger(self.__class__.__name__)

    async def get_status(self, dex: str, alias: str) -> Optional[Dict[str, Any]]:
        url = f"{self._base_url}/api/vaults/{dex}/{alias}/status"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json()
                self._logger.warning("status non-200 %s: %s %s", url, r.status_code, r.text)
        except Exception as exc:
            self._logger.exception("get_status error for %s: %s", url, exc)
        return None

    async def post_collect(self, dex: str, alias: str) -> Optional[Dict[str, Any]]:
        """
        POST /api/vaults/{dex}/{alias}/collect
        body: { "alias": <alias> }
        """
        url = f"{self._base_url}/api/vaults/{dex}/{alias}/collect"
        payload = {"alias": alias}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return r.json()
                self._logger.warning("collect non-200 %s: %s %s", url, r.status_code, r.text)
        except Exception as exc:
            self._logger.exception("post_collect error for %s: %s", url, exc)
        return None

    async def post_withdraw(self, dex: str, alias: str, mode: str = "pool") -> Optional[Dict[str, Any]]:
        """
        POST /api/vaults/{dex}/{alias}/withdraw
        body: { "alias": <alias>, "mode": "pool" }
        """
        url = f"{self._base_url}/api/vaults/{dex}/{alias}/withdraw"
        payload = {"alias": alias, "mode": mode}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return r.json()
                self._logger.warning("withdraw non-200 %s: %s %s", url, r.status_code, r.text)
        except Exception as exc:
            self._logger.exception("post_withdraw error for %s: %s", url, exc)
        return None

    async def post_swap_exact_in(
        self,
        dex: str,
        alias: str,
        token_in: str,
        token_out: str,
        amount_in: Optional[float] = None,
        amount_in_usd: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        POST /api/vaults/{dex}/{alias}/swap/exact-in
        body:
        {
          "token_in": "...",
          "token_out": "...",
          "amount_in": float,
          "amount_in_usd": float
        }
        """
        url = f"{self._base_url}/api/vaults/{dex}/{alias}/swap/exact-in"
        payload = {
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": amount_in,
            "amount_in_usd": amount_in_usd,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return r.json()
                self._logger.warning("swap non-200 %s: %s %s", url, r.status_code, r.text)
        except Exception as exc:
            self._logger.exception("post_swap_exact_in error for %s: %s", url, exc)
        return None

    async def post_rebalance(
        self,
        dex: str,
        alias: str,
        lower_price: float,
        upper_price: float,
        cap0: Optional[float] = None,
        cap1: Optional[float] = None,
        lower_tick: Optional[int] = None,
        upper_tick: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        POST /api/vaults/{dex}/{alias}/rebalance
        body:
        {
          "lower_tick": int,
          "upper_tick": int,
          "lower_price": float,
          "upper_price": float,
          "cap0": float,
          "cap1": float
        }

        We send ticks if we know them; otherwise 0 and let the provider compute.
        """
        url = f"{self._base_url}/api/vaults/{dex}/{alias}/rebalance"
        payload = {
            "lower_tick": lower_tick if lower_tick is not None else 0,
            "upper_tick": upper_tick if upper_tick is not None else 0,
            "lower_price": lower_price,
            "upper_price": upper_price,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    return r.json()
                self._logger.warning("rebalance non-200 %s: %s %s", url, r.status_code, r.text)
        except Exception as exc:
            self._logger.exception("post_rebalance error for %s: %s", url, exc)
        return None
