// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {stdStorage, StdStorage} from "forge-std/StdStorage.sol";

import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {MockNFPM} from "../mocks/MockNFPM.sol";
import {MockPool} from "../mocks/MockPool.sol";
import {VaultErrors} from "../../src/errors/VaultErrors.sol";

/// @title OpenInitialPosition unit tests
/// @notice Covers guard paths that do not require NFPM integration success.
contract SingleUserVault_OpenPosition_Test is Test {
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

        // Pool with tickSpacing = 60 and spot tick = 0
        pool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0);

        vm.prank(owner);
        vault.setPoolOnce(address(pool));
    }

    function test_Revert_WhenPoolNotSet() public {
        // Deploy novo vault sem setar pool
        vm.startPrank(owner);
        SingleUserVault v2 = new SingleUserVault(address(nfpm));
        vm.expectRevert(VaultErrors.PoolNotSet.selector);
        v2.openInitialPosition(-120, -60);
        vm.stopPrank();
    }

    function test_Revert_WhenPositionAlreadyOpened() public {
        // Define positionTokenId = 1 usando stdstore (slot-safe)
        _stdstore
            .target(address(vault))
            .sig(vault.positionTokenId.selector)
            .checked_write(uint256(1));

        vm.prank(owner);
        vm.expectRevert(VaultErrors.PositionAlreadyOpened.selector);
        vault.openInitialPosition(-120, -60);
    }

    function test_Revert_WhenInvalidWidth() public {
        // width = 30 (< minWidth=60 por default) → InvalidWidth
        vm.prank(owner);
        vm.expectRevert(VaultErrors.InvalidWidth.selector);
        vault.openInitialPosition(0, 30);
    }
}
