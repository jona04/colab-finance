# bot/exec.py
"""
Unified executor for vault operations (dry-run or on-chain execution).

Supported modes:
- Rebalance (default when --lower and --upper are provided)
- Rebalance with caps (no swaps): add --rebalance-caps --cap0 --cap1
- Exit position to vault:            --vault-exit
- Exit position and withdraw all:    --vault-exit-withdraw
- Deposit ERC20 into the vault:      --deposit --token --amount
- Collect pending fees to the vault: --collect
- Deploy a new vault:                --deploy-vault --nfpm [--alias ...] [--pool ...] [--rpc ...]

Behavior:
- Dry-run: prints the intent and exits.
- Execute: shells out to `forge script`, passing env vars the Solidity script consumes via vm.env*.
- PRIVATE_KEY is read from settings (.env), normalized, and passed to forge.

This file preserves your original "mode" pattern, fixing:
- Counting exactly one mode (now includes collect; caps is a modifier of rebalance)
- Using pool/nfpm before they were resolved
- Missing script branch for 'collect'
- Converting caps amounts only after Chain/pool metadata is available
"""

import os
import re
import argparse
import shutil
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from dotenv import load_dotenv

from bot.utils.log import log_info, log_warn
from bot.config import get_settings
from bot.vault_registry import get as vault_get, add as vault_add, set_active as vault_set_active
from bot.state_utils import load as _state_load, save as _state_save
from bot.chain import Chain

load_dotenv()


def _require_tool(name: str) -> str:
    """Find a binary in PATH or via env override (FORGE_BIN)."""
    if name == "forge" and os.environ.get("FORGE_BIN"):
        return os.environ["FORGE_BIN"]
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"'{name}' not found in PATH. Install foundryup or export FORGE_BIN=/path/to/forge")
    return path


def normalize_pk(raw: str | None) -> str:
    """
    Normalize a private key string:
    - strip whitespace and surrounding quotes
    - accept with or without 0x
    - validate 64 hex chars
    Return lowercase '0x' + 64 hex.
    """
    if not raw:
        raise ValueError("PRIVATE_KEY missing")

    pk = raw.strip()

    # strip accidental quotes
    if (pk.startswith('"') and pk.endswith('"')) or (pk.startswith("'") and pk.endswith("'")):
        pk = pk[1:-1].strip()

    # remove 0x for validation, re-add later
    body = pk[2:] if pk.lower().startswith("0x") else pk
    if not re.fullmatch(r"[0-9a-fA-F]{64}", body):
        raise ValueError("Invalid PRIVATE_KEY: expected 64 hex chars (with or without 0x)")

    return "0x" + body.lower()


