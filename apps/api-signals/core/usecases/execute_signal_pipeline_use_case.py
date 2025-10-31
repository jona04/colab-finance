import asyncio
import logging
from typing import Dict, List, Optional

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
                        # p_t1_t0 = "token1 per token0"
                        # -> valor USD do token0 em termos de token1 (USDC). Perfeito p/ converter amt0 -> USD.

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

                            target_major_pct = float(episode.get("target_major_pct", 0.5))
                            target_minor_pct = float(episode.get("target_minor_pct", 0.5))

                            majority_flag = episode.get("majority_on_open")
                            
                            if majority_flag == "token2":
                                major_side = "token0"
                                major_curr_usd = usd0
                                target_major_usd = total_usd * target_major_pct
                                delta_usd = target_major_usd - major_curr_usd

                                if delta_usd > 0.0:
                                    # comprar WETH usando USDC
                                    token_in_addr = token1_addr   # USDC
                                    token_out_addr = token0_addr  # WETH
                                    direction = "USDC->WETH"
                                    req_amount_usd = delta_usd
                                else:
                                    # comprar USDC usando WETH
                                    token_in_addr = token0_addr   # WETH
                                    token_out_addr = token1_addr  # USDC
                                    direction = "WETH->USDC"
                                    req_amount_usd = (-delta_usd)
                            
                            else:
                                major_side = "token1"
                                major_curr_usd = usd1
                                target_major_usd = total_usd * target_major_pct
                                delta_usd = target_major_usd - major_curr_usd

                                if delta_usd > 0.0:
                                    # comprar USDC usando WETH
                                    token_in_addr = token0_addr   # WETH
                                    token_out_addr = token1_addr  # USDC
                                    direction = "WETH->USDC"
                                    req_amount_usd = delta_usd
                                else:
                                    # comprar WETH usando USDC
                                    token_in_addr = token1_addr   # USDC
                                    token_out_addr = token0_addr  # WETH
                                    direction = "USDC->WETH"
                                    req_amount_usd = (-delta_usd)

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
                                    "major_side": major_side,
                                    "p_t1_t0": p_t1_t0,
                                    "usd0": usd0,
                                    "usd1": usd1,
                                    "total_usd": total_usd,
                                    "target_major_pct": target_major_pct,
                                    "target_minor_pct": target_minor_pct,
                                    "target_major_usd": target_major_usd,
                                    "major_curr_usd": major_curr_usd,
                                    "delta_usd": delta_usd,
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
