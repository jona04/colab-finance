// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";

import {IConcentratedLiquidityAdapter} from "../../interfaces/IConcentratedLiquidityAdapter.sol";
import {INonfungiblePositionManagerMinimal as NFPM} from "../../interfaces/INonfungiblePositionManagerMinimal.sol";
import {IUniV3PoolMinimal} from "../../interfaces/IUniV3PoolMinimal.sol";
import {IUniswapV3FactoryMinimal} from "../../interfaces/IUniswapV3FactoryMinimal.sol";
import {UniV3TwapOracle} from "./UniV3TwapOracle.sol";

/**
 * @title UniV3Adapter
 * @notice Uniswap v3 adapter used by the generic vault (V2).
 * @dev This version supports:
 *      - Rebalance that can pull deficits from the vault (transferFrom) up to the caps.
 *      - Validation: if target range contains the current tick, both tokens must be > 0.
 */
contract UniV3Adapter is IConcentratedLiquidityAdapter {
    using SafeERC20 for IERC20;

    address public immutable override pool;
    address public immutable override nfpm;
    address public immutable override gauge; // always zero for Uniswap

    // --- guard params (copiados do V1 para manter comportamento) ---
    uint256 public minCooldown = 30 minutes;
    int24   public minWidth    = 60;
    int24   public maxWidth    = 200_000;
    int24   public maxTwapDeviationTicks = 50; // ~0.5%
    uint32  public twapWindow  = 60;

    // vault => lastRebalance
    mapping(address => uint256) public lastRebalance;

    // vault => tokenId (NFT is held by the adapter)
    mapping(address => uint256) private _tokenId;

    error InRangeRequiresBothTokens(); // clear revert reason for UX

    constructor(address _nfpm, address _pool) {
        require(_nfpm != address(0) && _pool != address(0), "zero");
        address f = NFPM(_nfpm).factory();
        require(IUniV3PoolMinimal(_pool).factory() == f, "invalid factory");
        nfpm = _nfpm;
        pool = _pool;
        gauge = address(0);
    }

    // ===== views =====

    function tickSpacing() external view override returns (int24) {
        return IUniV3PoolMinimal(pool).tickSpacing();
    }

    function slot0() external view override returns (uint160 sqrtPriceX96, int24 tick) {
        (sqrtPriceX96, tick, , , , , ) = IUniV3PoolMinimal(pool).slot0();
    }

    function tokens() public view override returns (address token0, address token1) {
        token0 = IUniV3PoolMinimal(pool).token0();
        token1 = IUniV3PoolMinimal(pool).token1();
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
        ( , , , , , int24 l, int24 u, uint128 L, , , , ) = NFPM(nfpm).positions(tid);
        return (l, u, L);
    }

    function twapOk() external view returns (bool) {
        return _twapOk();
    }
    // ===== helpers internos =====

    function _validateWidth(int24 lower, int24 upper) internal view {
        require(upper > lower, "width");
        int24 width = upper - lower;
        require(width >= minWidth && width <= maxWidth, "width bounds");
    }

    function _validateTickSpacing(int24 lower, int24 upper) internal view {
        int24 spacing = IUniV3PoolMinimal(pool).tickSpacing();
        require((lower % spacing) == 0 && (upper % spacing) == 0, "spacing");
    }

    function _ensureCooldown(address vault) internal view {
        require(block.timestamp >= lastRebalance[vault] + minCooldown, "cooldown");
    }

    function _twapOk() internal view returns (bool) {
        (, int24 spotTick, , , , , ) = IUniV3PoolMinimal(pool).slot0();
        int24 twapTick = UniV3TwapOracle.consultTick(IUniV3PoolMinimal(pool), twapWindow);
        int24 diff = spotTick - twapTick;
        if (diff < 0) diff = -diff;
        return diff <= maxTwapDeviationTicks;
    }

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
        int24 lower,
        int24 upper
    ) external override returns (uint256 tokenId, uint128 liquidity) {
        require(_tokenId[vault] == 0, "already opened");
        _validateWidth(lower, upper);
        _validateTickSpacing(lower, upper);

        (address token0, address token1) = tokens();

        uint256 a0 = IERC20(token0).balanceOf(vault);
        uint256 a1 = IERC20(token1).balanceOf(vault);
        require(a0 > 0 || a1 > 0, "no funds");

        // pull tokens do vault para este adapter (apenas durante o mint) OU
        // alternativa: o vault aprova o NFPM diretamente.
        // Para simplicidade, pedimos que o **Vault transfira** para o adapter antes? Não.
        // Aqui assumimos que o Vault já aprovou o adapter para gastar seus tokens:
        // então puxamos para o adapter e aprovamos o NFPM.
        if (a0 > 0) IERC20(token0).safeTransferFrom(vault, address(this), a0);
        if (a1 > 0) IERC20(token1).safeTransferFrom(vault, address(this), a1);

        _approveIfNeeded(token0, nfpm, a0);
        _approveIfNeeded(token1, nfpm, a1);

        NFPM.MintParams memory mp = NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: IUniV3PoolMinimal(pool).fee(),
            tickLower: lower,
            tickUpper: upper,
            amount0Desired: a0,
            amount1Desired: a1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this), // NFT fica com o adapter (padrão simples)
            deadline: block.timestamp + 900
        });

        (tokenId, liquidity, , ) = NFPM(nfpm).mint(mp);
        _tokenId[vault] = tokenId;

        // devolve os saldos "sobrando" ao vault
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        lastRebalance[vault] = block.timestamp;
    }

