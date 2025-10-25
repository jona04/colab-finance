import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from pydantic import Field
from functools import lru_cache

load_dotenv()

@dataclass
class Settings:
    AERO_QUOTER: str          # QuoterV2 do Aerodrome
    AERO_ROUTER: str          # SwapRouter do Aerodrome
    AERO_TICK_SPACINGS: str
    
    UNI_V3_ROUTER: str  # ex.: Base SwapRouter02
    UNI_V3_QUOTER: str  # ex.: Base QuoterV2
    DEFAULT_SWAP_POOL_FEE: int
    
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

    STABLE_TOKEN_ADDRESSES: list[str] = field(default_factory=list)
    
@lru_cache()
def get_settings() -> Settings:
    return Settings(
        AERO_QUOTER = os.getenv("AERO_QUOTER","0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"),
        AERO_ROUTER = os.getenv("AERO_ROUTER", "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"),
        AERO_TICK_SPACINGS = os.getenv("AERO_TICK_SPACINGS","1,10,100"),
        
        PRIVATE_KEY=os.environ.get("PRIVATE_KEY", ""),  # keep empty when missing
        RPC_URL_DEFAULT=os.environ["RPC_SEPOLIA"],
        STABLE_TOKEN_ADDRESSES=os.environ.get("STABLE_TOKEN_ADDRESSES",[]),
        
        UNI_V3_ROUTER=os.environ.get("UNI_V3_ROUTER","0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E"),
        UNI_V3_QUOTER=os.environ.get("UNI_V3_QUOTER","0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"),
        DEFAULT_SWAP_POOL_FEE=int(os.environ.get("DEFAULT_SWAP_POOL_FEE", 3000))
    )