import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from pydantic import Field
from functools import lru_cache

load_dotenv()

@dataclass
class Settings:
    PANCAKE_V3_QUOTER: str
    PANCAKE_V3_ROUTER: str
    PANCAKE_FACTORY: str
    PANCAKE_TICK_SPACINGS: str  # opcional, se usar
    PANCAKE_MASTERCHEF_V3: str
    
    AERO_POOL_FACTORY_AMM: str
    
    AERO_QUOTER: str          # QuoterV2 do Aerodrome
    AERO_ROUTER: str          # SwapRouter do Aerodrome
    AERO_ROUTER_AMM: str
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
        AERO_ROUTER_AMM = os.getenv("AERO_ROUTER_AMM", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"),
        AERO_TICK_SPACINGS = os.getenv("AERO_TICK_SPACINGS","1,10,100"),
        
        PRIVATE_KEY=os.environ.get("PRIVATE_KEY", ""),  # keep empty when missing
        RPC_URL_DEFAULT=os.environ["RPC_SEPOLIA"],
        STABLE_TOKEN_ADDRESSES=os.environ.get("STABLE_TOKEN_ADDRESSES",[]),
        
        UNI_V3_ROUTER=os.environ.get("UNI_V3_ROUTER","0x2626664c2603336E57B271c5C0b26F421741e481"),
        UNI_V3_QUOTER=os.environ.get("UNI_V3_QUOTER","0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"),
        DEFAULT_SWAP_POOL_FEE=int(os.environ.get("DEFAULT_SWAP_POOL_FEE", 3000)),
        AERO_POOL_FACTORY_AMM=os.environ.get("AERO_POOL_FACTORY_AMM", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"),
        PANCAKE_V3_QUOTER = os.getenv("PANCAKE_QUOTER","0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"),
        PANCAKE_V3_ROUTER = os.getenv("PANCAKE_V3_ROUTER","0x1b81D678ffb9C0263b24A97847620C99d213eB14"),
        PANCAKE_FACTORY = os.getenv("PANCAKE_FACTORY","0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
        PANCAKE_TICK_SPACINGS = os.getenv("PANCAKE_TICK_SPACINGS","1,10,100"),
        PANCAKE_MASTERCHEF_V3 = os.getenv("PANCAKE_MASTERCHEF_V3", "0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3")
    )