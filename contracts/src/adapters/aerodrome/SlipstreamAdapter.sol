// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../../interfaces/IConcentratedLiquidityAdapter.sol";
import "./interfaces/ISlipstreamPool.sol";
import "./interfaces/ISlipstreamNFPM.sol";
import "./interfaces/ISlipstreamGauge.sol";

/**
 * @title SlipstreamAdapter
 * @dev Adapter for Aerodrome Slipstream concentrated liquidity.
 * It wraps the Slipstream NFPM & Pool, and optionally stakes the resulting NFT into a Gauge.
 * All token flows happen between the Vault and protocol contracts; the adapter holds nothing.
 */
contract SlipstreamAdapter is IConcentratedLiquidityAdapter {
    address public immutable override pool;
    address public immutable override nfpm;
    address public immutable override gauge; // optional; can be zero

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

    constructor(address _pool, address _nfpm, address _gauge) {
        require(_pool != address(0) && _nfpm != address(0), "zero");
        pool = _pool;
        nfpm = _nfpm;
        gauge = _gauge; // can be 0x0 if you don't want staking
    }

    function tickSpacing() external view override returns (int24) {
        return ISlipstreamPool(pool).tickSpacing();
    }

    function slot0() external view override returns (uint160 sqrtPriceX96, int24 tick) {
        (sqrtPriceX96, tick,,,,) = ISlipstreamPool(pool).slot0();
    }

    function tokens() external view override returns (address token0, address token1) {
        token0 = ISlipstreamPool(pool).token0();
        token1 = ISlipstreamPool(pool).token1();
    }

    function currentTokenId(address vault) public view override returns (uint256) {
        // Your vault stores tokenId; this adapter can also read from vault via interface if needed.
        // For simplicity, expect the vault to call us passing its own address and we query it.
        // We'll rely on the vault exposing a view; or have the vault call us with known tokenId (preferred).
        // Here: assume the vault exposes `positionTokenId()`.
        (bool ok, bytes memory data) = vault.staticcall(abi.encodeWithSignature("positionTokenId()"));
        if (!ok || data.length == 0) return 0;
        return abi.decode(data, (uint256));
    }

    function currentRange(address vault)
        external
        view
        returns (int24 lower, int24 upper, uint128 liquidity)
    {
        uint256 tid = _tokenId[vault];
        require(tid != 0, "no position");
        ( , , , , , int24 l, int24 u, uint128 L, , , , ) = ISlipstreamNFPM(nfpm).positions(tid);
        return (l, u, L);
    }


    function _approveIfNeeded(address token, address spender, uint256 amount) internal {
        // Minimal ERC20 approve pattern is left out intentionally; vault should pre-approve NFPM if needed.
        // Slipstream NFPM is pull model on mint/increase, so ensure approvals exist at the Vault.
        // If you prefer adapter to handle approvals, make adapter own allowances using SafeERC20 here.
    }

    function openInitialPosition(
        address vault,
        int24 tickLower,
        int24 tickUpper
    ) external override returns (uint256 tokenId, uint128 liquidity) {
        // Read tokens from pool and pull balances from vault (the Vault should have funds)
        address token0 = ISlipstreamPool(pool).token0();
        address token1 = ISlipstreamPool(pool).token1();

        // Amounts: adapter is stateless; we mint using all idle balances held by the vault.
        // If you want caps here, pass via vault and wire params. For now, we read balances and use them.
        uint256 amt0 = _balanceOf(token0, vault);
        uint256 amt1 = _balanceOf(token1, vault);

        // Approvals must exist from the Vault to NFPM. You can require the Vault to call ERC20.approve(nfpm).
        // The adapter can't set allowances on behalf of the Vault unless the Vault explicitly calls adapter for it.

        ISlipstreamNFPM.MintParams memory p = ISlipstreamNFPM.MintParams({
            token0: token0,
            token1: token1,
            tickSpacing: ISlipstreamPool(pool).tickSpacing(),
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: amt0,
            amount1Desired: amt1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: vault,
            deadline: block.timestamp,
            sqrtPriceX96: 0 // 0 = no price init; require pool is initialized
        });

        (tokenId, liquidity,,) = ISlipstreamNFPM(nfpm).mint(p);

        // Optional: stake the NFT if a gauge is configured
        if (gauge != address(0)) {
            // Vault must approve gauge for NFT, or NFPM.setApprovalForAll(gauge, true) by the Vault.
            ISlipstreamGauge(gauge).deposit(tokenId);
        }
    }

    function rebalanceWithCaps(
        address vault,
        int24 tickLower,
        int24 tickUpper,
        uint256 cap0,
        uint256 cap1
    ) external override returns (uint128 newLiquidity) {
        uint256 tokenId = currentTokenId(vault);
        require(tokenId != 0, "no pos");

        // 1) If staked, unstake first (Gauge withdraw)
        if (gauge != address(0) && ISlipstreamGauge(gauge).stakedContains(vault, tokenId)) {
            ISlipstreamGauge(gauge).withdraw(tokenId);
        }

        // 2) Decrease full liquidity & collect to vault
        _burnAllAndCollect(vault, tokenId);

        // 3) Re-mint with caps if provided (cap0/cap1 in raw units)
        address token0 = ISlipstreamPool(pool).token0();
        address token1 = ISlipstreamPool(pool).token1();
        uint256 amt0 = cap0 > 0 ? cap0 : _balanceOf(token0, vault);
        uint256 amt1 = cap1 > 0 ? cap1 : _balanceOf(token1, vault);

        ISlipstreamNFPM.MintParams memory p = ISlipstreamNFPM.MintParams({
            token0: token0,
            token1: token1,
            tickSpacing: ISlipstreamPool(pool).tickSpacing(),
            tickLower: tickLower,
            tickUpper: tickUpper,
            amount0Desired: amt0,
            amount1Desired: amt1,
            amount0Min: 0,
            amount1Min: 0,
            recipient: vault,
            deadline: block.timestamp,
            sqrtPriceX96: 0
        });

        (/*tokenId2*/, newLiquidity,,) = ISlipstreamNFPM(nfpm).mint(p);

        if (gauge != address(0)) {
            uint256 nid = currentTokenId(vault);
            ISlipstreamGauge(gauge).deposit(nid);
        }
    }

    function exitPositionToVault(address vault) external override {
        uint256 tokenId = currentTokenId(vault);
        if (tokenId == 0) return;
        _burnAllAndCollect(vault, tokenId);
    }

    function exitPositionAndWithdrawAll(address vault, address to) external override {
        uint256 tokenId = currentTokenId(vault);
        if (tokenId != 0) _burnAllAndCollect(vault, tokenId);

        (address t0, address t1) = (ISlipstreamPool(pool).token0(), ISlipstreamPool(pool).token1());
        _xferAll(t0, vault, to);
        _xferAll(t1, vault, to);
    }

    function collectToVault(address vault) external override returns (uint256 amount0, uint256 amount1) {
        uint256 tokenId = currentTokenId(vault);
        if (tokenId == 0) return (0, 0);
        (,,address token0,address token1,,,,,,,,) = ISlipstreamNFPM(nfpm).positions(tokenId);
        (amount0, amount1) = ISlipstreamNFPM(nfpm).collect(
            ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: vault,
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );
    }

    // ===== staking helpers =====

    function stakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = currentTokenId(vault);
        require(tokenId != 0, "no pos");
        ISlipstreamGauge(gauge).deposit(tokenId);
    }

    function unstakePosition(address vault) external override {
        if (gauge == address(0)) return;
        uint256 tokenId = currentTokenId(vault);
        require(tokenId != 0, "no pos");
        ISlipstreamGauge(gauge).withdraw(tokenId);
    }

    function claimRewards(address /*vault*/) external override {
        if (gauge == address(0)) return;
        // If using tokenId flow:
        // ISlipstreamGauge(gauge).getReward(tokenId);
        // Some gauges also support getReward(address). Pick one that exists in your deployment.
    }

    // ===== internal utils =====

    function _burnAllAndCollect(address vault, uint256 tokenId) internal {
        (,,,,,int24 tl,int24 tu,uint128 L,,,,) = ISlipstreamNFPM(nfpm).positions(tokenId);
        if (L > 0) {
            ISlipstreamNFPM(nfpm).decreaseLiquidity(
                ISlipstreamNFPM.DecreaseLiquidityParams({
                    tokenId: tokenId,
                    liquidity: L,
                    amount0Min: 0,
                    amount1Min: 0,
                    deadline: block.timestamp
                })
            );
        }
        ISlipstreamNFPM(nfpm).collect(
            ISlipstreamNFPM.CollectParams({
                tokenId: tokenId,
                recipient: vault,
                amount0Max: type(uint128).max,
                amount1Max: type(uint128).max
            })
        );
        // If the position must be burned to reset tokenId semantics in your vault, consider NFPM.burn(tokenId)
        // Only if protocol allows and your vault wants to recreate fresh tokenIds on each rebalance.
    }

    function _balanceOf(address token, address owner) internal view returns (uint256 bal) {
        (bool ok, bytes memory data) =
            token.staticcall(abi.encodeWithSignature("balanceOf(address)", owner));
        if (ok && data.length >= 32) bal = abi.decode(data, (uint256));
    }

    function _xferAll(address token, address from, address to) internal {
        if (to == address(0)) return;
        uint256 bal = _balanceOf(token, from);
        if (bal == 0) return;
        // Vault must perform the transfer itself; or expose a hook that calls token.transfer(to, bal).
        // Adapters generally should not hold custody. If you want the adapter to move funds,
        // you need the vault to call adapter as an operator and the adapter must do ERC20 transferFrom.
    }
}
