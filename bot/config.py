# bot/config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    rpc_url: str
    # vault: str
    # pool: str
    # nfpm: str
    twap_window: int
    max_twap_dev_ticks: int
    min_cooldown: int
    check_interval: int
    private_key: str

    # --- Security / governance ---
    read_only_mode: bool                 # block any on-chain mutations
    allowed_user_ids: list[str]          # optional per-user allow-list
    block_dm: bool                       # refuse private chats (force group/channel)

    # --- Alerts ---
    alerts_cooldown_sec: int             # cooldown between identical alerts
    alerts_dedup_window_sec: int         # ignore identical payload inside this window
    alert_out_of_range_minutes: int      # how long out of range before alerting
    alert_twap_false_minutes: int        # how long twapOk=false before alerting
    alert_fees_usd_threshold: float      # fire when uncollected fees > threshold
    alert_rpc_fail_max: int              # consecutive RPC failures to alert


def _csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]

def _bool(s: str | None) -> bool:
    return str(s or "").strip().lower() in ("1", "true", "yes", "y", "on")

def get_settings() -> Settings:
    return Settings(
        rpc_url=os.environ["RPC_SEPOLIA"],
        # vault=os.environ["VAULT_ADDRESS"],
        # pool=os.environ["POOL_ADDRESS"],
        # nfpm=os.environ["NFPM_ADDRESS"],
        twap_window=int(os.environ.get("TWAP_WINDOW", "60")),
        max_twap_dev_ticks=int(os.environ.get("MAX_TWAP_DEVIATION_TICKS", "50")),
        min_cooldown=int(os.environ.get("MIN_COOLDOWN", "1800")),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "30")),
        private_key=os.environ.get("PRIVATE_KEY", ""),  # keep empty when missing

        # Security / governance
        read_only_mode=_bool(os.environ.get("READ_ONLY_MODE")),
        allowed_user_ids=_csv(os.environ.get("ALLOWED_USER_IDS")),
        block_dm=_bool(os.environ.get("BLOCK_DM")),

        # Alerts
        alerts_cooldown_sec=int(os.environ.get("ALERTS_COOLDOWN_SEC", "60")),
        alerts_dedup_window_sec=int(os.environ.get("ALERTS_DEDUP_WINDOW_SEC", "180")),
        alert_out_of_range_minutes=int(os.environ.get("ALERT_OUT_OF_RANGE_MINUTES", "5")),
        alert_twap_false_minutes=int(os.environ.get("ALERT_TWAP_FALSE_MINUTES", "3")),
        alert_fees_usd_threshold=float(os.environ.get("ALERT_FEES_USD_THRESHOLD", "0")),
        alert_rpc_fail_max=int(os.environ.get("ALERT_RPC_FAIL_MAX", "3")),
    )
