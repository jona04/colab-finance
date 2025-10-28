from typing import Dict, Optional

from ...adapters.external.pipeline.liquidity_provider_client import LiquidityProviderClient


class StrategyReconcilerService:
    """
    Compares desired episode state with actual LP state and decides if a signal must be emitted.
    Produces high-level intent signals (OPEN_NEW_RANGE, REBALANCE_TO_RANGE, CLOSE_RANGE).
    """

    def __init__(self, lp_client: LiquidityProviderClient):
        self._lp = lp_client

    async def reconcile(self, strategy_id: str, desired: Dict, symbol: str) -> Optional[Dict]:
        """
        Compare 'desired' episode (OPEN) with LP. Return a signal dict or None.

        :param strategy_id: Strategy identifier.
        :param desired: Current OPEN episode doc (desired state).
        :param symbol: Trading symbol.
        """
        lp = await self._lp.get_status(strategy_id)
        Pa_des = desired["Pa"]; Pb_des = desired["Pb"]

        if not lp or not lp.get("pool_exists", False):
            return {
                "signal_type": "OPEN_NEW_RANGE",
                "payload": {"Pa": Pa_des, "Pb": Pb_des, "pool_type": desired.get("pool_type")},
            }

        Pa_lp = lp.get("Pa"); Pb_lp = lp.get("Pb")
        if Pa_lp is None or Pb_lp is None:
            return {
                "signal_type": "REBALANCE_TO_RANGE",
                "payload": {"Pa": Pa_des, "Pb": Pb_des, "reason": "LP_without_bounds"},
            }

        # Simple tolerance: if bounds differ materially, rebalance
        tol = 1e-9
        if abs(Pa_lp - Pa_des) > tol or abs(Pb_lp - Pb_des) > tol:
            return {
                "signal_type": "REBALANCE_TO_RANGE",
                "payload": {"Pa": Pa_des, "Pb": Pb_des},
            }

        # All good: no-op
        return None
