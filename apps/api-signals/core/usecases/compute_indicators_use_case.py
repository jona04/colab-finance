import logging
from typing import Optional

from ..repositories.candle_repository import CandleRepository
from ..repositories.indicator_repository import IndicatorRepository
from ..services.indicator_calculation_service import IndicatorCalculationService


class ComputeIndicatorsUseCase:
    """
    Use case that, on each closed candle, loads the last required bars and computes
    EMA fast/slow and ATR% for the last candle; then upserts an indicator snapshot.
    """

    def __init__(
        self,
        candle_repository: CandleRepository,
        indicator_repository: IndicatorRepository,
        indicator_service: IndicatorCalculationService,
        ema_fast: int,
        ema_slow: int,
        atr_window: int,
        logger: logging.Logger | None = None,
    ):
        """
        :param candle_repository: Candle repository.
        :param indicator_repository: Indicator repository.
        :param indicator_service: Indicator calculation helper.
        :param ema_fast: EMA fast period (bars).
        :param ema_slow: EMA slow period (bars).
        :param atr_window: ATR window (bars).
        :param logger: Optional logger.
        """
        self._candle_repo = candle_repository
        self._indicator_repo = indicator_repository
        self._svc = indicator_service
        self._ema_fast = int(ema_fast)
        self._ema_slow = int(ema_slow)
        self._atr_window = int(atr_window)
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    @property
    def required_bars(self) -> int:
        """
        Return the number of bars required to compute all indicators reliably.
        """
        return max(self._ema_slow, self._atr_window)

    async def execute_for_symbol_interval(self, symbol: str, interval: str) -> Optional[dict]:
        """
        Load the last required candles, compute indicators for the last bar
        and persist the snapshot. If not enough data, do nothing.

        :param symbol: Trading symbol, e.g. 'ETHUSDT'.
        :param interval: Interval string, e.g. '1m'.
        :return: The snapshot dict persisted, or None if skipped.
        """
        candles = await self._candle_repo.get_last_n_closed(symbol, interval, self.required_bars)
        snapshot = self._svc.compute_snapshot_for_last(
            candles,
            ema_fast=self._ema_fast,
            ema_slow=self._ema_slow,
            atr_window=self._atr_window,
        )
        if snapshot is None:
            self._logger.debug(
                "Not enough candles for indicators: have=%s need=%s",
                len(candles), self.required_bars
            )
            return None

        await self._indicator_repo.upsert_snapshot(snapshot)
        self._logger.debug(
            "Indicator snapshot upserted: symbol=%s ts=%s ema_fast=%.6f ema_slow=%.6f atr_pct=%.6f",
            snapshot["symbol"], snapshot["ts"],
            snapshot["ema_fast"], snapshot["ema_slow"], snapshot["atr_pct"]
        )
        return snapshot
