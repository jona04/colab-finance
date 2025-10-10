// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Events emitted by SingleUserVault
interface VaultEvents {
    /// @notice Emitted when the pool address is locked for this vault.
    event PoolSet(address indexed pool);

    /// @notice Emitted after the initial position is opened.
    event Opened(uint indexed tokenId, int24 lower, int24 upper);

    /// @notice Emitted after a manual rebalance.
    event Rebalanced(uint indexed tokenId, int24 lower, int24 upper, uint fees0, uint fees1);

    /// @notice Emitted after funds are withdrawn from the vault.
    event Withdrawn(uint amount0, uint amount1);

    /// @notice Emitted after exit the vault/pool.
    event Exited(uint256 tokenId, uint256 sent0, uint256 sent1);
}
