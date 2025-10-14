import os
from dotenv import load_dotenv
from dataclasses import dataclass
from pydantic import Field
from functools import lru_cache

load_dotenv()

@dataclass
class Settings:
    # signing / chain
    RPC_URL_DEFAULT: str
    PRIVATE_KEY: str  # hex 0x...

    # data roots (simulate DB)
    DATA_ROOT: str = "data"                   # ./data
    UNISWAP_ROOT: str = "uniswap"             # ./data/uniswap
    AERODROME_ROOT: str = "aerodrome"         # ./data/aerodrome

    # default TWAP/policies
    TWAP_WINDOW_SEC: int = 60
    MAX_TWAP_DEVIATION_TICKS: int = 50
    MIN_REBALANCE_COOLDOWN_SEC: int = 1800

    # generic
    ENV: str = Field(default="dev")
    LOG_LEVEL: str = Field(default="INFO")
    
@lru_cache()
def get_settings() -> Settings:
    return Settings(
        RPC_URL_DEFAULT=os.environ["RPC_SEPOLIA"],
        # twap_window=int(os.environ.get("TWAP_WINDOW", "60")),
        # max_twap_dev_ticks=int(os.environ.get("MAX_TWAP_DEVIATION_TICKS", "50")),
        # min_cooldown=int(os.environ.get("MIN_COOLDOWN", "1800")),
        # check_interval=int(os.environ.get("CHECK_INTERVAL", "30")),
        PRIVATE_KEY=os.environ.get("PRIVATE_KEY", ""),  # keep empty when missing

        # # Security / governance
        # read_only_mode=_bool(os.environ.get("READ_ONLY_MODE")),
        # allowed_user_ids=_csv(os.environ.get("ALLOWED_USER_IDS")),
        # block_dm=_bool(os.environ.get("BLOCK_DM")),

        # Alerts
        # alerts_cooldown_sec=int(os.environ.get("ALERTS_COOLDOWN_SEC", "60")),
        # alerts_dedup_window_sec=int(os.environ.get("ALERTS_DEDUP_WINDOW_SEC", "180")),
        # alert_out_of_range_minutes=int(os.environ.get("ALERT_OUT_OF_RANGE_MINUTES", "5")),
        # alert_twap_false_minutes=int(os.environ.get("ALERT_TWAP_FALSE_MINUTES", "3")),
        # alert_fees_usd_threshold=float(os.environ.get("ALERT_FEES_USD_THRESHOLD", "0")),
        # alert_rpc_fail_max=int(os.environ.get("ALERT_RPC_FAIL_MAX", "3")),
    )