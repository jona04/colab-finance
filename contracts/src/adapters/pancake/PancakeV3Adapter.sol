// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC721Receiver} from "openzeppelin-contracts/contracts/token/ERC721/IERC721Receiver.sol";
import {IERC721} from "openzeppelin-contracts/contracts/token/ERC721/IERC721.sol";

import "../../interfaces/IConcentratedLiquidityAdapter.sol";
import "./interfaces/IPancakeV3PoolMinimal.sol";
import "./interfaces/IPancakeV3NFPM.sol";
import "./interfaces/IMasterChefV3.sol";

/**
 * @title PancakeV3Adapter
 * @notice Adapter para PancakeSwap v3 (CLAMM) com staking no MasterChefV3.
 * @dev Mantém a mesma semântica dos seus adapters:
 *      - NFT mantido NO ADAPTER
 *      - openInitialPosition / rebalanceWithCaps puxam tokens do Vault (transferFrom)
 *      - stake/unstake/claimRewards via MasterChefV3 (se configurado)
 *      - collectToVault e exitPositionToVault sempre empurram fundos de volta pro Vault
 */
contract PancakeV3Adapter is IConcentratedLiquidityAdapter, IERC721Receiver {
    using SafeERC20 for IERC20;

    // endereços imutáveis do protocolo
    address public immutable override pool;
    address public immutable override nfpm;
    address public immutable override gauge; // aqui: MasterChefV3

    // por vault
    mapping(address => uint256) private _tokenId;
    mapping(address => uint256) public lastRebalance;

    // guardas
    uint256 public minCooldown = 1 minutes;
    int24   public minWidth    = 10;
    int24   public maxWidth    = 900_000;
    int24   public maxTwapDeviationTicks = 50;
    uint32  public twapWindow  = 60; // (placeholder; Pancake v3 não precisa TWAP aqui)

    constructor(address _pool, address _nfpm, address _masterChefV3) {
        require(_pool != address(0) && _nfpm != address(0), "zero");
        pool  = _pool;
        nfpm  = _nfpm;
        gauge = _masterChefV3; // pode ser zero se não quiser staking
    }

    // ===== ERC721 Receiver =====
    function onERC721Received(
        address /*operator*/, address /*from*/, uint256 /*id*/, bytes calldata /*data*/
    ) external pure override returns (bytes4) {
        return IERC721Receiver.onERC721Received.selector;
    }

    // ===== Views básicas =====
    function tokens() public view override returns (address token0, address token1) {
        token0 = IPancakeV3PoolMinimal(pool).token0();
        token1 = IPancakeV3PoolMinimal(pool).token1();
    }

    function tickSpacing() external view override returns (int24) {
        return IPancakeV3PoolMinimal(pool).tickSpacing();
    }

    function slot0() external view override returns (uint160 sqrtPriceX96, int24 tick) {
        (sqrtPriceX96, tick, , , , , ) = IPancakeV3PoolMinimal(pool).slot0();
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
        ( , , , , , int24 l, int24 u, uint128 L, , , , ) = IPancakeV3NFPM(nfpm).positions(tid);
        return (l, u, L);
    }

    // ===== helpers internos =====
    function _approveIfNeeded(address token, address spender, uint256 amount) internal {
        if (amount == 0) return;
        uint256 allowance = IERC20(token).allowance(address(this), spender);
        if (allowance < amount) {
            IERC20(token).forceApprove(spender, 0);
            IERC20(token).forceApprove(spender, type(uint256).max);
        }
    }

    function _ensureCooldown(address vault_) internal view {
        require(block.timestamp >= lastRebalance[vault_] + minCooldown, "cooldown");
    }

    function _isStaked(uint256 tokenId) internal view returns (bool) {
        if (tokenId == 0) return false;
        try IERC721(nfpm).ownerOf(tokenId) returns (address owner) {
            return owner != address(this);
        } catch {
            return false;
        }
    }

    // ================================================================
    // =============== MAIN LP LOGIC ==================================
    // ================================================================

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

        // puxa do Vault
        if (a0 > 0) IERC20(token0).safeTransferFrom(vault, address(this), a0);
        if (a1 > 0) IERC20(token1).safeTransferFrom(vault, address(this), a1);

        _approveIfNeeded(token0, nfpm, a0);
        _approveIfNeeded(token1, nfpm, a1);

        // fee é lido do pool
        uint24 fee = IPancakeV3PoolMinimal(pool).fee();

        IPancakeV3NFPM.MintParams memory p = IPancakeV3NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: fee,
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: a0,
            amount1Desired: a1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp + 900
        });

        (tokenId, liquidity, , ) = IPancakeV3NFPM(nfpm).mint(p);
        _tokenId[vault] = tokenId;

        // devolve sobras ao vault
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

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
        require(!_isStaked(tokenId), "position staked");
        _ensureCooldown(vault);

        // 1) collect fees p/ adapter
        IPancakeV3NFPM(nfpm).collect(
            IPancakeV3NFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        // 2) decrease all + collect owed
        (, , , , , , , uint128 liq, , , , ) = IPancakeV3NFPM(nfpm).positions(tokenId);
        if (liq > 0) {
            IPancakeV3NFPM(nfpm).decreaseLiquidity(
                IPancakeV3NFPM.DecreaseLiquidityParams({
                    tokenId: tokenId,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 900
                })
            );
            IPancakeV3NFPM(nfpm).collect(
                IPancakeV3NFPM.CollectParams({
                    tokenId: tokenId,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
        }

        // 3) burn velho NFT
        IPancakeV3NFPM(nfpm).burn(tokenId);
        _tokenId[vault] = 0;

        // 4) compõe amounts finais (caps + pull do vault)
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

        // 5) mint nova posição
        _approveIfNeeded(token0, nfpm, use0);
        _approveIfNeeded(token1, nfpm, use1);

        uint24 fee = IPancakeV3PoolMinimal(pool).fee();
        IPancakeV3NFPM.MintParams memory p = IPancakeV3NFPM.MintParams({
            token0: token0,
            token1: token1,
            fee: fee,
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: use0,
            amount1Desired: use1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: address(this),
            deadline: block.timestamp + 900
        });

        (uint256 newTid, uint128 L, , ) = IPancakeV3NFPM(nfpm).mint(p);
        _tokenId[vault] = newTid;
        newLiquidity = L;

        // 6) devolve sobras
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        lastRebalance[vault] = block.timestamp;
    }

    // ===== Exit & Collect =====
    function exitPositionToVault(address vault) external override {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;
        require(!_isStaked(tokenId), "position staked");

        // collect + remove + collect + burn
        IPancakeV3NFPM(nfpm).collect(
            IPancakeV3NFPM.CollectParams({
                tokenId: tokenId,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );

        (, , , , , , , uint128 liq, , , , ) = IPancakeV3NFPM(nfpm).positions(tokenId);
        if (liq > 0) {
            IPancakeV3NFPM(nfpm).decreaseLiquidity(
                IPancakeV3NFPM.DecreaseLiquidityParams({
                    tokenId: tokenId,
                    liquidity: liq,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp + 900
                })
            );
            IPancakeV3NFPM(nfpm).collect(
                IPancakeV3NFPM.CollectParams({
                    tokenId: tokenId,
                    recipient: address(this),
                    amount0Max: type(uint128).max,
                    amount1Max: type(uint128).max
                })
            );
        }

        // tenta claim antes de burn (se houver gauge)
        if (gauge != address(0)) {
            try this.claimRewards(vault) {} catch {}
        }

        IPancakeV3NFPM(nfpm).burn(tokenId);
        _tokenId[vault] = 0;

        (address token0, address token1) = tokens();
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b0 > 0) IERC20(token0).safeTransfer(vault, b0);
        if (b1 > 0) IERC20(token1).safeTransfer(vault, b1);
    }

    function collectToVault(address vault)
        external
        override
        returns (uint256 amount0, uint256 amount1)
    {
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return (0, 0);

        (amount0, amount1) = IPancakeV3NFPM(nfpm).collect(
            IPancakeV3NFPM.CollectParams({
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

    // ===== Staking (MasterChefV3) =====
    function stakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");

        IERC721(nfpm).safeTransferFrom(address(this), gauge, tokenId);
    }

    function unstakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        require(tokenId != 0, "no pos");

        // opcional: tentar colher antes
        try this.claimRewards(vault) {} catch {}
        IMasterChefV3(gauge).withdraw(tokenId, address(this));
    }

    function claimRewards(address vault) public override {
        if (gauge == address(0)) return;
        uint256 tokenId = _tokenId[vault];
        if (tokenId == 0) return;

        // manda direto para o vault (evita ter que descobrir o token e reenviar)
        try IMasterChefV3(gauge).harvest(tokenId, vault) {
            // ok
        } catch {
            // fallback (se alguma rede pedir para receber no adapter)
            uint256 beforeBal;
            address cake;
            try IMasterChefV3(gauge).CAKE() returns (address rt) {
                cake = rt; beforeBal = cake == address(0) ? 0 : IERC20(cake).balanceOf(address(this));
            } catch {}
            // tenta novamente para o adapter
            try IMasterChefV3(gauge).harvest(tokenId, address(this)) {
                if (cake != address(0)) {
                    uint256 gained = IERC20(cake).balanceOf(address(this)) - beforeBal;
                    if (gained > 0) IERC20(cake).safeTransfer(vault, gained);
                }
            } catch {}
        }
    }

}
