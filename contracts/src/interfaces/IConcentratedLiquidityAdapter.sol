// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title IConcentratedLiquidityAdapter
 * @dev Generic interface an LP adapter must implement so the vault can be DEX-agnostic.
 */
interface IConcentratedLiquidityAdapter {
    /// @notice Immutable addresses needed by the adapter (pool, nfpm, gauge...).
    function pool() external view returns (address);
    function nfpm() external view returns (address);
    function gauge() external view returns (address);

    /// @notice Returns current pool meta.
    function tickSpacing() external view returns (int24);
    function slot0() external view returns (uint160 sqrtPriceX96, int24 tick);
    function tokens() external view returns (address token0, address token1);

    /// @notice Returns current position tokenId (0 if none).
    function currentTokenId(address vault) external view returns (uint256);

    /// @notice Open initial range (mint position). Returns tokenId and liquidity.
    function openInitialPosition(
        address vault,
        int24 tickLower,
        int24 tickUpper
    ) external returns (uint256 tokenId, uint128 liquidity);

    /// @notice Rebalance width/caps. Implementations can burn & mint or adjust as needed.
    function rebalanceWithCaps(
        address vault,
        int24 tickLower,
        int24 tickUpper,
        uint256 cap0,     // 0 = ignore
        uint256 cap1      // 0 = ignore
    ) external returns (uint128 newLiquidity);

    /// @notice Exit position to vault: remove all liquidity and keep tokens in vault.
    function exitPositionToVault(address vault) external;

    /// @notice Collect pending fees to vault.
    function collectToVault(address vault) external returns (uint256 amount0, uint256 amount1);

    /// ===== Optional staking support (Gauge) =====
    function stakePosition(address vault) external;
    function unstakePosition(address vault) external;
    function claimRewards(address vault) external;
}
