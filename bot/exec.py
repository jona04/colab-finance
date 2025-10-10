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
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from bot.utils.log import log_info, log_warn
from bot.config import get_settings
from bot.vault_registry import get as vault_get

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


def main():
    parser = argparse.ArgumentParser(description="Manual vault rebalance executor.")
    parser.add_argument("--lower", type=int, required=True, help="Lower tick (multiple of tickSpacing)")
    parser.add_argument("--upper", type=int, required=True, help="Upper tick (multiple of tickSpacing)")
    parser.add_argument("--execute", action="store_true", help="Actually run forge script (otherwise dry-run)")
    parser.add_argument("--vault-exit", action="store_true", help="Exit position to vault (decrease+collect+burn).")
    parser.add_argument("--vault-exit-withdraw", action="store_true", help="Exit position and withdraw all to owner.")
    parser.add_argument("--vault", type=str, help="Vault alias or 0x-address (overrides settings)")
    
    args = parser.parse_args()

    mode_exit = bool(args.vault_exit)
    mode_exit_withdraw = bool(args.vault_exit_withdraw)

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
        
    s = get_settings()
    if args.vault:
        if args.vault.startswith("0x"):
            vault_addr = args.vault
        else:
            v = vault_get(args.vault)
            if not v:
                raise RuntimeError("unknown vault alias in --vault")
            vault_addr = v["address"]

    log_info(f"Preparing {'EXECUTION' if args.execute else 'DRY-RUN'} for vault={vault_addr}")
    log_info(action_label)

    # export the envs that your Forge script reads via vm.env*
    env = os.environ.copy()
    env["VAULT_ADDRESS"] = vault_addr
    env["RPC_SEPOLIA"]   = s.rpc_url
    if not (mode_exit or mode_exit_withdraw):
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
        # override opcional via FORGE_SCRIPT_EXIT_FILE
        script_target = env.get("FORGE_SCRIPT_EXIT_FILE", "script/VaultExit.s.sol:VaultExit")
    elif mode_exit_withdraw:
        # override opcional via FORGE_SCRIPT_EXIT_WITHDRAW_FILE
        script_target = env.get("FORGE_SCRIPT_EXIT_WITHDRAW_FILE", "script/VaultExitWithdraw.s.sol:VaultExitWithdraw")
    else:
        # override opcional via FORGE_SCRIPT_FILE
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

        state_path = Path("bot/state.json")
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        hist = state.get("exec_history", [])
        hist.append({
            "ts": datetime.now(datetime.UTC).isoformat(),
            "lower": args.lower if args.lower is not None else None,
            "upper": args.upper if args.upper is not None else None,
            "mode": ("exit_withdraw" if mode_exit_withdraw else ("exit" if mode_exit else "rebalance")),
            "tx": txh,
            "stdout_tail": proc.stdout[-3000:],
        })
        state["exec_history"] = hist[-50:]
        state_path.write_text(json.dumps(state, indent=2))

        log_info("Forge script OK.")
    except FileNotFoundError:
        log_warn("Forge not found. Set FORGE_BIN or install foundryup.")
    except Exception as e:
        log_warn(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
