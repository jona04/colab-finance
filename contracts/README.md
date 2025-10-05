# SingleUserVault (Uniswap v3) — Base / Base Sepolia

> Vault single-owner para prover liquidez em Uniswap v3 com rebalance manual.  
> Pool pode ser definida por endereço ou descoberta via getPool(tokenA, tokenB, fee).  
> Cálculo de fees no MVP deve ser feito off-chain usando callStatic.collect (sem mover fundos).

---

## Sumário

- [Arquitetura](#arquitetura)
- [Endereços por rede](#endereços-por-rede)
- [Pré-requisitos](#pré-requisitos)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Runbook (operacional)](#runbook-operacional)
- [Comandos úteis](#comandos-úteis)
  - [Forge / Cast (básico)](#forge--cast-básico)
  - [Make (atalhos)](#make-atalhos)
  - [Scripts (forge script)](#scripts-forge-script)
  - [Cheat sheet `cast`](#cheat-sheet-cast)
- [Testes](#testes)
  - [Unit tests](#unit-tests)
  - [Fork tests](#fork-tests)
  - [Como rodar um nó fork local (opcional)](#como-rodar-um-nó-fork-local-opcional)
- [Higiene & DX](#higiene--dx)
  - [Gas report / snapshot](#gas-report--snapshot)
  - [Formatador (forge fmt)](#formatador-forge-fmt)
  - [Slither (análise estática)](#slither-análise-estática)
  - [Outras verificações](#outras-verificações)
  - [CI (exemplo GitHub Actions)](#ci-exemplo-github-actions)
- [Algoritmos (notas técnicas)](#algoritmos-notas-técnicas)
- [Solucionando problemas](#solucionando-problemas)
- [Cast CLI](#cast-cli)
- [Licença](#licença)

---

## Arquitetura


contracts/
├─ src/
│  ├─ core/SingleUserVault.sol                 # contrato principal
│  ├─ adapters/UniV3TwapOracle.sol             # helper de TWAP (observe)
│  ├─ interfaces/…                             # interfaces mínimas (Pool, NFPM, Factory, Vault)
│  ├─ addresses/Base*.sol                      # constantes por rede (endereços Uniswap)
│  ├─ errors/VaultErrors.sol                   # erros custom
│  └─ events/VaultEvents.sol                   # eventos
├─ script/
│  ├─ Deploy.s.sol                             # deploy do vault
│  ├─ SetPoolOnce.s.sol                        # travar pool por endereço
│  ├─ OpenInitialPosition.s.sol                # abrir posição inicial
│  ├─ RebalanceManual.s.sol                    # rebalance manual
│  └─ ViewState.s.sol                          # inspeção (somente leitura)
├─ test/
│  ├─ unit/                                    # testes de unidade
│  ├─ fork/                                    # testes em fork Base Sepolia
│  ├─ invariant/                               # invariantes básicas
│  └─ mocks/                                   # mocks mínimos
├─ foundry.toml / remappings.txt               # configs Foundry
├─ slither.config.json                         # config Slither
└─ Makefile                                    # atalhos

---

## Endereços por rede

Base (8453)  
- UniswapV3Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD  
- NonfungiblePositionManager (NFPM): 0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1  
- QuoterV2: 0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a  
- SwapRouter02: 0x2626664c2603336E57B271c5C0b26F421741e481  
- WETH: 0x4200000000000000000000000000000000000006

Sepolia (11155111)  
- UniswapV3Factory: 0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24  
- NonfungiblePositionManager (NFPM): 0x1238536071E1c677A632429e3655c799b22cDA52
- QuoterV2: 0xC5290058841028F1614F3A6F0F5816cAd0df5E27  
- SwapRouter02: 0x94cC0AaC535CCDB3C01d6787D6413C739ae12bc4  
- WETH: 0xfff9976782d46cc05630d1f6ebab18b2324d6b14
- USDC: 0x1c7d4b196cb0c7b01d743fbc6116a902379c7238

Observação: setPoolByFactory resolve o endereço da pool via NFPM.factory().getPool(tokenA, tokenB, fee).  
setPoolOnce valida que a pool informada pertence ao mesmo Factory do NFPM.

---

## Pré-requisitos

- Foundry (forge, cast, anvil)  
- Node.js LTS (scripts auxiliares)  
- Python 3 (slither)  
- RPC Base Sepolia (Alchemy/Infura/etc.)  
- Chave de dev para Base Sepolia

---

## Variáveis de ambiente

Crie .env (baseado no .env.example):

PRIVATE_KEY=0xSEU_DEV_PRIVATE_KEY
RPC_SEPOLIA=https://sepolia.base.org
RPC_BASE=https://mainnet.base.org
ETHERSCAN_API_KEY=chave_basescan
NFPM_ADDRESS=0x27F971cb582BF9E50F397e4d29a5C7A34f11faA2
VAULT_ADDRESS=0xSEU_VAULT (opcional para scripts de estado)

Exportar no shell também funciona: export VAR=....

---

## Runbook (operacional)

1) Deploy do vault (Base Sepolia)  
forge script script/Deploy.s.sol:Deploy \
  --rpc-url $RPC_SEPOLIA --broadcast --private-key $PRIVATE_KEY -vvv

2) Travar pool (direto)  
export VAULT_ADDRESS=0xSEU_VAULT  
export POOL_ADDRESS=0xPOOL  
forge script script/SetPoolOnce.s.sol:SetPoolOnce \
  --rpc-url $RPC_SEPOLIA --broadcast --private-key $PRIVATE_KEY -vvv

3) Enviar tokens para o vault  
Transferir token0/token1 para o VAULT_ADDRESS.

4) Abrir posição inicial  
export LOWER_TICK=-120  
export UPPER_TICK=-60  
forge script script/OpenInitialPosition.s.sol:OpenInitialPosition \
  --rpc-url $RPC_SEPOLIA --broadcast --private-key $PRIVATE_KEY -vvv

5) Rebalance manual  
export LOWER_TICK=-60  
export UPPER_TICK=0  
forge script script/RebalanceManual.s.sol:RebalanceManual \
  --rpc-url $RPC_SEPOLIA --broadcast --private-key $PRIVATE_KEY -vvv

6) Inspecionar estado  
export VAULT_ADDRESS=0xSEU_VAULT  
forge script script/ViewState.s.sol:ViewState --rpc-url $RPC_SEPOLIA -vvvv

---

## Comandos úteis

Forge / Cast (básico)  
forge build  
forge fmt  
forge test -vvv  
forge test -vvv --match-path test/unit/*  
forge test -vvv --fork-url $RPC_SEPOLIA --match-path test/fork/*  
cast call $VAULT_ADDRESS "owner()(address)"

Make (atalhos)  
build: forge build  
fmt: forge fmt  
test: forge test -vvv  
fork-test: forge test -vvv --fork-url \$\$RPC_SEPOLIA  
deploy-sepolia: forge script script/Deploy.s.sol:Deploy --rpc-url \$\$RPC_SEPOLIA --broadcast --private-key \$\$PRIVATE_KEY -vvv  
view: forge script script/ViewState.s.sol:ViewState --rpc-url \$\$RPC_SEPOLIA -vvvv  
gas: forge test --gas-report --match-path test/unit/*

Scripts (forge script)  
- Deploy.s.sol — deploya o vault  
- SetPoolOnce.s.sol — trava a pool por endereço  
- OpenInitialPosition.s.sol — abre posição  
- RebalanceManual.s.sol — coleta fees e reabre  
- ViewState.s.sol — inspeção  

Cheat sheet cast  
cast call $POOL "fee()(uint24)"  
cast call $POOL "tickSpacing()(int24)"  
cast call $POOL "token0()(address)"  
cast call $POOL "token1()(address)"  
cast call $POOL "slot0()(uint160,int24,uint16,uint16,uint16,uint8,bool)"  
cast call $VAULT "owner()(address)"  
cast call $VAULT "pool()(address)"  
cast call $VAULT "positionTokenId()(uint256)"  
cast call $VAULT "currentRange()(int24,int24,uint128)"  
cast call $VAULT "twapOk()(bool)"  
cast call $TOKEN "balanceOf(address)(uint256)" $VAULT  

---

## Testes

### Unit tests  
forge test -vvv --match-path "test/unit/*.t.sol"
#### ou, se tiver subpastas dentro de unit:
forge test -vvv --match-path "test/unit/**/*.t.sol"

### Fork tests  
export RPC_SEPOLIA="https://sepolia.base.org"
export NFPM_ADDRESS="0x27F971cb582BF9E50F397e4d29a5C7A34f11faA2"
export POOL_ADDRESS="0xSEU_ENDERECO_DA_POOL"

forge test -vvv --fork-url "$RPC_SEPOLIA" --match-path "test/fork/*.t.sol"

### Como rodar fork local  
export RPC_SEPOLIA="https://sepolia.base.org"
anvil --fork-url "$RPC_SEPOLIA" --chain-id 84532
#### em outro terminal:
forge test -vvv --rpc-url "http://127.0.0.1:8545" --match-path "test/fork/*.t.sol"

---

## Higiene & DX

Gas report  
forge test --gas-report --match-path test/unit/*

Formatador  
forge fmt

Slither  
pip install slither-analyzer  
slither . --filter-paths "lib|out|script|test"

Outras verificações  
npx solhint 'src/**/*.sol'  
forge inspect src/core/SingleUserVault.sol:SingleUserVault abi  
forge inspect src/core/SingleUserVault.sol:SingleUserVault bytecode  

CI exemplo GitHub Actions  
(arquivo .github/workflows/contracts-ci.yml com steps de build, test, fmt, slither)

---

## Algoritmos (notas técnicas)

- Validação de largura: upper > lower, width ∈ [minWidth, maxWidth]  
- Tick spacing: múltiplos de tickSpacing  
- TWAP vs Spot: abs(spotTick - twapTick) <= maxTwapDeviationTicks  
- Rebalance v0: collect → decreaseLiquidity 100% → burn → mint nova faixa com todo saldo  
- Fees off-chain: NFPM.callStatic.collect

---

## Solucionando problemas

- NotOwner em testes: use vm.startPrank/stopPrank  
- InvalidTickSpacing: consulte cast call $POOL "tickSpacing()(int24)"  
- Falta de fees: faça swaps nos dois sentidos e warp tempo  
- Verificação no BaseScan: forge verify-contract ...

---

## Cast CLI

> Atalhos para inspecionar e operar o vault diretamente pelo CLI `cast` (Foundry).  
> Copiar e colar no terminal, ajustando variáveis conforme necessário.


### Setup inicial
```bash
export RPC_SEPOLIA=https://sepolia.infura.io/v3/<SUA_INFURA_KEY>
export PRIVATE_KEY=0xSUA_CHAVE_DEV
export VAULT_ADDRESS=0xSEU_VAULT
export POOL_ADDRESS=0xSUA_POOL
```

### Infos da pool

```bash
cast call $POOL_ADDRESS "fee()(uint24)" --rpc-url $RPC_SEPOLIA
cast call $POOL_ADDRESS "tickSpacing()(int24)" --rpc-url $RPC_SEPOLIA
cast call $POOL_ADDRESS "slot0()(uint160,int24,uint16,uint16,uint16,uint8,bool)" --rpc-url $RPC_SEPOLIA
cast call $POOL_ADDRESS "token0()(address)" --rpc-url $RPC_SEPOLIA
cast call $POOL_ADDRESS "token1()(address)" --rpc-url $RPC_SEPOLIA
```

### Get pool Sepolia

```bash
cast call 0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24 \
  "getPool(address,address,uint24)(address)" \
  0xTOKEN_A \
  0xTOKEN_B \
  3000 \
  --rpc-url $RPC_SEPOLIA
```

### Create pool Sepolia

```bash

```

### Infos do vault

```bash
cast call $VAULT_ADDRESS "owner()(address)" --rpc-url $RPC_SEPOLIA
cast call $VAULT_ADDRESS "pool()(address)" --rpc-url $RPC_SEPOLIA
cast call $VAULT_ADDRESS "positionTokenId()(uint256)" --rpc-url $RPC_SEPOLIA
cast call $VAULT_ADDRESS "currentRange()(int24,int24,uint128)" --rpc-url $RPC_SEPOLIA
cast call $VAULT_ADDRESS "twapOk()(bool)" --rpc-url $RPC_SEPOLIA

```

### Saldos do vault

```bash
TOKEN0=$(cast call $POOL_ADDRESS "token0()(address)" --rpc-url $RPC_SEPOLIA)
TOKEN1=$(cast call $POOL_ADDRESS "token1()(address)" --rpc-url $RPC_SEPOLIA)

cast call $TOKEN0 "balanceOf(address)(uint256)" $VAULT_ADDRESS --rpc-url $RPC_SEPOLIA
cast call $TOKEN1 "balanceOf(address)(uint256)" $VAULT_ADDRESS --rpc-url $RPC_SEPOLIA

```


### Transações manuais

```bash
# Travar pool uma vez
cast send $VAULT_ADDRESS "setPoolOnce(address)" $POOL_ADDRESS \
  --rpc-url $RPC_SEPOLIA --private-key $PRIVATE_KEY

# Transferir token para o vault
cast send $TOKEN0 "transfer(address,uint256)" $VAULT_ADDRESS 1000000000000000000 \
  --rpc-url $RPC_SEPOLIA --private-key $PRIVATE_KEY

# Abrir posição inicial
cast send $VAULT_ADDRESS "openInitialPosition(int24,int24)" -120 -60 \
  --rpc-url $RPC_SEPOLIA --private-key $PRIVATE_KEY

# Rebalance manual
cast send $VAULT_ADDRESS "rebalance(int24,int24)" -60 0 \
  --rpc-url $RPC_SEPOLIA --private-key $PRIVATE_KEY

```


### Logs & eventos

```bash
# Última transação
TX=0xHASH_DA_TX
cast tx $TX --rpc-url $RPC_SEPOLIA --verbose

# Buscar eventos Rebalanced
TOPIC_REBALANCED=$(cast keccak "Rebalanced(uint256,int24,int24,uint256,uint256)")
cast logs --from-block 0 --to-block latest \
  --address $VAULT_ADDRESS --topics $TOPIC_REBALANCED \
  --rpc-url $RPC_SEPOLIA

```


### Utilidades diversas

```bash
# Encode/Decode ABI
cast abi-encode "rebalance(int24,int24)" -60 0
cast abi-decode "(int24,int24,uint256,uint256)" 0x...

# Conversões
cast to-wei 1 ether
cast from-wei 1000000000000000000
cast keccak "Rebalanced(uint256,int24,int24,uint256,uint256)"

```

### Fluxo típico com cast no MVP

- Setar pool (uma vez).
- Transferir token0/token1 pro vault.
- Abrir posição (via script OpenInitialPosition.s.sol ou diretamente com cast send se expuser método igual).
- Checar currentRange e saldos.
- Fazer swaps pequenos (pode usar um router ou a própria pool via swap (se souber fazer o callback)).
- Rebalance (script ou cast send) e inspecionar evento Rebalanced.
- ViewState para conferir tudo em uma tacada.

--

## Licença

MIT
