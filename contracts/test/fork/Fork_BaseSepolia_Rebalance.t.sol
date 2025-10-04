// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {IUniV3PoolMinimal} from "../../src/interfaces/IUniV3PoolMinimal.sol";
import {INonfungiblePositionManagerMinimal as NFPM} from "../../src/interfaces/INonfungiblePositionManagerMinimal.sol";

/// @dev Minimal pool swap interface (Uniswap v3 core).
interface IUniV3PoolSwap {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256, int256);
}

/// @title Fork test: open, swap for fees, and rebalance
contract Fork_BaseSepolia_Rebalance is Test {
    SingleUserVault vault;
    address nfpm;
    address pool;

    // Env-driven guard
    bool internal enabled;

    // Swap callback selector
    bytes32 constant CALLBACK_SIG = keccak256("uniswapV3SwapCallback(int256,int256,bytes)");

    function setUp() public {
        // Enable only if env vars are set
        string memory rpc = vm.envOr("RPC_BASE_SEPOLIA", string(""));
        nfpm = vm.envOr("NFPM_ADDRESS", address(0));
        pool = vm.envOr("POOL_ADDRESS", address(0));

        enabled = (bytes(rpc).length > 0) && (nfpm != address(0)) && (pool != address(0));
        if (!enabled) return;

        vm.createSelectFork(rpc);

        // Deploy vault owned by this test contract
        vault = new SingleUserVault(nfpm);

        // Lock pool
        vault.setPoolOnce(pool);
    }

    function testFork_Rebalance_CollectsFees() public {
        if (!enabled) return;

        IUniV3PoolMinimal p = IUniV3PoolMinimal(pool);
        address token0 = p.token0();
        address token1 = p.token1();
        int24 spacing = p.tickSpacing();

        // Seed vault balances (deal cheatcode adjusts ERC20 storage directly)
        deal(token0, address(vault), 1_000e18);
        deal(token1, address(vault), 1_000e18);

        // Build a small range around current tick
        (, int24 spotTick, , , , , ) = p.slot0();
        int24 lower = (spotTick / spacing - 2) * spacing;
        int24 upper = (spotTick / spacing + 2) * spacing;

        // Open initial position
        vault.openInitialPosition(lower, upper);

        // Perform a small swap to generate fees. This contract implements the swap callback.
        // Swap 1e15 of token0 -> token1.
        IUniV3PoolSwap(address(p)).swap(
            address(this),
            true,                // zeroForOne: token0 -> token1
            int256(1e15),        // amountSpecified (exactIn)
            0,                   // no price limit
            abi.encode(token0, token1)
        );

        // Advance time to pass cooldown (default 30 min) and TWAP window
        vm.warp(block.timestamp + 31 minutes);

        // Record logs to inspect Rebalanced event
        vm.recordLogs();
        // Rebalance to another valid range (shift by one spacing)
        int24 newLower = lower + spacing;
        int24 newUpper = upper + spacing;
        vault.rebalance(newLower, newUpper);

        // Extract event fees from logs (topic0 = keccak("Rebalanced(uint256,int24,int24,uint256,uint256)"))
        bytes32 topic0 = keccak256("Rebalanced(uint256,int24,int24,uint256,uint256)");
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bool found;
        uint256 fees0;
        uint256 fees1;

        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(vault) && logs[i].topics[0] == topic0) {
                // data = abi.encode(lower, upper, fees0, fees1) sem os tópicos indexados
                // No contrato, lower/upper são int24 e fees são uint256; para simplicidade,
                // faz-se um decode parcial pegando apenas as duas últimas words (fees0, fees1).
                // Layout: int24 lower (padded) | int24 upper (padded) | fees0 | fees1
                (int24 l, int24 u, uint256 f0, uint256 f1) =
                    abi.decode(logs[i].data, (int24, int24, uint256, uint256));
                // Silencia warning de variáveis não usadas
                l; u;
                fees0 = f0;
                fees1 = f1;
                found = true;
                break;
            }
        }

        assertTrue(found, "Rebalanced event not found");
        assertTrue(fees0 > 0 || fees1 > 0, "Expected some fees collected");
    }

    /// @notice Uniswap v3 swap callback. Pays the pool with token deltas.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        // Pays positive deltas to the pool (msg.sender).
        (address token0, address token1) = abi.decode(data, (address, address));
        if (amount0Delta > 0) {
            IERC20(token0).transfer(msg.sender, uint256(amount0Delta));
        }
        if (amount1Delta > 0) {
            IERC20(token1).transfer(msg.sender, uint256(amount1Delta));
        }
    }
}
