"""
Manual rebalance executor (dry-run or exec)
Usage:
    python -m bot.exec --lower 181000 --upper 182000 [--execute]
"""
import os
import argparse
import subprocess
from bot.utils.log import log_info, log_warn
from bot.config import get_settings

def main():
    parser = argparse.ArgumentParser(description="Manual vault rebalance executor.")
    parser.add_argument("--lower", type=int, required=True, help="Lower tick")
    parser.add_argument("--upper", type=int, required=True, help="Upper tick")
    parser.add_argument("--execute", action="store_true", help="Actually run forge script (otherwise dry-run)")
    args = parser.parse_args()

    s = get_settings()
    log_info(f"Preparing {'EXECUTION' if args.execute else 'DRY-RUN'} for vault={s.vault}")
    log_info(f"Range suggestion: lower={args.lower}, upper={args.upper}")
    
    env = os.environ.copy()
    env["LOWER_TICK"] = str(args.lower)
    env["UPPER_TICK"] = str(args.upper)
    env["VAULT_ADDRESS"] = s.vault
    env["PRIVATE_KEY"] = s.private_key
    env["RPC_SEPOLIA"] = s.rpc_url
    
    if not args.execute:
        log_warn("Dry-run only — no transaction sent.")
        return

    # build forge command
    cmd = [
        "forge", "script", "script/RebalanceManual.s.sol:RebalanceManual",
        "--rpc-url", s.rpc_url,
        "--private-key", "${PRIVATE_KEY}",
        "--broadcast",
        "--sig", f"run()"
    ]
    # we rely on .env having LOWER_TICK/UPPER_TICK already exported
    subprocess.run(cmd, check=False)

if __name__ == "__main__":
    main()
