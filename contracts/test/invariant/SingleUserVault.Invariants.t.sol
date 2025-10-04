// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {MockNFPM} from "../mocks/MockNFPM.sol";
import {MockPool} from "../mocks/MockPool.sol";
import {VaultErrors} from "../../src/errors/VaultErrors.sol";

contract SingleUserVault_Invariants is Test {
    SingleUserVault vault;
    MockNFPM nfpm;
    address owner = address(0xBEEF);
    address poolA;
    address poolB;

    function setUp() public {
        vm.startPrank(owner);
        nfpm = new MockNFPM(address(0xFAcA0A));
        vault = new SingleUserVault(address(nfpm));
        vm.stopPrank();

        poolA = address(new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0));
        poolB = address(new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 10));

        vm.prank(owner);
        vault.setPoolOnce(poolA);
    }

    function test_Invariant_PoolCannotChange() public {
        // Tentar alterar deve reverter
        vm.prank(owner);
        vm.expectRevert(VaultErrors.PoolAlreadySet.selector);
        vault.setPoolOnce(poolB);

        assertEq(vault.pool(), poolA, "pool must remain the initial one");
    }
}