/**
     * @notice Rebalance into a new [lower, upper] using fees + returned liquidity from old position
     *         and (if needed) pulling the deficit from the vault (transferFrom), up to caps.
     * @param vault The vault address this adapter manages.
     * @param lower New lower tick.
     * @param upper New upper tick.
     * @param cap0  Max amount of token0 to use (0 = unlimited up to availability).
     * @param cap1  Max amount of token1 to use (0 = unlimited up to availability).
     * @return newLiquidity The liquidity of the newly minted position.
     *
     * Invariants & validations:
     * - If current spot tick is inside [lower, upper), both token0 and token1 must be positive
     *   (after pulling from vault if adapter balance alone is not enough), otherwise revert with
     *   InRangeRequiresBothTokens().
     */
    function rebalanceWithCaps(
        address vault,
        int24 lower,
        int24 upper,
        uint256 cap0,
        uint256 cap1
    ) external override returns (uint128 newLiquidity) {
        require(_tokenId[vault] != 0, "no position");
        _ensureCooldown(vault);
        require(_twapOk(), "twap");
        _validateWidth(lower, upper);
        _validateTickSpacing(lower, upper);

        // === 0) read spot (for "inside" validation later)
        (, int24 spotTick, , , , , ) = IUniV3PoolMinimal(pool).slot0();
        bool targetContainsSpot = (spotTick >= lower && spotTick < upper);

        uint256 tid = _tokenId[vault];

        // === 1) Collect fees to adapter
        NFPM(nfpm).collect(
            NFPM.CollectParams({
                tokenId: tid,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // === 2) Remove all old liquidity (if any) and collect owed
        (, , , , , , , uint128 liq, , , , ) = NFPM(nfpm).positions(tid);
        if (liq > 0) {
            NFPM(nfpm).decreaseLiquidity(
                NFPM.DecreaseLiquidityParams({
                    tokenId: tid,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 900
                })
            );
            NFPM(nfpm).collect(
                NFPM.CollectParams({
                    tokenId: tid,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
        }

        // === 3) Burn old NFT
        NFPM(nfpm).burn(tid);
        _tokenId[vault] = 0;

        // === 4) Compute desired amounts using adapter+vault, respecting caps
        (address token0, address token1) = tokens();

        // Current balances in adapter after collect/decrease/burn
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));

        // Caps: 0 = unlimited (bounded by availability)
        uint256 want0 = (cap0 == 0) ? type(uint256).max : cap0;
        uint256 want1 = (cap1 == 0) ? type(uint256).max : cap1;

        // Start with what we already have in the adapter
        uint256 use0 = bal0;
        uint256 use1 = bal1;

        // Pull deficits from vault (vault pre-approved this adapter in the caller)
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

        // === 5) Validation for "inside" mints: both sides must be positive
        if (targetContainsSpot) {
            // If still one side is zero after pulling from the vault, revert with a clear message.
            if (use0 == 0 || use1 == 0) {
                revert InRangeRequiresBothTokens();
            }
        }

        // === 6) Mint new position using the final amounts
        _approveIfNeeded(token0, nfpm, use0);
        _approveIfNeeded(token1, nfpm, use1);

        NFPM.MintParams memory mp = NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: IUniV3PoolMinimal(pool).fee(),
            tickLower: lower,
            tickUpper: upper,
            amount0Desired: use0,
            amount1Desired: use1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp + 900
        });

        (uint256 newTid, uint128 L, , ) = NFPM(nfpm).mint(mp);
        _tokenId[vault] = newTid;
        newLiquidity = L;

        // === 7) Return leftovers to vault
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        lastRebalance[vault] = block.timestamp;
    }

    // ----- exit helpers -----

    function _exitPositionToVault(address vault) internal {
        uint256 tid = _tokenId[vault];
        if (tid == 0) return;

        // collect -> decrease -> collect -> burn
        NFPM(nfpm).collect(NFPM.CollectParams({
            tokenId: tid,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        }));

        (, , , , , , , uint128 liq, , , , ) = NFPM(nfpm).positions(tid);
        if (liq > 0) {
            NFPM(nfpm).decreaseLiquidity(NFPM.DecreaseLiquidityParams({
                tokenId: tid,
                liquidity: liq,
                amount0Min: 0,
                amount1Min: 0,
                deadline: block.timestamp + 900
            }));
            NFPM(nfpm).collect(NFPM.CollectParams({
                tokenId: tid,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            }));
        }

        // burn and send all to vault
        NFPM(nfpm).burn(tid);
        _tokenId[vault] = 0;

        (address token0, address token1) = tokens();
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b0 > 0) IERC20(token0).safeTransfer(vault, b0);
        if (b1 > 0) IERC20(token1).safeTransfer(vault, b1);
    }

    /**
     * @notice For Uniswap we leave the actual "send-to-user" to the Vault.
     *         We only close the position and push all funds to the Vault.
     */
    function exitPositionToVault(address vault) external override {
        _exitPositionToVault(vault);
    }

    function collectToVault(address vault) external override returns (uint256 amount0, uint256 amount1) {
        uint256 tid = _tokenId[vault];
        require(tid != 0, "no position");

        (amount0, amount1) = NFPM(nfpm).collect(
            NFPM.CollectParams({
                tokenId: tid,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        (address token0, address token1) = tokens();
        if (amount0 > 0) IERC20(token0).safeTransfer(vault, amount0);
        if (amount1 > 0) IERC20(token1).safeTransfer(vault, amount1);
    }


    // ===== staking (not applicable to Uniswap v3) =====
    function stakePosition(address) external override {}
    function unstakePosition(address) external override {}
    function claimRewards(address) external override {}
}
