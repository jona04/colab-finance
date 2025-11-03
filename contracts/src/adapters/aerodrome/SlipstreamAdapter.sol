// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC721Receiver} from "openzeppelin-contracts/contracts/token/ERC721/IERC721Receiver.sol";

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
contract SlipstreamAdapter is IConcentratedLiquidityAdapter, IERC721Receiver {
    using SafeERC20 for IERC20;

    // immutable protocol addresses
    address public immutable override pool;
    address public immutable override nfpm;
    address public immutable override gauge; // can be zero if no staking

    // vault state
    mapping(address => uint256) private _tokenId;
    mapping(address => uint256) public lastRebalance;

    // config params
    uint256 public minCooldown = 1 minutes;
    int24   public minWidth    = 5;
    int24   public maxWidth    = 900_000;
    int24   public maxTwapDeviationTicks = 50;
    uint32  public twapWindow  = 60;

    constructor(address _pool, address _nfpm, address _gauge) {
        require(_pool != address(0) && _nfpm != address(0), "zero");
        pool = _pool;
        nfpm = _nfpm;
        gauge = _gauge;
    }

    function onERC721Received(
        address /*operator*/,
        address /*from*/,
        uint256 /*tokenId*/,
        bytes calldata /*data*/
    ) external pure override returns (bytes4) {
        return IERC721Receiver.onERC721Received.selector;
    }

    // --- View helpers ---
    function tokens() public view override returns (address token0, address token1) {
        token0 = ISlipstreamPool(pool).token0();
        token1 = ISlipstreamPool(pool).token1();
    }

    function tickSpacing() external view override returns (int24) {
        return ISlipstreamPool(pool).tickSpacing();
    }

    function slot0() external view override returns (uint160 sqrtPriceX96, int24 tick) {
        (sqrtPriceX96, tick,,,,) = ISlipstreamPool(pool).slot0();
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


    // --- internal ---
    function _approveIfNeeded(address token, address spender, uint256 amount) internal {
        if (amount == 0) return;
        uint256 allowance = IERC20(token).allowance(address(this), spender);
        if (allowance < amount) {
            IERC20(token).forceApprove(spender, 0);
            IERC20(token).forceApprove(spender, type(uint256).max);
        }
    }

    function _isStaked(uint256 tokenId) internal view returns (bool) {
        if (gauge == address(0) || tokenId == 0) return false;
        return ISlipstreamGauge(gauge).stakedContains(address(this), tokenId);
    }

    // ================================================================
    // =============== MAIN LOGIC =====================================
    // ================================================================

    /**
     * @notice Opens the initial LP position. NFT is held by adapter.
     */
    function openInitialPosition(
        address vault,
        int24 tickLower,
        int24 tickUpper
    ) external override returns (uint256 tokenId, uint128 liquidity) {
        require(_tokenId[vault] == 0, "already opened");

        (address token0, address token1) = tokens();
        uint256 a0 = IERC20(token0).balanceOf(vault);
        uint256 a1 = IERC20(token1).balanceOf(vault);
        require(a0 > 0 || a1 > 0, "no funds");

        // pull tokens from vault
        if (a0 > 0) IERC20(token0).safeTransferFrom(vault, address(this), a0);
        if (a1 > 0) IERC20(token1).safeTransferFrom(vault, address(this), a1);

        _approveIfNeeded(token0, nfpm, a0);
        _approveIfNeeded(token1, nfpm, a1);

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
            sqrtPriceX96: 0
        });

        (tokenId, liquidity,,) = ISlipstreamNFPM(nfpm).mint(p);
        _tokenId[vault] = tokenId;

        // return leftovers to vault
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        lastRebalance[vault] = block.timestamp;
    }

    /**
     * @notice Claim gauge rewards and forward them to vault.
     */
    function claimRewards(address vault) public override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;

        // 1. Claim pending rewards (both variants)
        try ISlipstreamGauge(gauge).getReward(tokenId) {} catch {}
        try ISlipstreamGauge(gauge).getReward(address(this)) {} catch {}

        // 2. Identify reward token and sweep balance to vault
        address rewardToken;
        try ISlipstreamGauge(gauge).rewardToken() returns (address rt) {
            rewardToken = rt;
        } catch {
            rewardToken = address(0);
        }

        if (rewardToken != address(0)) {
            uint256 bal = IERC20(rewardToken).balanceOf(address(this));
            if (bal > 0) IERC20(rewardToken).safeTransfer(vault, bal);
        }
    }

    /**
     * @notice Stake the current position in the gauge.
     */
    function stakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");

        // Approve once (no reset needed)
        ISlipstreamNFPM(nfpm).setApprovalForAll(gauge, true);
        ISlipstreamGauge(gauge).deposit(tokenId);
    }

    /**
     * @notice Unstake from gauge, claiming rewards first.
     */
    function unstakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");

        // 1. claim rewards before unstake
        claimRewards(vault);

        // 2. withdraw from gauge
        ISlipstreamGauge(gauge).withdraw(tokenId);
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
        
        require(!_isStaked(tokenId), "staked");

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

        lastRebalance[vault] = block.timestamp;
    }

    
    /**
     * @notice Exit position completely. Reverts if still staked.
     */
    function exitPositionToVault(address vault) external override {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;

        // safety: prevent exit while staked
        require(!_isStaked(tokenId), "position staked");

        // collect + remove liquidity
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
        
        // also sweep any gauge reward tokens
        if (gauge != address(0)) {
            try this.claimRewards(vault) {} catch {}
        }

        // burn NFT
        ISlipstreamNFPM(nfpm).burn(tokenId);
        _tokenId[vault] = 0;

        // send all balances to vault
        (address token0, address token1) = tokens();
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b0 > 0) IERC20(token0).safeTransfer(vault, b0);
        if (b1 > 0) IERC20(token1).safeTransfer(vault, b1);
    }

    /**
     * @notice Collect fees from NFPM and forward to vault.
     */
    function collectToVault(address vault)
        external
        override
        returns (uint256 amount0, uint256 amount1)
    {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return (0, 0);

        (amount0, amount1) = ISlipstreamNFPM(nfpm).collect(
            ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        (address token0, address token1) = tokens();
        if (amount0 > 0) IERC20(token0).safeTransfer(vault, amount0);
        if (amount1 > 0) IERC20(token1).safeTransfer(vault, amount1);
    }
}
