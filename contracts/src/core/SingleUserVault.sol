// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import { ReentrancyGuard } from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import { ISingleUserVault } from "../interfaces/ISingleUserVault.sol";
import { IUniV3PoolMinimal } from "../interfaces/IUniV3PoolMinimal.sol";
import { IUniswapV3FactoryMinimal } from "../interfaces/IUniswapV3FactoryMinimal.sol";
import { INonfungiblePositionManagerMinimal as NFPM } from
    "../interfaces/INonfungiblePositionManagerMinimal.sol";
import { VaultEvents } from "../events/VaultEvents.sol";
import { VaultErrors } from "../errors/VaultErrors.sol";

// OpenZeppelin
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { SafeERC20 } from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

// Local TWAP helper
import { UniV3TwapOracle } from "../adapters/UniV3TwapOracle.sol";

/// @title SingleUserVault (Uniswap V3)
/// @notice Single-owner vault that manages a single Uniswap V3 position. Pool is locked once set.
/// @dev V0 is intentionally minimal: manual operations only, no shares, no keeper fee.
contract SingleUserVault is ISingleUserVault, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // -------------------------
    // Immutable / configuration
    // -------------------------

    /// @inheritdoc ISingleUserVault
    address public owner;

    /// @notice NonfungiblePositionManager address for the target chain.
    address public immutable nfpm;

    // -------------------------
    // State
    // -------------------------

    /// @inheritdoc ISingleUserVault
    address public pool;

    /// @inheritdoc ISingleUserVault
    uint public positionTokenId;

    // Guard parameters (documented in README / NatSpec)
    uint public minCooldown; // seconds
    int24 public minWidth; // ticks
    int24 public maxWidth; // ticks
    int24 public maxTwapDeviationTicks; // basis points
    uint32 public twapWindow; // seconds
    uint public lastRebalance; // timestamp

    // -------------------------
    // Constructor
    // -------------------------

    /// @param _nfpm NonfungiblePositionManager address for the active network.
    constructor(address _nfpm) {
        if (_nfpm == address(0)) revert VaultErrors.ZeroAddress();
        owner = msg.sender;
        nfpm = _nfpm;

        // Defaults (can be adjusted in a future version with setters if needed)
        minCooldown = 30 minutes;
        minWidth = 60; // example placeholder (ticks)
        maxWidth = 200_000; // example placeholder (ticks)
        maxTwapDeviationTicks = 50; // ≈0,5% de desvio (1 tick ~ 0,01%)
        twapWindow = 60; // 60s
    }

    // -------------------------
    // Modifiers
    // -------------------------

    modifier onlyOwner() {
        if (msg.sender != owner) revert VaultErrors.NotOwner();
        _;
    }

    modifier poolSet() {
        if (pool == address(0)) revert VaultErrors.PoolNotSet();
        _;
    }

    // -------------------------
    // ISingleUserVault
    // -------------------------

    /// @inheritdoc ISingleUserVault
    function setPoolOnce(address pool_) external onlyOwner {
        if (pool != address(0)) revert VaultErrors.PoolAlreadySet();
        if (pool_ == address(0)) revert VaultErrors.ZeroAddress();

        address expectedFactory = NFPM(nfpm).factory();
        if (IUniV3PoolMinimal(pool_).factory() != expectedFactory) {
            revert VaultErrors.InvalidFactory();
        }

        pool = pool_;
        emit PoolSet(pool_);
    }

    /// @inheritdoc ISingleUserVault
    function setPoolByFactory(address tokenA, address tokenB, uint24 fee) external onlyOwner {
        if (pool != address(0)) revert VaultErrors.PoolAlreadySet();
        if (tokenA == address(0) || tokenB == address(0)) revert VaultErrors.ZeroAddress();

        // Resolve factory from NFPM to avoid network-specific constants here.
        address factory = NFPM(nfpm).factory();
        if (factory == address(0)) revert VaultErrors.InvalidFactory();

        address resolved = IUniswapV3FactoryMinimal(factory).getPool(tokenA, tokenB, fee);
        if (resolved == address(0)) revert VaultErrors.InvalidFactory(); // using InvalidFactory for "pool not found"
        pool = resolved;
        emit PoolSet(resolved);
    }

    /// @inheritdoc ISingleUserVault
    function openInitialPosition(int24 lower, int24 upper)
        external
        onlyOwner
        poolSet
        nonReentrant
    {
        if (positionTokenId != 0) revert VaultErrors.PositionAlreadyOpened();
        _validateWidth(lower, upper);
        _validateTickSpacing(lower, upper);

        (address token0, address token1, uint24 fee) = _poolTokens();

        // Use all idle balances as desired amounts for MVP simplicity.
        uint amount0Desired = IERC20(token0).balanceOf(address(this));
        uint amount1Desired = IERC20(token1).balanceOf(address(this));
        // Accept any execution (no slippage min) in MVP; can be refined later.
        uint amount0Min = 0;
        uint amount1Min = 0;

        // Approve NFPM to pull tokens from this vault.
        _approveIfNeeded(token0, nfpm, amount0Desired);
        _approveIfNeeded(token1, nfpm, amount1Desired);

        NFPM.MintParams memory mp = NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: fee,
            tickLower: lower,
            tickUpper: upper,
            amount0Desired: amount0Desired,
            amount1Desired: amount1Desired,
            amount0Min: amount0Min,
            amount1Min: amount1Min,
            recipient: address(this),
            deadline: block.timestamp + 900 // 15 min
         });

        (uint tokenId,,,) = NFPM(nfpm).mint(mp);
        positionTokenId = tokenId;

        emit Opened(tokenId, lower, upper);
    }

    /// @inheritdoc ISingleUserVault
    function rebalance(int24 lower, int24 upper) external onlyOwner poolSet nonReentrant {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();
        _ensureCooldown();
        _validateWidth(lower, upper);
        _validateTickSpacing(lower, upper);
        _ensureTwapOk();

        // Collect any fees owed first (static value is used off-chain; on-chain here is the actual claim).
        NFPM.CollectParams memory cparams = NFPM.CollectParams({
            tokenId: positionTokenId,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        });
        (uint fees0, uint fees1) = NFPM(nfpm).collect(cparams);

        // Close prior liquidity completely.
        (,,,,, int24 tickLower, int24 tickUpper, uint128 liquidity,,,,) =
            NFPM(nfpm).positions(positionTokenId);
        if (liquidity > 0) {
            NFPM.DecreaseLiquidityParams memory dl = NFPM.DecreaseLiquidityParams({
                tokenId: positionTokenId,
                liquidity: liquidity,
                amount0Min: 0,
                amount1Min: 0,
                deadline: block.timestamp + 900
            });
            NFPM(nfpm).decreaseLiquidity(dl);
        }

        // Burn the tokenId if liquidity is zero (keeps state clean).
        NFPM(nfpm).burn(positionTokenId);

        // Mint new range using all idle balances.
        (address token0, address token1, uint24 fee) = _poolTokens();
        uint amount0Desired = IERC20(token0).balanceOf(address(this));
        uint amount1Desired = IERC20(token1).balanceOf(address(this));

        _approveIfNeeded(token0, nfpm, amount0Desired);
        _approveIfNeeded(token1, nfpm, amount1Desired);

        NFPM.MintParams memory mp = NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: fee,
            tickLower: lower,
            tickUpper: upper,
            amount0Desired: amount0Desired,
            amount1Desired: amount1Desired,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp + 900
        });

        (uint newTokenId,,,) = NFPM(nfpm).mint(mp);
        positionTokenId = newTokenId;
        lastRebalance = block.timestamp;

        emit Rebalanced(newTokenId, lower, upper, fees0, fees1);
    }

    /// @inheritdoc ISingleUserVault
    function withdrawAll() external onlyOwner nonReentrant {
        // Transfer all balances of token0 and token1 to owner.
        // This function does not modify the active LP position; if liquidity is present,
        // user should rebalance/decrease to zero and collect before calling withdrawAll.
        if (pool == address(0)) revert VaultErrors.PoolNotSet();
        (address token0, address token1,) = _poolTokens();

        uint sent0 = 0;
        uint sent1 = 0;

        uint bal0 = IERC20(token0).balanceOf(address(this));
        if (bal0 > 0) {
            IERC20(token0).safeTransfer(owner, bal0);
            sent0 = bal0;
        }

        uint bal1 = IERC20(token1).balanceOf(address(this));
        if (bal1 > 0) {
            IERC20(token1).safeTransfer(owner, bal1);
            sent1 = bal1;
        }

        emit Withdrawn(sent0, sent1);
    }

    /// @inheritdoc ISingleUserVault
    function currentRange() external view returns (int24 lower, int24 upper, uint128 liquidity) {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();
        (,,,,, int24 tickLower, int24 tickUpper, uint128 liq,,,,) =
            NFPM(nfpm).positions(positionTokenId);
        return (tickLower, tickUpper, liq);
    }

    /// @inheritdoc ISingleUserVault
    function twapOk() external view returns (bool) {
        return _twapOkInternal();
    }

    // -------------------------
    // Internal helpers
    // -------------------------

    function _poolTokens() internal view returns (address token0, address token1, uint24 fee) {
        IUniV3PoolMinimal p = IUniV3PoolMinimal(pool);
        token0 = p.token0();
        token1 = p.token1();
        fee = p.fee();
    }

    function _approveIfNeeded(address token, address spender, uint amount) internal {
        if (amount == 0) return;
        uint allowance = IERC20(token).allowance(address(this), spender);
        if (allowance < amount) {
            // Reset to 0 first for compatibility with some ERC20s, then set max.
            IERC20(token).forceApprove(spender, 0);
            IERC20(token).forceApprove(spender, type(uint).max);
        }
    }

    function _validateWidth(int24 lower, int24 upper) internal view {
        if (upper <= lower) revert VaultErrors.InvalidWidth();
        int24 width = upper - lower;
        if (width < minWidth || width > maxWidth) revert VaultErrors.InvalidWidth();
    }

    function _ensureCooldown() internal view {
        if (block.timestamp < lastRebalance + minCooldown) revert VaultErrors.CooldownNotPassed();
    }

    function _ensureTwapOk() internal view {
        if (!_twapOkInternal()) revert VaultErrors.TwapDeviationTooHigh();
    }

    function _twapOkInternal() internal view returns (bool) {
        IUniV3PoolMinimal p = IUniV3PoolMinimal(pool);
        (, int24 spotTick,,,,,) = p.slot0();
        int24 twapTick = UniV3TwapOracle.consultTick(p, twapWindow);

        int24 diff = spotTick - twapTick;
        // abs em int24
        int24 absDiff = diff >= 0 ? diff : -diff;

        return absDiff <= maxTwapDeviationTicks;
    }

    function _tickSpacing() internal view returns (int24) {
        return IUniV3PoolMinimal(pool).tickSpacing();
    }

    function _validateTickSpacing(int24 lower, int24 upper) internal view {
        int24 spacing = _tickSpacing();
        // bounds devem ser múltiplos do spacing
        if ((lower % spacing) != 0 || (upper % spacing) != 0) {
            revert VaultErrors.InvalidTickSpacing();
        }
        // largura também deve respeitar múltiplos, mas já está implícito por ser ambos múltiplos
    }

    /// @notice Returns pool tokens and fee tier (for UI/bot).
    function tokens() external view returns (address token0, address token1, uint24 fee) {
        return _poolTokens();
    }

    /// @notice Returns NFPM.tokensOwed values (not including unaccounted fee growth).
    function pendingTokensOwed() external view returns (uint128 owed0, uint128 owed1) {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();
        (,,,,,,,,,, uint128 t0, uint128 t1) = NFPM(nfpm).positions(positionTokenId);
        return (t0, t1);
    }
}
