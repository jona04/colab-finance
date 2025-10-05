import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    rpc_url: str
    vault: str
    pool: str
    nfpm: str
    twap_window: int
    max_twap_dev_ticks: int
    min_cooldown: int
    check_interval: int
    private_key: str

def get_settings() -> Settings:
    return Settings(
        rpc_url=os.environ["RPC_SEPOLIA"],
        vault=os.environ["VAULT_ADDRESS"],
        pool=os.environ["POOL_ADDRESS"],
        nfpm=os.environ["NFPM_ADDRESS"],
        twap_window=int(os.environ.get("TWAP_WINDOW", "60")),
        max_twap_dev_ticks=int(os.environ.get("MAX_TWAP_DEVIATION_TICKS", "50")),
        min_cooldown=int(os.environ.get("MIN_COOLDOWN", "1800")),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "30")),
        private_key=str(os.environ.get("PRIVATE_KEY"))   
    )
