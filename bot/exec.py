# bot/exec.py
"""
Manual rebalance executor (dry-run or on-chain execution).

Usage:
    python -m bot.exec --lower 181000 --upper 182000 [--execute]

Behavior:
- Dry-run: only prints the suggested range and exits.
- Execute: shells out to `forge script` passing LOWER_TICK/UPPER_TICK/VAULT_ADDRESS
  via environment and the private key via `--private-key`.

Notes:
- PRIVATE_KEY is read from env (.env loaded by Settings). We normalize it to ensure
  it's a 32-byte hex string, with or without 0x, and without quotes.
- We pass `env=...` to subprocess so that vm.env* in the Forge script can read ticks.
"""

import os
import re
import argparse
import shutil
import subprocess
import re, json
from datetime import datetime, timezone
from decimal import Decimal
from dotenv import load_dotenv
from bot.utils.log import log_info, log_warn
from bot.config import get_settings
from bot.vault_registry import get as vault_get
from bot.state_utils import load as _state_load, save as _state_save
from bot.chain import Chain

load_dotenv()


def _require_tool(name: str) -> str:
    """Find a binary in PATH or via env override (FORGE_BIN)."""
    if name == "forge":
        if os.environ.get("FORGE_BIN"):
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
    parser = argparse.ArgumentParser(description="Manual vault rebalance executor.")
    # rebalance
    parser.add_argument("--lower", type=int, help="Lower tick (multiple of tickSpacing)")
    parser.add_argument("--upper", type=int, help="Upper tick (multiple of tickSpacing)")
    # modes
    parser.add_argument("--execute", action="store_true", help="Actually run forge script (otherwise dry-run)")
    parser.add_argument("--vault-exit", action="store_true", help="Exit position to vault (decrease+collect+burn).")
    parser.add_argument("--vault-exit-withdraw", action="store_true", help="Exit position and withdraw all to owner.")
    parser.add_argument("--deposit", action="store_true", help="Deposit ERC20 into the vault (simple transfer).")
    parser.add_argument("--collect", action="store_true", help="Collect pending fees into the vault (no liquidity change).")
    # deposit args
    parser.add_argument("--token", type=str, help="ERC20 token address to deposit")
    parser.add_argument("--amount", type=str, help="Human amount (e.g., 1000.5)")
    # target vault
    parser.add_argument("--vault", type=str, help="Vault alias prefixed with @ or 0x-address")

    
    args = parser.parse_args()

    mode_collect = bool(args.collect)
    mode_exit = bool(args.vault_exit)
    mode_exit_withdraw = bool(args.vault_exit_withdraw)
    mode_deposit = bool(args.deposit)
    mode_rebalance = not (mode_exit or mode_exit_withdraw or mode_deposit or mode_collect)

    if sum([mode_rebalance, mode_exit, mode_exit_withdraw, mode_deposit, mode_collect]) != 1:
        raise RuntimeError("Choose exactly one mode: rebalance | --vault-exit | --vault-exit-withdraw | --deposit | --collect")

    if mode_exit and mode_exit_withdraw:
        raise RuntimeError("Use only one of --vault-exit OR --vault-exit-withdraw (not both).")

    if not mode_exit and not mode_exit_withdraw:
        # modo padrão: rebalance — exige lower/upper
        if args.lower is None or args.upper is None:
            raise RuntimeError("Rebalance mode requires --lower and --upper.")
        action_label = f"Rebalance lower={args.lower}, upper={args.upper}"
    else:
        # modos de exit: não exigem lower/upper
        action_label = "Exit position to vault" if mode_exit else "Exit + WithdrawAll to owner"
    
    # Validate mode-specific required args
    if mode_rebalance:
        if args.lower is None or args.upper is None:
            raise RuntimeError("Rebalance mode requires --lower and --upper.")
        action_label = f"Rebalance lower={args.lower}, upper={args.upper}"
    elif mode_exit:
        action_label = "Exit position to vault"
    elif mode_exit_withdraw:
        action_label = "Exit + WithdrawAll to owner"
    elif mode_deposit:
        if not args.token or not args.amount:
            raise RuntimeError("Deposit mode requires --token and --amount.")
        action_label = f"Deposit token={args.token} amount={args.amount}"
    else:  # collect
        action_label = "Collect pending fees to vault"
        
    s = get_settings()
    vault_addr, v = _resolve_vault_and_ctx(args.vault)
    rpc  = v.get("rpc_url")
    nfpm = v.get("nfpm")
    pool = v.get("pool")
    
    alias_for_state = (args.vault[1:] if args.vault and args.vault.startswith("@") else "default")

    log_info(f"Preparing {'EXECUTION' if args.execute else 'DRY-RUN'} for vault={vault_addr}")
    log_info(action_label)

    # export the envs that Forge script reads via vm.env*
    env = os.environ.copy()
    env["VAULT_ADDRESS"] = vault_addr
    env["RPC_SEPOLIA"]   = s.rpc_url

    # For deposit: validate token is token0 or token1 of vault's pool
    if mode_deposit:
        if not pool:
            raise RuntimeError("Vault has no pool set. Use /vault_setpool first.")
        # Build a tiny Chain just to read pool + token decimals
        ch = Chain(s.rpc_url, pool, nfpm, vault_addr)
        t0 = ch.pool.functions.token0().call()
        t1 = ch.pool.functions.token1().call()
        tok = args.token
        if tok.lower() not in (t0.lower(), t1.lower()):
            raise RuntimeError("Token is not part of the vault pool (must be token0 or token1).")
        dec = ch.erc20(tok).functions.decimals().call()
        # amountRaw = amount * 10^dec
        amt_raw = int(Decimal(args.amount) * (Decimal(10) ** dec))
        env["TOKEN_ADDRESS"] = tok
        env["AMOUNT_RAW"] = str(amt_raw)

    if mode_rebalance:
        env["LOWER_TICK"] = str(args.lower)
        env["UPPER_TICK"] = str(args.upper)

    if not args.execute:
        log_warn("Dry-run only — no transaction sent.")
        return
    
    # normalize private key (and fail early if bad)
    pk = normalize_pk(s.private_key)

    # Descobre 'forge' e define CWD=contracts
    forge = _require_tool("forge")
    contracts_dir = os.path.join(os.getcwd(), "contracts")
    if not os.path.isdir(contracts_dir):
        raise RuntimeError(f"contracts/ directory not found at {contracts_dir}")

    if mode_exit:
        script_target = env.get("FORGE_SCRIPT_EXIT_FILE", "script/VaultExit.s.sol:VaultExit")
    elif mode_exit_withdraw:
        script_target = env.get("FORGE_SCRIPT_EXIT_WITHDRAW_FILE", "script/VaultExitWithdraw.s.sol:VaultExitWithdraw")
    elif mode_deposit:
        script_target = env.get("FORGE_SCRIPT_DEPOSIT_FILE", "script/VaultDeposit.s.sol:VaultDeposit")
    elif mode_collect:
        script_target = env.get("FORGE_SCRIPT_COLLECT_FILE", "script/VaultCollect.s.sol:VaultCollect")
    else:
        script_target = env.get("FORGE_SCRIPT_FILE", "script/RebalanceManual.s.sol:RebalanceManual")

    log_info("Running forge script...")
    try:
        # --rpc-url pega do env; --private-key também
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
            # Mostra stderr primeiro (geralmente tem os 'Unable to resolve imports')
            print(proc.stderr)
            print(proc.stdout)
            log_warn("Forge script failed.")
            return

        print(proc.stdout)
        
        txh = None
        m = re.search(r"transactionHash\s+(0x[0-9a-fA-F]{64})", proc.stdout)
        if m: txh = m.group(1)

        state = _state_load(alias_for_state or "default")
        hist = state.get("exec_history", [])
        hist.append({
            "ts": _now_iso(),
            "lower": args.lower if mode_rebalance else None,
            "upper": args.upper if mode_rebalance else None,
            "mode": ("deposit" if mode_deposit else ("exit_withdraw" if mode_exit_withdraw else ("exit" if mode_exit else "rebalance"))),
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
