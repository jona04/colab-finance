import asyncio
import logging
from typing import Dict, List, Optional

from ..repositories.signal_repository import SignalRepository
from ...adapters.external.pipeline.pipeline_http_client import PipelineHttpClient


class ExecuteSignalPipelineUseCase:
    """
    Consumes PENDING signals from Mongo and executes their steps IN ORDER
    via PipelineHttpClient (api-liquidity-provider).

    Rules:
      - Steps = [COLLECT, WITHDRAW, SWAP_EXACT_IN, REBALANCE] (some may be skipped).
      - Each step retries with backoff.
      - On hard failure -> mark FAILED and stop processing that signal.
      - On full success -> mark SENT.

    This use case *also* does runtime sizing:
      - before SWAP_EXACT_IN we read /status to pick direction/amount.
      - before REBALANCE we read /status again to size caps.
    """

    def __init__(
        self,
        signal_repo: SignalRepository,
        lp_client: PipelineHttpClient,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        base_backoff_sec: float = 1.0,
    ):
        self._signals = signal_repo
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

    async def _process_single_signal(self, sig: Dict) -> bool:
        """
        Executes a single signal's steps sequentially.
        Returns True on full success, False if FAILED.
        """
        steps: List[Dict] = sig.get("steps") or []
        episode = sig.get("episode") or {}

        dex = episode.get("dex")
        alias = episode.get("alias")
        token0_addr = episode.get("token0_address")
        token1_addr = episode.get("token1_address")
        majority_flag = episode.get("majority_on_open")
        
        for step in steps:
            action = step.get("action")
            self._logger.info("Executing step %s for %s/%s", action, dex, alias)

            success = False
            last_err: Optional[str] = None

            for attempt in range(self._max_retries):
                try:
                    if action == "NOOP_LEGACY":
                        success = True

                    elif action == "COLLECT":
                        res = await self._lp.post_collect(dex, alias)
                        if res is None:
                            raise RuntimeError("collect_failed")
                        success = True

                    elif action == "WITHDRAW":
                        # always withdraw mode "pool" to bring capital back idle
                        res = await self._lp.post_withdraw(dex, alias, mode="pool")
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


                        if p_t1_t0 <= 0.0:
                            # sem preço confiável = não dá pra calcular USD; nesse caso a gente não swapa
                            success = True
                        else:
                            usd0 = amt0 * p_t1_t0  # quanto vale nosso token0 em USDC
                            usd1 = amt1            # token1 já é USDC
                            total_usd = usd0 + usd1

                            target_major_pct = float(episode.get("target_major_pct", 0.5))
                            target_minor_pct = float(episode.get("target_minor_pct", 0.5))

                            majority_flag = episode.get("majority_on_open")
                            
                            if majority_flag == "token2":
                                # token0 (WETH) é o major
                                major_curr_usd = usd0
                                target_major_usd = total_usd * target_major_pct

                                delta_usd = target_major_usd - major_curr_usd
                                if delta_usd > 0.0:
                                    # precisamos comprar WETH usando USDC
                                    # isso significa: vender token1 (USDC) para comprar token0 (WETH)
                                    token_in_addr = token1_addr   # USDC
                                    token_out_addr = token0_addr  # WETH

                                    res = await self._lp.post_swap_exact_in(
                                        dex=dex,
                                        alias=alias,
                                        token_in=token_in_addr,
                                        token_out=token_out_addr,
                                        amount_in_usd=delta_usd, # pedimos p/ gastar delta_usd do lado minor
                                    )
                                    if res is None:
                                        raise RuntimeError("swap_failed")
                                else:
                                    # precisamos comprar USDC usando WETH
                                    # isso significa: vender token0 (WETH) para comprar token1 (USDC)
                                    token_in_addr = token0_addr   # WETH
                                    token_out_addr = token1_addr  # USDC

                                    res = await self._lp.post_swap_exact_in(
                                        dex=dex,
                                        alias=alias,
                                        token_in=token_in_addr,
                                        token_out=token_out_addr,
                                        amount_in_usd=delta_usd*-1, # pedimos p/ gastar delta_usd do lado minor
                                    )
                                    if res is None:
                                        raise RuntimeError("swap_failed")

                                success = True
                            
                            else:
                                # default: token1 (USDC) é o major
                                major_curr_usd = usd1
                                target_major_usd = total_usd * target_major_pct

                                delta_usd = target_major_usd - major_curr_usd
                                if delta_usd > 0.0:
                                    # precisamos comprar USDC usando WETH
                                    # isso significa: vender token0 (WETH) para comprar token1 (USDC)
                                    token_in_addr = token0_addr   # WETH
                                    token_out_addr = token1_addr  # USDC

                                    res = await self._lp.post_swap_exact_in(
                                        dex=dex,
                                        alias=alias,
                                        token_in=token_in_addr,
                                        token_out=token_out_addr,
                                        amount_in_usd=delta_usd,
                                    )
                                    if res is None:
                                        raise RuntimeError("swap_failed")
                                else:
                                    # precisamos comprar WETH usando USDC
                                    # isso significa: vender token1 (USDC) para comprar token0 (WETH)
                                    token_in_addr = token1_addr   # USDC
                                    token_out_addr = token0_addr  # WETH

                                    res = await self._lp.post_swap_exact_in(
                                        dex=dex,
                                        alias=alias,
                                        token_in=token_in_addr,
                                        token_out=token_out_addr,
                                        amount_in_usd=delta_usd*-1,
                                    )
                                    if res is None:
                                        raise RuntimeError("swap_failed")
                                    
                                success = True


                    elif action == "REBALANCE":
                        # Before opening new range, read status again to grab final idle balances.
                        st2 = await self._lp.get_status(dex, alias)
                        if not st2:
                            raise RuntimeError("status_unavailable_before_rebalance")

                        hold2 = st2.get("holdings", {}) or {}
                        totals2 = hold2.get("totals", {}) or {}
                        cap0 = float(totals2.get("token0", 0.0))
                        cap1 = float(totals2.get("token1", 0.0))

                        lower_price = step["payload"].get("lower_price")
                        upper_price = step["payload"].get("upper_price")

                        res = await self._lp.post_rebalance(
                            dex=dex,
                            alias=alias,
                            lower_price=lower_price,
                            upper_price=upper_price,
                            lower_tick=None,
                            upper_tick=None,
                        )
                        if res is None:
                            raise RuntimeError("rebalance_failed")
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
                    # incremental backoff
                    await asyncio.sleep(self._base_backoff * (attempt + 1))

            if not success:
                # hard fail -> mark FAILED and stop this signal
                await self._signals.mark_failure(sig, last_err or f"{action} failed")
                return False

        # all steps ok
        return True
