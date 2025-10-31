import asyncio
import logging
from math import sqrt
from typing import Dict, List, Optional, Tuple

from ...core.repositories.strategy_episode_repository import StrategyEpisodeRepository

from ..repositories.signal_repository import SignalRepository
from ...adapters.external.pipeline.pipeline_http_client import PipelineHttpClient


class ExecuteSignalPipelineUseCase:
    """
    Consumes PENDING signals from Mongo and executes their steps IN ORDER
    via PipelineHttpClient (api-liquidity-provider).

    Rules:
      - Steps = [COLLECT, WITHDRAW, SWAP_EXACT_IN, OPEN] (some may be skipped).
      - Each step retries with backoff.
      - On hard failure -> mark FAILED and stop processing that signal.
      - On full success -> mark SENT.

    Runtime sizing logic:
      - before SWAP_EXACT_IN we read /status to pick direction/amount.
      - before OPEN we read /status again to snapshot idle caps.
    """

    def __init__(
        self,
        signal_repo: SignalRepository,
        episode_repo: StrategyEpisodeRepository,
        lp_client: PipelineHttpClient,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        base_backoff_sec: float = 1.0,
    ):
        self._signals = signal_repo
        self._episodes = episode_repo
        self._lp = lp_client
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._max_retries = max_retries
        self._base_backoff = base_backoff_sec
        self.EPS_POS = 1e-12  # usado para clamps de raiz e separação Pa<P<Pb
        
    def _tokens_from_L(self, L, Pa, Pb, P):
        xa, xb, x = sqrt(Pa), sqrt(Pb), sqrt(P)
        if P <= Pa:   # tudo vira token0
            t0 = L * (1/xa - 1/xb); t1 = 0
        elif P >= Pb: # tudo vira token1
            t0 = 0; t1 = L * (xb - xa)
        else:         # misto
            t0 = L * (1/x - 1/xb)
            t1 = L * (x - xa)
        return t0, t1

    def _ensure_valid_band(self, Pa: float, Pb: float, P: float) -> Tuple[float, float]:
        """
        Garante:
        - Pa >= EPS_POS
        - Pb >= Pa + EPS_POS
        - Banda não degenera no mid (respeita almofada mínima em torno de P)
        """
        Pa = max(self.EPS_POS, Pa)
        Pb = max(Pa + self.EPS_POS, Pb)

        # almofada mínima ao redor de P (como você já faz em alguns pontos)
        mid_pad = self.EPS_POS * max(1.0, P)
        Pa = min(P - mid_pad, Pa)
        Pb = max(P + mid_pad, Pb)

        # Se por alguma razão Pa ultrapassou Pb após clamps, corrige separando pelo mid_pad:
        if not (Pa < Pb):
            Pa = P - mid_pad
            Pb = P + mid_pad

        return Pa, Pb

    def _L_closed(self, total_P: float, P: float, Pa: float, Pb: float) -> float:
        # assegura banda válida
        Pa, Pb = self._ensure_valid_band(Pa, Pb, P)

        a  = sqrt(max(self.EPS_POS, P))
        xa = sqrt(max(self.EPS_POS, Pa))
        xb = sqrt(max(self.EPS_POS, Pb))

        denom = 2 * a - xa - (P / xb)
        if denom <= 0:
            denom = self.EPS_POS
        return total_P / denom

    async def execute_once(self) -> None:
        """
        Fetch up to N pending signals and attempt to execute them.
        """
        pending = await self._signals.list_pending(limit=50)
        for sig in pending:
            try:
                ok = await self._process_single_signal(sig)
                if ok:
                    await self._signals.mark_success(sig)
                # if not ok, _process_single_signal already marked FAILED
            except Exception as exc:
                self._logger.exception("Unexpected error processing signal %s: %s", sig, exc)
                await self._signals.mark_failure(sig, f"UNEXPECTED: {exc}")

    async def _append_log(
        self,
        episode_id: Optional[str],
        base: Dict,
    ) -> None:
        """
        Helper: push a log line into the episode doc, if we have an episode_id.
        """
        if not episode_id:
            return
        try:
            await self._episodes.append_execution_log(episode_id, base)
        except Exception as log_exc:
            # logging de fallback pra não matar o fluxo
            self._logger.warning("Failed to append_execution_log for %s: %s", episode_id, log_exc)


    async def _process_single_signal(self, sig: Dict) -> bool:
        """
        Executes a single signal's steps sequentially.
        Returns True on full success, False if FAILED.
        """
        steps: List[Dict] = sig.get("steps") or []
        episode = sig.get("episode") or {}

        episode_id = episode.get("_id")
        dex = episode.get("dex")
        alias = episode.get("alias")
        token0_addr = episode.get("token0_address")
        token1_addr = episode.get("token1_address")
        majority_flag = episode.get("majority_on_open")

        for step in steps:
            action = step.get("action")
            self._logger.info("Executing step %s for %s/%s", action, dex, alias)

            if (not dex or not alias) and action != "NOOP_LEGACY":
                skip_msg = "Skipping step because no dex/alias is wired for this strategy."
                self._logger.info("%s %s", action, skip_msg)
                
                await self._append_log(
                    episode_id,
                    {
                        "step": action,
                        "phase": "skipped_no_dex_alias",
                        "reason": skip_msg,
                    },
                )
                
                continue
            
            success = False
            last_err: Optional[str] = None

            for attempt in range(self._max_retries):
                try:
                    if action == "NOOP_LEGACY":
                        success = True
                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "phase": "noop",
                                "attempt": attempt + 1,
                                "info": "NOOP_LEGACY executed",
                            },
                        )
                        
                    elif action == "COLLECT":
                        res = await self._lp.post_collect(dex, alias)
                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "attempt": attempt + 1,
                                "request": {"dex": dex, "alias": alias},
                                "response": res,
                            },
                        )
                        if res is None:
                            raise RuntimeError("collect_failed")
                        success = True

                    elif action == "WITHDRAW":
                        # always withdraw mode "pool" to bring capital back idle
                        res = await self._lp.post_withdraw(dex, alias, mode="pool")
                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "attempt": attempt + 1,
                                "request": {"dex": dex, "alias": alias, "mode": "pool"},
                                "response": res,
                            },
                        )
                        if res is None:
                            raise RuntimeError("withdraw_failed")
                        success = True

                    elif action == "SWAP_EXACT_IN":
                        # after withdraw, capital is idle in vault.
                        st = await self._lp.get_status(dex, alias)
                        if not st:
                            raise RuntimeError("status_unavailable_before_swap")
                        
                        holdings = st.get("holdings", {}) or {}
                        totals = holdings.get("totals", {}) or {}
                        amt0 = float(totals.get("token0", 0.0))
                        amt1 = float(totals.get("token1", 0.0))
                        
                        prices = st.get("prices", {}) or {}
                        current_prices = prices.get("current", {}) or {}
                        p_t1_t0 = float(current_prices.get("p_t1_t0", 0.0))
                    
                        # log snapshot BEFORE swap calc
                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "phase": "pre_calc",
                                "attempt": attempt + 1,
                                "holdings_raw": {
                                    "amt0": amt0,
                                    "amt1": amt1,
                                },
                                "price_snapshot": {
                                    "p_t1_t0": p_t1_t0,
                                },
                            },
                        )

                        if p_t1_t0 <= 0.0:
                            # sem preço confiável = não dá pra calcular USD; nesse caso a gente não swapa
                            await self._append_log(
                                episode_id,
                                {
                                    "step": action,
                                    "phase": "skip_no_price",
                                    "attempt": attempt + 1,
                                    "reason": "p_t1_t0 <= 0",
                                },
                            )
                            success = True
                        else:
                            usd0 = amt0 * p_t1_t0  # quanto vale nosso token0 em USDC
                            usd1 = amt1            # token1 já é USDC
                            total_usd = usd0 + usd1
                            P = p_t1_t0
                            
                            Pa = step["payload"].get("lower_price")
                            Pb = step["payload"].get("upper_price")
                            L_target = self._L_closed(total_usd, P, Pa, Pb)
                            t0_needed, t1_needed = self._tokens_from_L(L_target, Pa, Pb, P)
                            
                            majority_flag = episode.get("majority_on_open")
                            
                            if majority_flag == "token1":
                                # queremos alinhar token1 (USDC-like)
                                falta_t1 = t1_needed - usd1
                                if falta_t1 > 0:
                                    token_in_addr = token0_addr  # vender WETH
                                    token_out_addr = token1_addr # comprar USDC
                                    req_amount_usd = falta_t1
                                    direction = "WETH->USDC"
                                else:
                                    token_in_addr = token1_addr  # vender USDC
                                    token_out_addr = token0_addr # comprar WETH
                                    req_amount_usd = (-falta_t1)
                                    direction = "USDC->WETH"
                                    
                            else:
                                # majority_flag == "token2" (WETH)
                                t0_needed_usd = t0_needed * P
                                falta_t0  = t0_needed_usd - usd0
                                if falta_t0 > 0:
                                    token_in_addr = token1_addr  # vender USDC
                                    token_out_addr = token0_addr # comprar WETH
                                    req_amount_usd = falta_t0
                                    direction = "USDC->WETH"
                                else:
                                    token_in_addr = token0_addr  # vender WETH
                                    token_out_addr = token1_addr # comprar USDC
                                    req_amount_usd = (-falta_t0)
                                    direction = "WETH->USDC"

                            # para evitar o valor exato e causar erros de saldo
                            req_amount_usd = req_amount_usd - 0.01
                            
                            # log cálculo alvo
                            await self._append_log(
                                episode_id,
                                {
                                    "step": action,
                                    "phase": "calc_swap",
                                    "attempt": attempt + 1,
                                    "majority_flag": majority_flag,
                                    "p_t1_t0": p_t1_t0,
                                    "usd0": usd0,
                                    "usd1": usd1,
                                    "total_usd": total_usd,
                                    "t0_needed":t0_needed if t0_needed else None,
                                    "t1_needed": t1_needed if t1_needed else None,
                                    "falta_t1": falta_t1 if falta_t1 else None,
                                    "falta_t0": falta_t0 if falta_t0 else None,
                                    "t0_needed_usd": t0_needed_usd if t0_needed_usd else None,
                                    "req_amount_usd": req_amount_usd if req_amount_usd else None,
                                    "direction": direction,
                                    "request_amount_in_usd": req_amount_usd,
                                },
                            )

                            # se req_amount_usd ~ 0, nada a fazer
                            if req_amount_usd <= 0.0:
                                await self._append_log(
                                    episode_id,
                                    {
                                        "step": action,
                                        "phase": "skip_small",
                                        "attempt": attempt + 1,
                                        "reason": "no meaningful delta",
                                    },
                                )
                                success = True
                            else:
                                res = await self._lp.post_swap_exact_in(
                                    dex=dex,
                                    alias=alias,
                                    token_in=token_in_addr,
                                    token_out=token_out_addr,
                                    amount_in_usd=req_amount_usd,
                                )

                                await self._append_log(
                                    episode_id,
                                    {
                                        "step": action,
                                        "phase": "swap_call",
                                        "attempt": attempt + 1,
                                        "request": {
                                            "token_in": token_in_addr,
                                            "token_out": token_out_addr,
                                            "amount_in_usd": req_amount_usd,
                                        },
                                        "response": res,
                                    },
                                )

                                if res is None:
                                    raise RuntimeError("swap_failed")

                                success = True
                            
                            
                    elif action == "OPEN":
                        # Antes de abrir nova faixa, snapshot de idle caps atuais
                        st2 = await self._lp.get_status(dex, alias)
                        if not st2:
                            raise RuntimeError("status_unavailable_before_open")

                        hold2 = st2.get("holdings", {}) or {}
                        totals2 = hold2.get("totals", {}) or {}
                        cap0 = float(totals2.get("token0", 0.0))
                        cap1 = float(totals2.get("token1", 0.0))

                        lower_price = step["payload"].get("lower_price")
                        upper_price = step["payload"].get("upper_price")

                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "phase": "pre_open",
                                "attempt": attempt + 1,
                                "idle_caps": {
                                    "cap0": cap0,
                                    "cap1": cap1,
                                },
                                "range": {
                                    "lower_price": lower_price,
                                    "upper_price": upper_price,
                                },
                            },
                        )

                        # Chamar o novo endpoint open
                        res = await self._lp.post_open(
                            dex=dex,
                            alias=alias,
                            lower_price=lower_price,
                            upper_price=upper_price,
                            lower_tick=None,
                            upper_tick=None,
                        )

                        await self._append_log(
                            episode_id,
                            {
                                "step": action,
                                "phase": "open_call",
                                "attempt": attempt + 1,
                                "request": {
                                    "lower_price": lower_price,
                                    "upper_price": upper_price,
                                },
                                "response": res,
                            },
                        )

                        if res is None:
                            raise RuntimeError("open_failed")
                        success = True

                    else:
                        raise RuntimeError(f"unknown action {action}")

                    if success:
                        break

                except Exception as exc:
                    last_err = str(exc)
                    self._logger.warning(
                        "Step %s failed on attempt %s/%s: %s",
                        action, attempt + 1, self._max_retries, exc,
                    )
                    
                    await self._append_log(
                        episode_id,
                        {
                            "step": action,
                            "phase": "attempt_fail",
                            "attempt": attempt + 1,
                            "error": last_err,
                        },
                    )
                    
                    # incremental backoff
                    await asyncio.sleep(self._base_backoff * (attempt + 1))

            if not success:
                # hard fail -> mark FAILED and stop this signal
                await self._signals.mark_failure(sig, last_err or f"{action} failed")
                
                await self._append_log(
                    episode_id,
                    {
                        "step": action,
                        "phase": "hard_fail",
                        "error": last_err or f"{action} failed",
                    },
                )
                
                return False

        # all steps ok
        await self._append_log(
            episode_id,
            {
                "phase": "all_steps_done",
                "status": "SENT",
            },
        )
        return True
