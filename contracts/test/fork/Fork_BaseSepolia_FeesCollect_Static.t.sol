// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {IUniV3PoolMinimal} from "../../src/interfaces/IUniV3PoolMinimal.sol";

interface IUniV3PoolSwap {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256, int256);
}

contract Fork_BaseSepolia_FeesCollect_Static is Test {
    SingleUserVault vault;
    address nfpm;
    address pool;

    bool internal enabled;

    function setUp() public {
        string memory rpc = vm.envOr("RPC_BASE_SEPOLIA", string(""));
        nfpm = vm.envOr("NFPM_ADDRESS", address(0));
        pool = vm.envOr("POOL_ADDRESS", address(0));

        enabled = (bytes(rpc).length > 0) && (nfpm != address(0)) && (pool != address(0));
        if (!enabled) return;

        vm.createSelectFork(rpc);
        vault = new SingleUserVault(nfpm);
        vault.setPoolOnce(pool);
    }

    function testFork_FeesIncrease_AfterSwapsAndRebalance() public {
        if (!enabled) return;

        IUniV3PoolMinimal p = IUniV3PoolMinimal(pool);
        address token0 = p.token0();
        address token1 = p.token1();
        int24 spacing = p.tickSpacing();

        // Seed balances
        deal(token0, address(vault), 2_000e18);
        deal(token1, address(vault), 2_000e18);

        // Range around spot
        (, int24 spotTick, , , , , ) = p.slot0();
        int24 lower = (spotTick / spacing - 3) * spacing;
        int24 upper = (spotTick / spacing + 3) * spacing;

        vault.openInitialPosition(lower, upper);

        // Execute 2 small swaps to accrue fees
        IUniV3PoolSwap(address(p)).swap(address(this), true,  int256(2e15), 0, abi.encode(token0, token1));
        IUniV3PoolSwap(address(p)).swap(address(this), false, int256(2e15), 0, abi.encode(token0, token1));

        vm.warp(block.timestamp + 31 minutes);

        // Record logs and rebalance
        vm.recordLogs();
        vault.rebalance(lower, upper); // mantém a mesma faixa apenas para coletar

        // Inspect Rebalanced event
        bytes32 topic0 = keccak256("Rebalanced(uint256,int24,int24,uint256,uint256)");
        Vm.Log[] memory logs = vm.getRecordedLogs();

        uint256 fees0;
        uint256 fees1;
        bool found;

        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(vault) && logs[i].topics[0] == topic0) {
                (int24 l, int24 u, uint256 f0, uint256 f1) =
                    abi.decode(logs[i].data, (int24, int24, uint256, uint256));
                l; u;
                fees0 = f0; fees1 = f1;
                found = true;
                break;
            }
        }

        assertTrue(found, "Rebalanced event not found");
        assertTrue(fees0 > 0 || fees1 > 0, "Expected some fees collected");
    }

    // Swap callback
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        (address token0, address token1) = abi.decode(data, (address, address));
        if (amount0Delta > 0) IERC20(token0).transfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(token1).transfer(msg.sender, uint256(amount1Delta));
    }
}
