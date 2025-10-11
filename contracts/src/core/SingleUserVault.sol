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

        // 1) collect fees "pendentes" (antes do decrease)
        NFPM.CollectParams memory c1 = NFPM.CollectParams({
            tokenId: positionTokenId,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        });
        (uint256 fees0, uint256 fees1) = NFPM(nfpm).collect(c1);

        // 2) decreaseLiquidity total
        (, , , , , int24 prevLower, int24 prevUpper, uint128 liq, , , , ) = NFPM(nfpm).positions(positionTokenId);
        if (liq > 0) {
            NFPM.DecreaseLiquidityParams memory dl = NFPM.DecreaseLiquidityParams({
                tokenId: positionTokenId,
                liquidity: liq,
                amount0Min: 0,
                amount1Min: 0,
                deadline: block.timestamp + 900
            });
            NFPM(nfpm).decreaseLiquidity(dl);

            // 3) collect novamente para limpar os tokensOwed gerados pelo decrease
            NFPM.CollectParams memory c2 = NFPM.CollectParams({
                tokenId: positionTokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            });
            (uint256 add0, uint256 add1) = NFPM(nfpm).collect(c2);
            fees0 += add0;
            fees1 += add1;
        }

        // 4) agora pode burn (token fica "cleared")
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
        _withdrawAll(); // internal helper
    }

    function _withdrawAll() internal {
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

    /// @dev Internal helper: closes the current Uniswap V3 position and leaves all funds idle in the vault.
    ///      - Collects pending fees
    ///      - Decreases all liquidity
    ///      - Collects tokens owed after decrease
    ///      - Burns the position NFT
    ///      - Sets positionTokenId = 0
    function _exitPosition() internal returns (uint256 out0, uint256 out1) {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();

        // 1) Collect pending fees before decrease
        (uint256 fees0, uint256 fees1) = NFPM(nfpm).collect(
            NFPM.CollectParams({
                tokenId: positionTokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // 2) Decrease all liquidity (if any)
        (, , , , , , , uint128 liq, , , , ) = NFPM(nfpm).positions(positionTokenId);
        if (liq > 0) {
            NFPM(nfpm).decreaseLiquidity(
                NFPM.DecreaseLiquidityParams({
                    tokenId: positionTokenId,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 900
                })
            );

            // 3) Collect tokens owed from decrease
            (uint256 add0, uint256 add1) = NFPM(nfpm).collect(
                NFPM.CollectParams({
                    tokenId: positionTokenId,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
            fees0 += add0;
            fees1 += add1;
        }

        // 4) Burn the position NFT and clear storage
        uint256 oldId = positionTokenId;
        NFPM(nfpm).burn(positionTokenId);
        positionTokenId = 0;

        emit Exited(oldId, fees0, fees1);
        return (fees0, fees1);
    }

    /// @notice Closes the managed Uniswap V3 position and keeps all funds in the vault (idle).
    /// @dev Owner-only; does not touch pool config nor cooldown/TWAP.
    ///      Useful for "pool → vault" consolidation before a final withdrawal.
    function exitPositionToVault() external onlyOwner poolSet nonReentrant {
        _exitPosition();
    }

    /// @notice Closes the position and withdraws all idle balances to the owner in a single call.
    /// @dev Owner-only; combines exitPositionToVault() + withdrawAll().
    function exitAndWithdrawAll() external onlyOwner poolSet nonReentrant {
        _exitPosition();
        // reuses existing _withdrawAll() behavior (sends both tokens to owner)
        _withdrawAll();
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

    /// @notice Collects pending fees from the current Uniswap V3 position into the vault.
    /// @dev Owner-only; does not change liquidity nor burns the NFT.
    ///      Emits `Collected(fees0, fees1)`. Reverts if no position is opened.
    function collectFees()
        external
        onlyOwner
        poolSet
        nonReentrant
        returns (uint256 fees0, uint256 fees1)
    {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();

        (fees0, fees1) = NFPM(nfpm).collect(
            NFPM.CollectParams({
                tokenId: positionTokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        emit Collected(fees0, fees1);
    }

    /// @notice Rebalance using explicit caps (max amounts) for token0 and token1 (no swaps).
    /// @dev Same flow as rebalance(): collect fees -> decrease all -> collect -> burn -> mint.
    ///      The mint step uses min(balance, cap) for each token as amountXDesired.
    ///      This allows "no-swap / ratio-capped" rebalances when inventory is imbalanced.
    function rebalanceWithCaps(int24 lower, int24 upper, uint256 maxUse0, uint256 maxUse1)
        external
        onlyOwner
        poolSet
        nonReentrant
    {
        if (positionTokenId == 0) revert VaultErrors.PositionNotInitialized();
        _ensureCooldown();
        _validateWidth(lower, upper);
        _validateTickSpacing(lower, upper);
        _ensureTwapOk();

        // 1) Collect pending fees prior to decrease
        (uint256 fees0, uint256 fees1) = NFPM(nfpm).collect(
            NFPM.CollectParams({
                tokenId: positionTokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // 2) Decrease all liquidity (if any)
        (, , , , , , , uint128 liq, , , , ) = NFPM(nfpm).positions(positionTokenId);
        if (liq > 0) {
            NFPM(nfpm).decreaseLiquidity(
                NFPM.DecreaseLiquidityParams({
                    tokenId: positionTokenId,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 900
                })
            );
            // 3) Collect tokens owed from decrease
            (uint256 add0, uint256 add1) = NFPM(nfpm).collect(
                NFPM.CollectParams({
                    tokenId: positionTokenId,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
            fees0 += add0;
            fees1 += add1;
        }

        // 4) Burn old NFT and clear
        uint256 oldId = positionTokenId;
        NFPM(nfpm).burn(positionTokenId);
        positionTokenId = 0;

        (address token0, address token1, uint24 fee) = _poolTokens();

        // Determine desired amounts: limited by caps and current balances
        uint bal0 = IERC20(token0).balanceOf(address(this));
        uint bal1 = IERC20(token1).balanceOf(address(this));
        uint amount0Desired = maxUse0 < bal0 ? maxUse0 : bal0;
        uint amount1Desired = maxUse1 < bal1 ? maxUse1 : bal1;

        // Approvals (idempotent / max approval pattern)
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

        // Reuse existing event (fees0/fees1 are the total collected along the flow)
        emit Rebalanced(newTokenId, lower, upper, fees0, fees1);
    }
}