def _resolve_vault_and_ctx(vault_arg: str | None) -> tuple[str, dict]:
    """
    Resolve vault address + registry row from either @alias or 0x-address.
    Returns (vault_addr, vault_row_or_empty_dict). When 0x-address, row may be {}.
    """
    if not vault_arg:
        raise RuntimeError("missing --vault (alias like @ethusdc or 0x-address)")

    if vault_arg.startswith("@"):
        alias = vault_arg[1:]
    else:
        alias = vault_arg if not vault_arg.startswith("0x") else None

    if vault_arg.startswith("0x"):
        return vault_arg, {}

    v = vault_get(alias)
    if not v:
        raise RuntimeError("unknown vault alias in --vault")
    return v["address"], v


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description="Vault executor (rebalance/deploy/deposit/withdraw/collect).")

    # --- Rebalance args (with optional caps) ---
    parser.add_argument("--lower", type=int, help="Lower tick (multiple of tickSpacing)")
    parser.add_argument("--upper", type=int, help="Upper tick (multiple of tickSpacing)")
    parser.add_argument("--rebalance-caps", action="store_true",
                        help="Use caps (no swaps): pass --cap0/--cap1 as human amounts.")
    parser.add_argument("--cap0", type=str, help="Human amount of token0 to use (cap).")
    parser.add_argument("--cap1", type=str, help="Human amount of token1 to use (cap).")

    # --- Execution & side modes ---
    parser.add_argument("--execute", action="store_true", help="Actually run forge script (otherwise dry-run)")
    parser.add_argument("--vault-exit", action="store_true", help="Exit position to vault (decrease+collect+burn).")
    parser.add_argument("--vault-exit-withdraw", action="store_true", help="Exit position and withdraw all to owner.")
    parser.add_argument("--deposit", action="store_true", help="Deposit ERC20 into the vault (simple transfer).")
    parser.add_argument("--collect", action="store_true", help="Collect pending fees into the vault (no liquidity change).")

    # --- Deposit args ---
    parser.add_argument("--token", type=str, help="ERC20 token address to deposit")
    parser.add_argument("--amount", type=str, help="Human amount (e.g., 1000.5)")

    # --- Target vault (all non-deploy modes) ---
    parser.add_argument("--vault", type=str, help="Vault alias prefixed with @ or 0x-address")

    # --- Deploy mode ---
    parser.add_argument("--deploy-vault", action="store_true", help="Deploy a new SingleUserVault.")
    parser.add_argument("--alias", type=str, help="Alias to register the newly deployed vault (e.g., ethusdc).")
    parser.add_argument("--nfpm", type=str, help="NFPM address (required in deploy mode).")
    parser.add_argument("--pool", type=str, help="Pool address (optional in deploy mode).")
    parser.add_argument("--rpc", type=str, help="RPC URL to store in registry for this alias (optional).")

    args = parser.parse_args()

    # -----------------------------
    # Mode resolution (keep your pattern)
    # -----------------------------
    mode_deploy         = bool(args.deploy_vault)
    mode_collect        = bool(args.collect)
    mode_exit           = bool(args.vault_exit)
    mode_exit_withdraw  = bool(args.vault_exit_withdraw)
    mode_deposit        = bool(args.deposit)
    mode_caps           = bool(args.rebalance_caps)  # modifier, not a mode
    mode_rebalance      = not (mode_exit or mode_exit_withdraw or mode_deposit or mode_collect or mode_deploy)

    # Exactly one main mode among: deploy, collect, exit, exit_withdraw, deposit, rebalance
    if sum([mode_deploy, mode_collect, mode_exit, mode_exit_withdraw, mode_deposit, mode_rebalance]) != 1:
        raise RuntimeError(
            "Select exactly one mode: "
            "--deploy-vault | --collect | --vault-exit | --vault-exit-withdraw | --deposit | (rebalance default)"
        )

    if mode_exit and mode_exit_withdraw:
        raise RuntimeError("Use only one of --vault-exit OR --vault-exit-withdraw (not both).")

    # -----------------------------
    # Settings
    # -----------------------------
    s = get_settings()

    # -----------------------------
    # Resolve vault (for all non-deploy modes)
    # -----------------------------
    vault_addr = None
    v = {}
    nfpm = None
    pool = None
    alias_for_state = "default"

    if not mode_deploy:
        vault_addr, v = _resolve_vault_and_ctx(args.vault)
        nfpm = v.get("nfpm")
        pool = v.get("pool")
        # alias used to write state history per vault (if provided as @alias)
        alias_for_state = (args.vault[1:] if args.vault and args.vault.startswith("@") else "default")

    # -----------------------------
    # Per-mode validations + label
    # -----------------------------
    if mode_deploy:
        if not args.nfpm:
            raise RuntimeError("Deploy mode requires --nfpm.")
        # alias is optional (recommended to auto-register)
        action_label = f"Deploy SingleUserVault(nfpm={args.nfpm})" + (f" & setPoolOnce({args.pool})" if args.pool else "")
    elif mode_collect:
        action_label = "Collect pending fees to vault"
    elif mode_exit_withdraw:
        action_label = "Exit + WithdrawAll to owner"
    elif mode_exit:
        action_label = "Exit position to vault"
    elif mode_deposit:
        if not args.token or not args.amount:
            raise RuntimeError("Deposit mode requires --token and --amount.")
        action_label = f"Deposit token={args.token} amount={args.amount}"
    else:
        # Rebalance (default)
        if args.lower is None or args.upper is None:
            raise RuntimeError("Rebalance mode requires --lower and --upper.")
        action_label = f"Rebalance lower={args.lower}, upper={args.upper}"

    # -----------------------------
    # If using caps, convert cap0/cap1 to raw using pool decimals
    # -----------------------------
    cap0_raw = None
    cap1_raw = None
    if mode_caps:
        if mode_deploy or mode_collect or mode_exit or mode_exit_withdraw or mode_deposit:
            raise RuntimeError("--rebalance-caps can only be combined with rebalance mode.")
        if args.lower is None or args.upper is None:
            raise RuntimeError("Rebalance-caps requires --lower and --upper.")
        if not pool:
            raise RuntimeError("Vault has no pool set. Use /vault_setpool first.")
        if args.cap0 is None or args.cap1 is None:
            raise RuntimeError("Provide --cap0 and --cap1 (human amounts) for --rebalance-caps.")

        # Build a Chain to read token decimals
        ch = Chain(s.rpc_url, pool, nfpm, vault_addr)
        t0 = ch.pool.functions.token0().call()
        t1 = ch.pool.functions.token1().call()
        dec0 = ch.erc20(t0).functions.decimals().call()
        dec1 = ch.erc20(t1).functions.decimals().call()

        cap0_raw = int(Decimal(args.cap0) * (Decimal(10) ** dec0))
        cap1_raw = int(Decimal(args.cap1) * (Decimal(10) ** dec1))

    # -----------------------------
    # Logs
    # -----------------------------
    log_info(f"Preparing {'EXECUTION' if args.execute else 'DRY-RUN'} for vault={vault_addr}")
    log_info(action_label)

    # -----------------------------
    # Build env for forge script
    # -----------------------------
    env = os.environ.copy()
    env["RPC_SEPOLIA"] = s.rpc_url  # keep the variable name you use in your forge scripts

    if mode_deploy:
        env["NFPM_ADDRESS"] = args.nfpm
        if args.pool:
            env["POOL_ADDRESS"] = args.pool
    else:
        env["VAULT_ADDRESS"] = vault_addr

        if mode_deposit:
            if not pool:
                raise RuntimeError("Vault has no pool set. Use /vault_setpool first.")
            ch = Chain(s.rpc_url, pool, nfpm, vault_addr)
            t0 = ch.pool.functions.token0().call()
            t1 = ch.pool.functions.token1().call()
            tok = args.token
            if tok.lower() not in (t0.lower(), t1.lower()):
                raise RuntimeError("Token is not part of the vault pool (must be token0 or token1).")
            dec = ch.erc20(tok).functions.decimals().call()
            amt_raw = int(Decimal(args.amount) * (Decimal(10) ** dec))
            env["TOKEN_ADDRESS"] = tok
            env["AMOUNT_RAW"] = str(amt_raw)

        if mode_rebalance:
            env["LOWER_TICK"] = str(args.lower)
            env["UPPER_TICK"] = str(args.upper)
            if mode_caps:
                env["CAP0_RAW"] = str(cap0_raw)
                env["CAP1_RAW"] = str(cap1_raw)

    # -----------------------------
    # Dry-run short-circuit
    # -----------------------------
    if not args.execute:
        log_warn("Dry-run only — no transaction sent.")
        return

    # -----------------------------
    # Execute forge script
    # -----------------------------
    pk = normalize_pk(s.private_key)
    forge = _require_tool("forge")
    contracts_dir = os.path.join(os.getcwd(), "contracts")
    if not os.path.isdir(contracts_dir):
        raise RuntimeError(f"contracts/ directory not found at {contracts_dir}")

    if mode_deploy:
        script_target = env.get("FORGE_SCRIPT_CREATE_FILE", "script/VaultCreate.s.sol:VaultCreate")
    elif mode_exit:
        script_target = env.get("FORGE_SCRIPT_EXIT_FILE", "script/VaultExit.s.sol:VaultExit")
    elif mode_exit_withdraw:
        script_target = env.get("FORGE_SCRIPT_EXIT_WITHDRAW_FILE", "script/VaultExitWithdraw.s.sol:VaultExitWithdraw")
    elif mode_deposit:
        script_target = env.get("FORGE_SCRIPT_DEPOSIT_FILE", "script/VaultDeposit.s.sol:VaultDeposit")
    elif mode_collect:
        script_target = env.get("FORGE_SCRIPT_COLLECT_FILE", "script/VaultCollect.s.sol:VaultCollect")
    elif mode_caps:
        script_target = env.get("FORGE_SCRIPT_REBALANCE_CAPS_FILE", "script/RebalanceCaps.s.sol:RebalanceCaps")
    else:
        script_target = env.get("FORGE_SCRIPT_FILE", "script/RebalanceManual.s.sol:RebalanceManual")

    log_info("Running forge script...")
    try:
        cmd = [
            forge, "script", script_target,
            "--rpc-url", s.rpc_url,
            "--private-key", pk,
            "--broadcast",
            "-vvvv"
        ]
        proc = subprocess.run(
            cmd,
            cwd=contracts_dir,
            env=env,
            capture_output=True,
            text=True
        )
        if proc.returncode != 0:
            # Show stderr first (usually has import/VM hints)
            print(proc.stderr)
            print(proc.stdout)
            log_warn("Forge script failed.")
            return

        print(proc.stdout)

        # Extract tx hash (best-effort)
        txh = None
        m = re.search(r"transactionHash\s+(0x[0-9a-fA-F]{64})", proc.stdout)
        if m:
            txh = m.group(1)

        # Persist history (per alias if provided, else "default")
        state = _state_load(alias_for_state or "default")
        hist = state.get("exec_history", [])

        if mode_deploy:
            # Parse deployed address line (adapt if your script prints differently)
            vm = re.search(r"Deployed SingleUserVault at:\s+(0x[0-9a-fA-F]{40})", proc.stdout)
            if not vm:
                log_warn("Could not find deployed address in output.")
                deployed_addr = None
            else:
                deployed_addr = vm.group(1)
                # Auto-register in registry if alias provided
                if args.alias and deployed_addr:
                    try:
                        vault_add(args.alias, deployed_addr, args.pool, args.nfpm, args.rpc or s.rpc_url)
                        vault_set_active(args.alias)
                        log_info(f"Registered @{args.alias} -> {deployed_addr} (active).")
                    except Exception as e:
                        log_warn(f"Failed to add/set_active in registry: {e}")

            hist.append({
                "ts": _now_iso(),
                "mode": "deploy_vault",
                "vault": deployed_addr,
                "pool": args.pool,
                "nfpm": args.nfpm,
                "tx": txh,
                "stdout_tail": proc.stdout[-3000:],
            })
            state["exec_history"] = hist[-50:]
            _state_save(alias_for_state, state)
            log_info("Forge script OK (deploy).")
            return

        # Non-deploy: append exec entry
        hist.append({
            "ts": _now_iso(),
            "lower": args.lower if mode_rebalance else None,
            "upper": args.upper if mode_rebalance else None,
            "mode": (
                "collect" if mode_collect else
                ("rebalance_caps" if mode_caps else
                 ("deposit" if mode_deposit else
                  ("exit_withdraw" if mode_exit_withdraw else
                   ("exit" if mode_exit else "rebalance"))))
            ),
            "tx": txh,
            "stdout_tail": proc.stdout[-3000:],
        })
        state["exec_history"] = hist[-50:]

        if mode_deposit:
            deps = state.get("deposits", [])
            deps.append({
                "ts": _now_iso(),
                "token": args.token,
                "amount_human": args.amount,
                "tx": txh,
            })
            state["deposits"] = deps[-200:]

        if mode_collect:
            col = state.get("collect_history", [])
            col.append({
                "ts": _now_iso(),
                "tx": txh,
            })
            state["collect_history"] = col[-200:]

        _state_save(alias_for_state, state)
        log_info("Forge script OK.")
    except FileNotFoundError:
        log_warn("Forge not found. Set FORGE_BIN or install foundryup.")
    except Exception as e:
        log_warn(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
