// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import { VaultEvents } from "../events/VaultEvents.sol";

/// @title Interface for SingleUserVault
/// @notice Single-owner vault that manages a single Uniswap V3 LP position with manual rebalances.
interface ISingleUserVault is VaultEvents {
    /// @notice Returns the current owner address.
    function owner() external view returns (address);

    /// @notice Returns the locked Uniswap V3 pool address.
    function pool() external view returns (address);

    /// @notice Returns the current Uniswap V3 position tokenId (0 if not opened).
    function positionTokenId() external view returns (uint);

    /// @notice Locks the pool by direct address. Can be called only once by owner.
    function setPoolOnce(address pool_) external;

    /// @notice Resolves pool via factory using tokens and fee, then locks it. Can be called only once by owner.
    function setPoolByFactory(address tokenA, address tokenB, uint24 fee) external;

    /// @notice Opens the initial LP position using provided tick bounds. Owner-only.
    function openInitialPosition(int24 lower, int24 upper) external;

    /// @notice Performs a manual rebalance to new tick bounds. Owner-only.
    function rebalance(int24 lower, int24 upper) external;

    /// @notice Withdraws all available token balances to owner. Owner-only.
    function withdrawAll() external;

    /// @notice Returns current tick bounds and liquidity for the managed position.
    function currentRange() external view returns (int24 lower, int24 upper, uint128 liquidity);

    /// @notice Returns true if the TWAP deviation against spot is within the configured bound.
    function twapOk() external view returns (bool);

    /// @notice Exits only the pool. Owner-only.
    function exitPositionToVault() external;

    /// @notice Exits de the pool and the vault. Owner -only.
    function exitAndWithdrawAll() external;
}
