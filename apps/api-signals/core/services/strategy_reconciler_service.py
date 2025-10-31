from typing import Dict, Optional, List

from ...adapters.external.pipeline.pipeline_http_client import PipelineHttpClient


class StrategyReconcilerService:
    """
    Compares desired episode state with on-chain LP state (via api-liquidity-provider)
    and produces an executable plan ("steps") for the pipeline.
    The steps array is strictly ordered. The pipeline MUST execute in order.

    Steps canonical order for a rotation is:
      1) COLLECT  (harvest fees to vault)
      2) WITHDRAW (pull liquidity from pool back to vault idle balance)
      3) SWAP_EXACT_IN (rebalance token proportions in idle balance)
      4) REBALANCE (open new range using idle balances)
    """

    def __init__(self, lp_client: PipelineHttpClient):
        self._lp = lp_client

    async def reconcile(self, strategy_id: str, desired: Dict, symbol: str) -> Optional[Dict]:
        """
        Build an execution plan for the given desired episode.

        Returns:
            {
              "strategy_id": "...",
              "signal_type": "OPEN_NEW_RANGE" | "REBALANCE_TO_RANGE",
              "steps": [
                 {"action": "COLLECT", ...},
                 {"action": "WITHDRAW", ...},
                 {"action": "SWAP_EXACT_IN", ...},
                 {"action": "REBALANCE", ...}
              ],
              "episode": desired,
              "symbol": symbol
            }

        Or None if LP is already aligned.
        """

        Pa_des = float(desired["Pa"])
        Pb_des = float(desired["Pb"])

        dex = desired.get("dex")
        alias = desired.get("alias")

        # pull live vault status so we know if position exists / is aligned
        lp_status = None
        if dex and alias:
            lp_status = await self._lp.get_status(dex=dex, alias=alias)

        # No LP or no position yet -> first time open
        if not lp_status or not lp_status.get("pool"):
            if dex and alias:
                # temos vault configurado, só ainda não abriu range -> podemos pedir REBALANCE direto
                steps = [
                    {
                        "action": "REBALANCE",
                        "payload": {
                            "dex": dex,
                            "alias": alias,
                            "lower_price": Pa_des,
                            "upper_price": Pb_des,
                            # caps / ticks serão decididos em runtime pelo executor
                        },
                    }
                ]
            else:
                # não temos dex/alias => não tem infra. Registrar intenção apenas.
                steps = [
                    {
                        "action": "NOOP_LEGACY",
                        "payload": {
                            "reason": "FIRST_OPEN_NO_VAULT",
                            "lower_price": Pa_des,
                            "upper_price": Pb_des,
                        },
                    }
                ]

            return {
                "strategy_id": strategy_id,
                "signal_type": "OPEN_NEW_RANGE",
                "steps": steps,
                "episode": desired,
                "symbol": symbol,
            }

        # Try to read current band
        prices_cur = lp_status.get("prices", {}) or {}
        lower_cur = prices_cur.get("lower", {}) or {}
        upper_cur = prices_cur.get("upper", {}) or {}

        Pa_lp = lower_cur.get("p_t1_t0")
        Pb_lp = upper_cur.get("p_t1_t0")

        tol = 1e-9
        aligned = (
            Pa_lp is not None
            and Pb_lp is not None
            and abs(Pa_lp - Pa_des) <= tol
            and abs(Pb_lp - Pb_des) <= tol
        )
        if aligned:
            # nothing to do
            return None

        # Not aligned -> full rotate plan
        return self._build_full_plan(
            dex=dex,
            alias=alias,
            Pa_des=Pa_des,
            Pb_des=Pb_des,
            strategy_id=strategy_id,
            desired=desired,
            symbol=symbol,
            reason="RANGE_MISMATCH_OR_REDEPLOY",
        )

    def _build_full_plan(
        self,
        dex: Optional[str],
        alias: Optional[str],
        Pa_des: float,
        Pb_des: float,
        strategy_id: str,
        desired: Dict,
        symbol: str,
        reason: str,
    ) -> Dict:
        """
        Build canonical rotation steps with WITHDRAW included.
        If dex/alias is missing, we can't actually call vault API, so fall back to NOOP.
        """
        steps: List[Dict] = []

        if dex and alias:
            steps.append({
                "action": "COLLECT",
                "payload": {
                    "dex": dex,
                    "alias": alias,
                },
            })
            steps.append({
                "action": "WITHDRAW",
                "payload": {
                    "dex": dex,
                    "alias": alias,
                    "mode": "pool",  # always withdraw LP liquidity back to idle balance
                },
            })
            steps.append({
                "action": "SWAP_EXACT_IN",
                "payload": {
                    "dex": dex,
                    "alias": alias,
                    # token_in / token_out / amount decided at runtime
                },
            })
            steps.append({
                "action": "REBALANCE",
                "payload": {
                    "dex": dex,
                    "alias": alias,
                    "lower_price": Pa_des,
                    "upper_price": Pb_des,
                    # caps decided at runtime
                },
            })
        else:
            steps.append({
                "action": "NOOP_LEGACY",
                "payload": {
                    "reason": reason,
                    "lower_price": Pa_des,
                    "upper_price": Pb_des,
                },
            })

        return {
            "strategy_id": strategy_id,
            "signal_type": "REBALANCE_TO_RANGE",
            "steps": steps,
            "episode": desired,
            "symbol": symbol,
        }
