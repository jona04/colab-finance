// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "openzeppelin-contracts/contracts/access/Ownable.sol";
import { ReentrancyGuard } from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "../interfaces/IConcentratedLiquidityAdapter.sol";

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

        (address token0, address token1) = adapter.tokens();
        address spender = address(adapter);
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        uint256 need0 = (cap0 == 0) ? bal0 : (cap0 < bal0 ? cap0 : bal0);
        uint256 need1 = (cap1 == 0) ? bal1 : (cap1 < bal1 ? cap1 : bal1);
        
        _approveIfNeeded(token0, spender, need0);
        _approveIfNeeded(token1, spender, need1);

        uint128 L = adapter.rebalanceWithCaps(address(this), lower, upper, cap0, cap1);
        // if your adapter always recreates the NFT, refresh positionTokenId:
        positionTokenId = adapter.currentTokenId(address(this));
        emit Rebalanced(lower, upper, L);
    }

    function exitPositionToVault() external onlyOwner nonReentrant {
        adapter.exitPositionToVault(address(this));
        emit Exited();
    }

    function exitPositionAndWithdrawAll(address to) external onlyOwner nonReentrant {
        adapter.exitPositionAndWithdrawAll(address(this), to);
        positionTokenId = adapter.currentTokenId(address(this)); // likely zero after burn
        emit Exited();
    }

    function collectToVault() external onlyOwner nonReentrant returns (uint256 a0, uint256 a1) {
        (a0, a1) = adapter.collectToVault(address(this));
        emit Collected(a0, a1);
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
