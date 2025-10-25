// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";

import "../../interfaces/IConcentratedLiquidityAdapter.sol";
import "./interfaces/ISlipstreamPool.sol";
import "./interfaces/ISlipstreamNFPM.sol";
import "./interfaces/ISlipstreamGauge.sol";

/**
 * @title SlipstreamAdapter
 * @dev Adapter for Aerodrome Slipstream concentrated liquidity.
 * It wraps the Slipstream NFPM & Pool, and optionally stakes the resulting NFT into a Gauge.
 * All token flows happen between the Vault and protocol contracts; the adapter holds nothing.
 */
contract SlipstreamAdapter is IConcentratedLiquidityAdapter {
    using SafeERC20 for IERC20;

    address public immutable override pool;
    address public immutable override nfpm;
    address public immutable override gauge; // opcional; pode ser zero

    // --- guard params (copiados do V1 para manter comportamento) ---
    uint256 public minCooldown = 30 minutes;
    int24   public minWidth    = 60;
    int24   public maxWidth    = 200_000;
    int24   public maxTwapDeviationTicks = 50; // ~0.5%
    uint32  public twapWindow  = 60;

    // vault => lastRebalance
    mapping(address => uint256) public lastRebalance;

    // vault => tokenId (NFT É mantido pelo ADAPTER)
    mapping(address => uint256) private _tokenId;

    constructor(address _pool, address _nfpm, address _gauge) {
        require(_pool != address(0) && _nfpm != address(0), "zero");
        pool = _pool;
        nfpm = _nfpm;
        gauge = _gauge; // can be 0x0 if you don't want staking
    }

    function tickSpacing() external view override returns (int24) {
        return ISlipstreamPool(pool).tickSpacing();
    }

    function slot0() external view override returns (uint160 sqrtPriceX96, int24 tick) {
        (sqrtPriceX96, tick,,,,) = ISlipstreamPool(pool).slot0();
    }

    function tokens() external view override returns (address token0, address token1) {
        token0 = ISlipstreamPool(pool).token0();
        token1 = ISlipstreamPool(pool).token1();
    }

    function currentTokenId(address vault) public view override returns (uint256) {
        return _tokenId[vault];
    }

    function currentRange(address vault)
        external
        view
        returns (int24 lower, int24 upper, uint128 liquidity)
    {
        uint256 tid = _tokenId[vault];
        require(tid != 0, "no position");
        ( , , , , , int24 l, int24 u, uint128 L, , , , ) = ISlipstreamNFPM(nfpm).positions(tid);
        return (l, u, L);
    }


    // ===== internals =====

    function _approveIfNeeded(address token, address spender, uint256 amount) internal {
        if (amount == 0) return;
        uint256 allowance = IERC20(token).allowance(address(this), spender);
        if (allowance < amount) {
            IERC20(token).forceApprove(spender, 0);
            IERC20(token).forceApprove(spender, type(uint256).max);
        }
    }

    // ===== mutations (IConcentratedLiquidityAdapter) =====

    function openInitialPosition(
        address vault,
        int24 tickLower,
        int24 tickUpper
    ) external override returns (uint256 tokenId, uint128 liquidity) {
        require(_tokenId[vault] == 0, "already opened");

        (address token0, address token1) = tokens();

        // Saldos no VAULT
        uint256 a0 = IERC20(token0).balanceOf(vault);
        uint256 a1 = IERC20(token1).balanceOf(vault);
        require(a0 > 0 || a1 > 0, "no funds");

        // Puxa tokens do VAULT -> ADAPTER (Vault deve ter aprovado o adapter)
        if (a0 > 0) IERC20(token0).safeTransferFrom(vault, address(this), a0);
        if (a1 > 0) IERC20(token1).safeTransferFrom(vault, address(this), a1);

        // Aprova NFPM
        _approveIfNeeded(token0, nfpm, a0);
        _approveIfNeeded(token1, nfpm, a1);

        // Mint — NFT fica no ADAPTER
        ISlipstreamNFPM.MintParams memory p = ISlipstreamNFPM.MintParams({
            token0: token0,
            token1: token1,
            tickSpacing: ISlipstreamPool(pool).tickSpacing(),
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: a0,
            amount1Desired: a1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp,
            sqrtPriceX96: 0 // 0 = sem init-price; exige pool inicializado
        });

        (tokenId, liquidity,,) = ISlipstreamNFPM(nfpm).mint(p);
        _tokenId[vault] = tokenId;

        // Devolve sobras para o VAULT
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        // Stake (opcional)
        if (gauge != address(0)) {
            // Como o ADAPTER é dono, pode depositar diretamente
            ISlipstreamGauge(gauge).deposit(tokenId);
        }

        lastRebalance[vault] = block.timestamp;
    }


    function rebalanceWithCaps(
        address vault,
        int24 tickLower,
        int24 tickUpper,
        uint256 cap0,
        uint256 cap1
    ) external override returns (uint128 newLiquidity) {
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no position");

        // 0) Se staked, desestacar primeiro
        if (gauge != address(0) && ISlipstreamGauge(gauge).stakedContains(address(this), tokenId)) {
            ISlipstreamGauge(gauge).withdraw(tokenId);
        }

        // 1) Collect fees para o ADAPTER (mantém no adapter)
        ISlipstreamNFPM(nfpm).collect(
            ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // 2) Remove toda a liquidez (se houver) e coleta "owed"
        (, , , , , , , uint128 liq, , , , ) = ISlipstreamNFPM(nfpm).positions(tokenId);
        if (liq > 0) {
            ISlipstreamNFPM(nfpm).decreaseLiquidity(
                ISlipstreamNFPM.DecreaseLiquidityParams({
                    tokenId: tokenId,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp
                })
            );
            ISlipstreamNFPM(nfpm).collect(
                ISlipstreamNFPM.CollectParams({
                    tokenId: tokenId,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
        }

        // 3) Burn antigo NFT
        ISlipstreamNFPM(nfpm).burn(tokenId);
        _tokenId[vault] = 0;

        // 4) Monta amounts finais respeitando caps; puxa déficits do VAULT
        (address token0, address token1) = tokens();

        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));

        uint256 want0 = (cap0 == 0) ? type(uint256).max : cap0;
        uint256 want1 = (cap1 == 0) ? type(uint256).max : cap1;

        uint256 use0 = bal0;
        uint256 use1 = bal1;

        if (want0 > use0) {
            uint256 deficit0 = want0 - use0;
            uint256 v0 = IERC20(token0).balanceOf(vault);
            uint256 pull0 = deficit0 > v0 ? v0 : deficit0;
            if (pull0 > 0) {
                IERC20(token0).safeTransferFrom(vault, address(this), pull0);
                use0 += pull0;
            }
        }
        if (want1 > use1) {
            uint256 deficit1 = want1 - use1;
            uint256 v1 = IERC20(token1).balanceOf(vault);
            uint256 pull1 = deficit1 > v1 ? v1 : deficit1;
            if (pull1 > 0) {
                IERC20(token1).safeTransferFrom(vault, address(this), pull1);
                use1 += pull1;
            }
        }

        // 5) Mint nova posição (NFT no ADAPTER)
        _approveIfNeeded(token0, nfpm, use0);
        _approveIfNeeded(token1, nfpm, use1);

        ISlipstreamNFPM.MintParams memory p = ISlipstreamNFPM.MintParams({
            token0: token0,
            token1: token1,
            tickSpacing: ISlipstreamPool(pool).tickSpacing(),
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: use0,
            amount1Desired: use1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp,
            sqrtPriceX96: 0
        });

        (uint256 newTid, uint128 L,,) = ISlipstreamNFPM(nfpm).mint(p);
        _tokenId[vault] = newTid;
        newLiquidity = L;

        // 6) Devolve sobras para o VAULT
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        // 7) Restake (se configurado)
        if (gauge != address(0)) {
            ISlipstreamGauge(gauge).deposit(newTid);
        }

        lastRebalance[vault] = block.timestamp;
    }

    function exitPositionToVault(address vault) external override {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;

        // Se staked, retirar
        if (gauge != address(0) && ISlipstreamGauge(gauge).stakedContains(address(this), tokenId)) {
            ISlipstreamGauge(gauge).withdraw(tokenId);
        }

        // collect -> decrease -> collect -> burn (mantendo no adapter)
        ISlipstreamNFPM(nfpm).collect(ISlipstreamNFPM.CollectParams({
            tokenId: tokenId,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        }));

        (, , , , , , , uint128 liq, , , , ) = ISlipstreamNFPM(nfpm).positions(tokenId);
        if (liq > 0) {
            ISlipstreamNFPM(nfpm).decreaseLiquidity(ISlipstreamNFPM.DecreaseLiquidityParams({
                tokenId: tokenId,
                liquidity: liq,
                amount0Min: 0,
                amount1Min: 0,
                deadline: block.timestamp
            }));
            ISlipstreamNFPM(nfpm).collect(ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            }));
        }

        ISlipstreamNFPM(nfpm).burn(tokenId);
        _tokenId[vault] = 0;

        // Envia todos os saldos ao VAULT
        (address token0, address token1) = tokens();
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b0 > 0) IERC20(token0).safeTransfer(vault, b0);
        if (b1 > 0) IERC20(token1).safeTransfer(vault, b1);
    }

    function collectToVault(address vault) external override returns (uint256 amount0, uint256 amount1) {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return (0, 0);

        // Coleta para o ADAPTER…
        (amount0, amount1) = ISlipstreamNFPM(nfpm).collect(
            ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // …e envia ao VAULT
        (address token0, address token1) = tokens();
        if (amount0 > 0) IERC20(token0).safeTransfer(vault, amount0);
        if (amount1 > 0) IERC20(token1).safeTransfer(vault, amount1);
    }

    // ===== staking helpers =====

    function stakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");
        ISlipstreamGauge(gauge).deposit(tokenId);
    }

    function unstakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");
        ISlipstreamGauge(gauge).withdraw(tokenId);
    }

    function claimRewards(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;
        // Preferimos claim por tokenId (há também overload getReward(address))
        ISlipstreamGauge(gauge).getReward(tokenId);
    }
}
