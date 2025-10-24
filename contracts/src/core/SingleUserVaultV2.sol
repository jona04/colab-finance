// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "openzeppelin-contracts/contracts/access/Ownable.sol";
import { ReentrancyGuard } from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "../interfaces/IConcentratedLiquidityAdapter.sol";
import {ISwapRouterV3Minimal} from "../interfaces/ISwapRouterV3Minimal.sol";

/**
 * @title SingleUserVaultV2
 * @notice Dex-agnostic vault that delegates LP logic to an adapter.
 * @dev The vault holds tokens and (optionally) the LP NFT. It does not custody in the adapter.
 */
contract SingleUserVaultV2 is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    IConcentratedLiquidityAdapter public adapter;
    bool public poolSet;
    uint256 public positionTokenId;

    event PoolAdapterSet(address adapter);
    event PositionOpened(uint256 tokenId, int24 lower, int24 upper, uint128 liq);
    event Rebalanced(int24 lower, int24 upper, uint128 newLiq);
    event Exited();
    event Collected(uint256 amount0, uint256 amount1);
    event Staked();
    event Unstaked();
    event Swapped(address indexed router, address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut);
    
    /// @dev OZ v5 Ownable requires the base constructor argument.
    /// Pass the initial owner at deployment time.
    constructor(address _owner) Ownable(_owner) {
        // nothing else to do
    }

    /// @notice One-time set of the adapter (which implies pool/nfpm/gauge for this vault).
    function setPoolOnce(address _adapter) external onlyOwner {
        require(!poolSet, "already set");
        require(_adapter != address(0), "zero");
        adapter = IConcentratedLiquidityAdapter(_adapter);
        poolSet = true;
        emit PoolAdapterSet(_adapter);
    }

    /// @notice Current pool tick spacing (adapter passthrough).
    function tickSpacing() external view returns (int24) {
        return adapter.tickSpacing();
    }

    /// @notice Current pool slot0 (adapter passthrough).
    function slot0() external view returns (uint160, int24) {
        return adapter.slot0();
    }

    // ---- internal helper ----
    function _approveIfNeeded(address token, address spender, uint256 needed) internal {
        if (needed == 0) return;
        uint256 allowance = IERC20(token).allowance(address(this), spender);
        if (allowance < needed) {
            IERC20(token).forceApprove(spender, 0);
            IERC20(token).forceApprove(spender, type(uint256).max);
        }
    }

    /// ===== Mutations =====

    function openInitialPosition(int24 lower, int24 upper) external onlyOwner nonReentrant {
        require(poolSet, "no pool");

        (address token0, address token1) = adapter.tokens();
        address spender = address(adapter); // <- o adapter vai puxar via transferFrom
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        
        _approveIfNeeded(token0, spender, bal0);
        _approveIfNeeded(token1, spender, bal1);

        (uint256 tid, uint128 L) = adapter.openInitialPosition(address(this), lower, upper);
        positionTokenId = tid;
        emit PositionOpened(tid, lower, upper, L);
    }

    function rebalanceWithCaps(int24 lower, int24 upper, uint256 cap0, uint256 cap1)
        external
        onlyOwner
        nonReentrant
    {
        require(poolSet, "no pool");

        // Pre-approve adapter to pull up to the caps (or current balances if caps are 0)
        (address token0, address token1) = adapter.tokens();
        address spender = address(adapter);
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        uint256 need0 = (cap0 == 0) ? bal0 : (cap0 < bal0 ? cap0 : bal0);
        uint256 need1 = (cap1 == 0) ? bal1 : (cap1 < bal1 ? cap1 : bal1);
        
        _approveIfNeeded(token0, spender, need0);
        _approveIfNeeded(token1, spender, need1);

        uint128 L = adapter.rebalanceWithCaps(address(this), lower, upper, cap0, cap1);
        positionTokenId = adapter.currentTokenId(address(this));
        emit Rebalanced(lower, upper, L);
    }

    function exitPositionToVault() external onlyOwner nonReentrant {
        adapter.exitPositionToVault(address(this));
        positionTokenId = adapter.currentTokenId(address(this)); // likely 0
        emit Exited();
    }

    /**
     * @notice Close position (if any), move all funds to the vault, then transfer all vault balances to `to`.
     * @param to Recipient EOA/contract that will receive both tokens.
     */
    function exitPositionAndWithdrawAll(address to) external onlyOwner nonReentrant {
        require(to != address(0), "zero to");

        // 1) Close position and move funds to the vault
        adapter.exitPositionToVault(address(this));
        positionTokenId = adapter.currentTokenId(address(this)); // likely 0

        // 2) Transfer all vault balances of token0 and token1 to `to`
        (address token0, address token1) = adapter.tokens();

        uint256 b0 = IERC20(token0).balanceOf(address(this));
        if (b0 > 0) {
            IERC20(token0).safeTransfer(to, b0);
        }

        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b1 > 0) {
            IERC20(token1).safeTransfer(to, b1);
        }

        emit Exited();
    }

    function collectToVault() external onlyOwner nonReentrant returns (uint256 a0, uint256 a1) {
        (a0, a1) = adapter.collectToVault(address(this));
        emit Collected(a0, a1);
    }

    /**
     * @notice Swap exact amountIn of tokenIn -> tokenOut via Uniswap v3 Router, keeping proceeds in the vault.
     * @dev OnlyOwner, nonReentrant. Approves just-in-time the router, then resets approval to 0.
     * @param router Uniswap v3 router address (e.g., SwapRouter02 on Base)
     * @param tokenIn Input token address (must be in the vault)
     * @param tokenOut Output token address
     * @param fee Pool fee tier (e.g., 500, 3000, 10000)
     * @param amountIn Exact input amount (raw units)
     * @param amountOutMinimum Minimum acceptable output (raw units) for slippage protection
     * @param sqrtPriceLimitX96 Optional price limit (usually 0)
     */
    function swapExactIn(
        address router,
        address tokenIn,
        address tokenOut,
        uint24  fee,
        uint256 amountIn,
        uint256 amountOutMinimum,
        uint160 sqrtPriceLimitX96
    ) external onlyOwner nonReentrant returns (uint256 amountOut) {
        require(router != address(0), "router=0");
        require(amountIn > 0, "amountIn=0");
        // approve router to pull tokenIn from this vault
        _approveIfNeeded(tokenIn, router, amountIn);

        ISwapRouterV3Minimal.ExactInputSingleParams memory p =
        ISwapRouterV3Minimal.ExactInputSingleParams({
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            fee: fee,
            recipient: address(this),
            amountIn: amountIn,
            amountOutMinimum: amountOutMinimum,
            sqrtPriceLimitX96: sqrtPriceLimitX96
        });

        amountOut = ISwapRouterV3Minimal(router).exactInputSingle(p);

        // reset approval (defensive)
        IERC20(tokenIn).forceApprove(router, 0);

        emit Swapped(router, tokenIn, tokenOut, amountIn, amountOut);
    }



    // ===== staking (optional) =====

    function stake() external onlyOwner nonReentrant {
        adapter.stakePosition(address(this));
        emit Staked();
    }

    function unstake() external onlyOwner nonReentrant {
        adapter.unstakePosition(address(this));
        emit Unstaked();
    }

    function claimRewards() external onlyOwner nonReentrant {
        adapter.claimRewards(address(this));
    }

    // Minimal views for your bot/api
    function positionTokenIdView() external view returns (uint256) {
        return positionTokenId;
    }
}
