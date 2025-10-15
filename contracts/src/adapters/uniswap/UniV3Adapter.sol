// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";

import {IConcentratedLiquidityAdapter} from "../../interfaces/IConcentratedLiquidityAdapter.sol";
import {INonfungiblePositionManagerMinimal as NFPM} from "../../interfaces/INonfungiblePositionManagerMinimal.sol";
import {IUniV3PoolMinimal} from "../../interfaces/IUniV3PoolMinimal.sol";
import {IUniswapV3FactoryMinimal} from "../../interfaces/IUniswapV3FactoryMinimal.sol";

import {UniV3TwapOracle} from "./UniV3TwapOracle.sol";

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

    // vault => tokenId (mantemos aqui; o Vault V2 também salva localmente pra exibir)
    mapping(address => uint256) private _tokenId;

    constructor(address _nfpm, address _pool) {
        require(_nfpm != address(0) && _pool != address(0), "zero");
        // sanity: factory do pool deve ser a mesma do NFPM
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

        uint256 tid = _tokenId[vault];

        // 1) collect fees antes de mexer na liquidity (para o adapter)
        NFPM(nfpm).collect(NFPM.CollectParams({
            tokenId: tid,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        }));

        // 2) decrease total (se houver)
        (, , , , , , , uint128 liq, , , , ) = NFPM(nfpm).positions(tid);
        if (liq > 0) {
            NFPM(nfpm).decreaseLiquidity(NFPM.DecreaseLiquidityParams({
                tokenId: tid,
                liquidity: liq,
                amount0Min: 0,
                amount1Min: 0,
                deadline: block.timestamp + 900
            }));
            // coleta de tokens owed do decrease
            NFPM(nfpm).collect(NFPM.CollectParams({
                tokenId: tid,
                recipient: address(this),
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            }));
        }

        // 3) burn posição antiga
        NFPM(nfpm).burn(tid);
        _tokenId[vault] = 0;

        // 4) calcular “desired” com caps a partir dos tokens que estão NO ADAPTER (coletados)
        (address token0, address token1) = tokens();
        uint256 bal0 = IERC20(token0).balanceOf(address(this));
        uint256 bal1 = IERC20(token1).balanceOf(address(this));
        uint256 use0 = (cap0 == 0 || cap0 > bal0) ? bal0 : cap0;
        uint256 use1 = (cap1 == 0 || cap1 > bal1) ? bal1 : cap1;

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

        // devolve sobras ao vault
        uint256 r0 = IERC20(token0).balanceOf(address(this));
        uint256 r1 = IERC20(token1).balanceOf(address(this));
        if (r0 > 0) IERC20(token0).safeTransfer(vault, r0);
        if (r1 > 0) IERC20(token1).safeTransfer(vault, r1);

        lastRebalance[vault] = block.timestamp;
    }

    function _exitPositionToVault(address vault) internal {
        uint256 tid = _tokenId[vault];
        require(tid != 0, "no position");

        // collect fees (para adapter), depois decrease e collect owed
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

        // burn e envia tudo ao vault
        NFPM(nfpm).burn(tid);
        _tokenId[vault] = 0;

        (address token0, address token1) = tokens();
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        if (b0 > 0) IERC20(token0).safeTransfer(vault, b0);
        if (b1 > 0) IERC20(token1).safeTransfer(vault, b1);
    }

    function exitPositionToVault(address vault) external override {
        _exitPositionToVault(vault);
    }

    function exitPositionAndWithdrawAll(address vault, address to) external override {
        _exitPositionToVault(vault);
        (address token0, address token1) = tokens();

        // agora os tokens estão no vault; o contrato vault deve transferir para `to` em outro passo,
        // porém para manter a assinatura do interface, não movimentamos tokens do vault aqui.
        // Se você prefere, mude a semântica: o adapter não toca “to”, só fecha posição.
        // Mantemos “no-op” extra aqui.
        to; // silenciar warning
    }

    function collectToVault(address vault) external override returns (uint256 amount0, uint256 amount1) {
        uint256 tid = _tokenId[vault];
        require(tid != 0, "no position");

        // coleta para o adapter
        (amount0, amount1) = NFPM(nfpm).collect(NFPM.CollectParams({
            tokenId: tid,
            recipient: address(this),
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max
        }));

        // envia ao vault
        (address token0, address token1) = tokens();
        if (amount0 > 0) IERC20(token0).safeTransfer(vault, amount0);
        if (amount1 > 0) IERC20(token1).safeTransfer(vault, amount1);
    }

    // ===== staking (não aplicável no Uniswap v3) =====
    function stakePosition(address) external override {}
    function unstakePosition(address) external override {}
    function claimRewards(address) external override {}
}
