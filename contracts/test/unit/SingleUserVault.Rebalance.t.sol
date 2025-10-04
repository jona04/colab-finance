// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {stdStorage, StdStorage} from "forge-std/StdStorage.sol";

import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {MockNFPM} from "../mocks/MockNFPM.sol";
import {MockPool} from "../mocks/MockPool.sol";
import {VaultErrors} from "../../src/errors/VaultErrors.sol";

/// @title Rebalance unit tests
/// @notice Valida os guards que disparam antes das interações com o NFPM.
contract SingleUserVault_Rebalance_Test is Test {
    using stdStorage for StdStorage;

    StdStorage private _stdstore;

    SingleUserVault vault;
    MockNFPM nfpm;
    MockPool pool;

    address owner = address(0xBEEF);

    function setUp() public {
        vm.startPrank(owner);
        nfpm = new MockNFPM(address(0xFAcA0A));
        vault = new SingleUserVault(address(nfpm));
        vm.stopPrank();

        // Pool com tickSpacing=60 e tick spot = 0
        pool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0);

        vm.prank(owner);
        vault.setPoolOnce(address(pool));
    }

    /// @dev Marca a posição como "aberta" setando positionTokenId = 1 via stdstore (slot-safe).
    function _seedPositionOpened() internal {
        _stdstore
            .target(address(vault))
            .sig(vault.positionTokenId.selector)
            .checked_write(uint256(1));
    }

    /// @dev Define lastRebalance via stdstore (slot-safe).
    function _setLastRebalance(uint256 ts) internal {
        _stdstore
            .target(address(vault))
            .sig(vault.lastRebalance.selector)
            .checked_write(ts);
    }

    function test_Revert_WhenPositionNotInitialized() public {
        // positionTokenId == 0
        vm.prank(owner);
        vm.expectRevert(VaultErrors.PositionNotInitialized.selector);
        vault.rebalance(-120, -60);
    }

    function test_Revert_CooldownNotPassed() public {
        _seedPositionOpened();

        // lastRebalance = now; minCooldown default = 30 min
        _setLastRebalance(block.timestamp);

        vm.prank(owner);
        vm.expectRevert(VaultErrors.CooldownNotPassed.selector);
        vault.rebalance(-120, -60);
    }

    function test_Revert_InvalidWidth() public {
        _seedPositionOpened();

        // passar cooldown
        vm.warp(block.timestamp + 31 minutes);

        vm.prank(owner);
        vm.expectRevert(VaultErrors.InvalidWidth.selector);
        vault.rebalance(0, 30); // width < minWidth
    }

    function test_Revert_InvalidTickSpacing() public {
        _seedPositionOpened();
        vm.warp(block.timestamp + 31 minutes);

        // tickSpacing=60 → bounds não múltiplos
        vm.prank(owner);
        vm.expectRevert(VaultErrors.InvalidTickSpacing.selector);
        vault.rebalance(-121, -61);
    }

    function test_Revert_TwapDeviationTooHigh() public {
        _seedPositionOpened();
        vm.warp(block.timestamp + 31 minutes);

        // Força grande desvio de spot vs TWAP recriando pool com tick spot alto.
        MockPool newPool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 1000);

        // Sobrescreve o endereço da pool na storage do vault (somente para teste)
        // pool() getter → usar stdstore para achar o slot.
        _stdstore
            .target(address(vault))
            .sig(vault.pool.selector)
            .checked_write(uint256(uint160(address(newPool))));

        // Bounds válidos (múltiplos de 60) para cair especificamente no TWAP guard
        vm.prank(owner);
        vm.expectRevert(VaultErrors.TwapDeviationTooHigh.selector);
        vault.rebalance(0, 60);
    }
}
