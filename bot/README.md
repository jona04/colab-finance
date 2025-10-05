# 🦾 Uni-Range-Bot  
**Automated Uniswap v3 Vault Observer & Strategy Runner**

---

## 📚 Summary
1. [Overview](#overview)  
2. [Architecture](#architecture)  
3. [Setup & Installation](#setup--installation)  
4. [Environment Variables](#environment-variables)  
5. [Folder Structure](#folder-structure)  
6. [Running the Bot](#running-the-bot)  
7. [CLI Utilities](#cli-utilities)  
8. [Logs & State](#logs--state)  
9. [Next Steps](#next-steps)

---

## 🧠 Overview
`uni-range-bot` is a Python-based automation bot for **observing**, **analyzing**, and **rebalancing** Uniswap v3 vaults.  
It reads live on-chain data, computes market metrics, tracks performance, and can execute range adjustments (manually or automatically).

### Core Features
- Reads **real vault and pool data** via Web3  
- Calculates **spot price**, **volatility**, and **PnL (ΔUSD)**  
- Detects **out-of-range** positions  
- Evaluates **strategy JSONs** dynamically  
- Runs as a **CLI tool** or continuous background service  

---

## 🏗 Architecture
The bot is modular, designed to separate concerns:
- **Observer layer:** handles all on-chain reads (VaultObserver)
- **Chain layer:** Web3 connection + ABI contracts
- **Strategy layer:** JSON-defined logic + registry of handlers
- **Execution layer:** manual/dry-run commands via `bot.exec`
- **State management:** lightweight JSON persistence (`state.json`)

Flow:

Chain → VaultObserver → Strategies → Executor


---

## ⚙️ Setup & Installation

### 1. Clone the repository

git clone https://github.com/
cd uni-range-bot


### 2. Create and activate a virtual environment


python -m venv venv
source venv/bin/activate
pip install -r bot/requirements.txt


### 3. Configure environment variables
Create a `.env` file in the project root:


RPC_URL="https://polygon-amoy.g.alchemy.com/v2/
POOL="0x..." # Uniswap v3 Pool
NFPM="0x..." # NonfungiblePositionManager
VAULT="0x..." # Vault contract
TWAP_WINDOW=60 # Seconds for TWAP tick
CHECK_INTERVAL=30 # Loop sleep interval
STRATEGIES_FILE="bot/strategy/examples/strategies.json"


---

## 🔧 Environment Variables

| Variable | Description |
|-----------|-------------|
| `RPC_URL` | Blockchain RPC endpoint |
| `POOL` | Address of the Uniswap v3 pool |
| `NFPM` | NonfungiblePositionManager contract |
| `VAULT` | Vault contract to observe |
| `TWAP_WINDOW` | TWAP averaging window in seconds |
| `CHECK_INTERVAL` | Loop delay in seconds |
| `STRATEGIES_FILE` | JSON file path containing strategy definitions |

---

## 🗂 Folder Structure


```bash
.
├── bot
│ ├── alerts.py
│ ├── chain.py
│ ├── config.py
│ ├── exec.py
│ ├── init.py
│ ├── main.py
│ ├── observer
│ │ ├── init.py
│ │ ├── state_manager.py
│ │ └── vault_observer.py
│ ├── requirements.txt
│ ├── state.json
│ ├── status.py
│ ├── strategy
│ │ ├── examples
│ │ ├── init.py
│ │ └── registry.py
│ ├── telegram_client.py
│ └── utils
│ ├── formatters.py
│ ├── init.py
│ ├── log.py
│ ├── math_univ3.py
│ ├── ticks.py
│ └── volatility.py
└── state.json
```


---

## 🚀 Running the Bot

### 1. Continuous observation loop
Runs strategies automatically every N seconds:


python -m bot.main


### 2. View current vault status
Prints formatted vault, pool, and USD data:


python -m bot.status



Example output:


=== RANGE & PRICES ===
USDC/ETH: [13793.13 , 16846.80]
ETH/USDC: [0.0000593585 , 0.0000724999]
STATE side=inside | inRange=True | pct_outside_tick≈0.000% | vol=0.000%
FEES uncollected: 0.037487 USDC + 0.000000 WETH (≈ $0.0375)

ASSETS
Idle (vault): 0.037487 USDC | 0.000000 WETH (≈ $0.04)
In position: 16.44 USDC | 0.00098 WETH (≈ $16.44)
Totals: 16.48 USDC eq. | 0.00098 WETH (≈ $16.48)
Composition: 50.13% USDC | 49.87% WETH | Spot=16286.30 USDC/ETH



---

## 💻 CLI Utilities

### `/status`
Displays one-time snapshot including:
- Tick, range, and spacing
- Spot price (ETH/USDC and inverse)
- Range bounds (sorted)
- Fees collected and USD estimation
- Vault balance breakdown (idle + in position)
- PnL vs baseline (`ΔUSD`)

### `/alerts`
Lists last N strategy alerts from `state.json`:



python -m bot.alerts


### `/exec`
Manual rebalance executor:


python -m bot.exec --lower 179000 --upper 181000 --execute


- Add `--execute` to actually trigger transaction  
- Without flag = dry-run (only logs ticks and target range)

---

## 📈 Logs & State

**Logs:** structured `[HH:MM:SS][LEVEL]` format  
Example:


[20:35:50][INFO] USD Value=$16.48 | ΔUSD=+11.77 | Baseline=$4.71 | Spot USDC/ETH=16286.30



**State file (`bot/state.json`):**
Stores:
- Entry price
- Baseline USD
- Alerts (last 100)
- Out-of-range timestamps
- Last snapshot

---

## 🔮 Next Steps

✅ Phase 1 – Core observer, status CLI, and live vault metrics  
✅ Phase 2 – Manual executor (dry-run + forge integration)  
⬜ Phase 3 – Telegram notifications (`telegram_client.py`)  
⬜ Phase 4 – Strategy-triggered rebalancing  
⬜ Phase 5 – API server for dashboard integration  
⬜ Phase 6 – Multi-vault management and reporting  

---

### 🧩 License
MIT © 2025 — ColabFinance Research
