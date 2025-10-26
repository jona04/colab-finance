import asyncio
import json
import logging
import random
from typing import Awaitable, Callable, Optional

import websockets


class BinanceWebsocketClient:
    """
    Minimal native WebSocket client for Binance public kline_1m stream.

    - Connects to: wss://stream.binance.com:9443/ws/{symbol}@kline_1m
    - Calls the async callback ONLY when the kline is CLOSED (kline.x == true).
    - Handles reconnect with exponential backoff + jitter.
    """

    def __init__(self, base_ws_url: str = "wss://stream.binance.com:9443"):
        """
        :param base_ws_url: Binance base WebSocket URL.
        """
        self._logger = logging.getLogger(self.__class__.__name__)
        self._base_ws_url = base_ws_url.rstrip("/")
        self._symbol: Optional[str] = None
        self._on_kline_closed: Optional[Callable[[dict], Awaitable[None]]] = None
        self._stop_event = asyncio.Event()
        self._runner_task: Optional[asyncio.Task] = None

    async def subscribe_kline_1m(self, symbol: str, on_kline_closed: Callable[[dict], Awaitable[None]]):
        """
        Start background task to consume {symbol}@kline_1m and dispatch closed candles.

        :param symbol: Trading symbol (e.g., 'ethusdt').
        :param on_kline_closed: Async callback receiving the full event dict.
        """
        if self._runner_task and not self._runner_task.done():
            self._logger.info("WebSocket already running; ignoring duplicate subscribe.")
            return

        self._symbol = symbol.lower()
        self._on_kline_closed = on_kline_closed
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._run_loop())
        self._logger.info("WS runner started for %s@kline_1m", self._symbol)

    async def close(self):
        """
        Signal the background task to stop and wait for completion.
        """
        self._stop_event.set()
        if self._runner_task:
            try:
                await asyncio.wait_for(self._runner_task, timeout=5)
            except asyncio.TimeoutError:
                self._logger.warning("Timeout waiting WS runner to stop; cancelling task.")
                self._runner_task.cancel()
            finally:
                self._runner_task = None

    async def _run_loop(self):
        """
        Reconnect loop with exponential backoff + jitter.
        """
        assert self._symbol is not None
        url = f"{self._base_ws_url}/ws/{self._symbol}@kline_1m"

        backoff = 1
        backoff_max = 30

        while not self._stop_event.is_set():
            try:
                self._logger.info("Connecting WS: %s", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._logger.info("WS connected: %s", url)
                    backoff = 1  # reset on successful connect

                    async for message in ws:
                        if self._stop_event.is_set():
                            break
                        await self._handle_message(message)

            except Exception as exc:
                # Connection dropped or parse failure — backoff and retry
                self._logger.warning("WS error: %s. Reconnecting...", exc)
                jitter = random.uniform(0, 0.5)
                sleep_for = min(backoff, backoff_max) + jitter
                await asyncio.sleep(sleep_for)
                backoff = min(backoff * 2, backoff_max)

    async def _handle_message(self, message: str):
        """
        Parse an incoming WS message and dispatch closed kline to the callback.
        """
        try:
            payload = json.loads(message)
            k = payload.get("k")
            if not k:
                return
            # Only closed candles
            if k.get("x") is True and self._on_kline_closed is not None:
                await self._on_kline_closed(payload)
        except Exception as exc:
            self._logger.exception("Error handling WS message: %s", exc)
