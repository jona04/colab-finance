// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import { SingleUserVault } from "../../src/core/SingleUserVault.sol";
import { MockNFPM } from "../mocks/MockNFPM.sol";
import { MockPool } from "../mocks/MockPool.sol";
import { VaultErrors } from "../../src/errors/VaultErrors.sol";

contract SingleUserVault_SetPool_Test is Test {
    SingleUserVault vault;
    MockNFPM nfpm;
    address owner = address(0xBEEF);

    function setUp() public {
        // O prank precisa cobrir as duas criações; use start/stop
        vm.startPrank(owner);
        nfpm = new MockNFPM(address(0xFAcA0A)); // NFPM.factory() = A
        vault = new SingleUserVault(address(nfpm)); // owner correto = 0xBEEF
        vm.stopPrank();
    }

    function test_SetPoolOnce_Reverts_WhenPoolFactoryDiffers() public {
        // Pool criada por factory B (≠ A)
        MockPool pool = new MockPool(address(0xFAcA0B), address(1), address(2), 3000, 60, 0);

        vm.prank(owner);
        vm.expectRevert(VaultErrors.InvalidFactory.selector);
        vault.setPoolOnce(address(pool));
    }

    function test_SetPoolOnce_Succeeds_WhenFactoryMatches() public {
        // Pool criada pela mesma factory A
        MockPool pool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0);

        vm.prank(owner);
        vault.setPoolOnce(address(pool));
        assertEq(vault.pool(), address(pool));
    }

    function test_SetPoolOnce_OnlyOwner() public {
        MockPool pool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0);

        // Sem prank: caller = contrato de teste (≠ owner) → NotOwner
        vm.expectRevert(VaultErrors.NotOwner.selector);
        vault.setPoolOnce(address(pool));
    }
}
