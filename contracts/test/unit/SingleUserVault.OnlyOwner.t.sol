// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {SingleUserVault} from "../../src/core/SingleUserVault.sol";
import {MockNFPM} from "../mocks/MockNFPM.sol";
import {MockPool} from "../mocks/MockPool.sol";
import {VaultErrors} from "../../src/errors/VaultErrors.sol";

contract SingleUserVault_OnlyOwner_Test is Test {
    SingleUserVault vault;
    MockNFPM nfpm;
    MockPool pool;

    address owner = address(0xBEEF);
    address attacker = address(0xBAD);

    function setUp() public {
        vm.startPrank(owner);
        nfpm = new MockNFPM(address(0xFAcA0A));
        vault = new SingleUserVault(address(nfpm));
        vm.stopPrank();

        pool = new MockPool(address(0xFAcA0A), address(1), address(2), 3000, 60, 0);

        vm.prank(owner);
        vault.setPoolOnce(address(pool));
    }

    function test_OpenInitialPosition_OnlyOwner() public {
        vm.prank(attacker);
        vm.expectRevert(VaultErrors.NotOwner.selector);
        vault.openInitialPosition(-120, -60);
    }

    function test_Rebalance_OnlyOwner() public {
        vm.prank(attacker);
        vm.expectRevert(VaultErrors.NotOwner.selector);
        vault.rebalance(-120, -60);
    }

    function test_WithdrawAll_OnlyOwner() public {
        vm.prank(attacker);
        vm.expectRevert(VaultErrors.NotOwner.selector);
        vault.withdrawAll(); // use esta assinatura se já migrou para a versão sem params
    }
}
