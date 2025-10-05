// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import { SingleUserVault } from "../../src/core/SingleUserVault.sol";
import { MockNFPM } from "../mocks/MockNFPM.sol";
import { MockPool } from "../mocks/MockPool.sol";
import { VaultErrors } from "../../src/errors/VaultErrors.sol";
import { ERC20 } from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @dev Simple mintable ERC20 for tests.
contract MockERC20 is ERC20 {
    constructor(string memory n, string memory s) ERC20(n, s) { }

    function mint(address to, uint amt) external {
        _mint(to, amt);
    }
}

/// @title WithdrawAll unit tests
contract SingleUserVault_Withdraw_Test is Test {
    SingleUserVault vault;
    MockNFPM nfpm;
    MockPool pool;

    MockERC20 t0;
    MockERC20 t1;

    address owner = address(0xBEEF);

    function setUp() public {
        vm.startPrank(owner);
        nfpm = new MockNFPM(address(0xFAcA0A));
        vault = new SingleUserVault(address(nfpm));
        vm.stopPrank();

        t0 = new MockERC20("Token0", "T0");
        t1 = new MockERC20("Token1", "T1");

        // Pool aponta para t0/t1
        pool = new MockPool(address(0xFAcA0A), address(t0), address(t1), 3000, 60, 0);

        vm.prank(owner);
        vault.setPoolOnce(address(pool));
    }

    function test_Revert_WhenPoolNotSet() public {
        vm.startPrank(owner);
        SingleUserVault v2 = new SingleUserVault(address(nfpm));
        // withdrawAll() deve depender da pool para descobrir tokens
        vm.expectRevert(VaultErrors.PoolNotSet.selector);
        v2.withdrawAll();
        vm.stopPrank();
    }

    function test_TransfersAllBalancesToOwner() public {
        // Deposita saldos no vault
        t0.mint(address(vault), 1000 ether);
        t1.mint(address(vault), 500 ether);

        uint pre0 = t0.balanceOf(owner);
        uint pre1 = t1.balanceOf(owner);

        vm.prank(owner);
        vault.withdrawAll();

        assertEq(t0.balanceOf(address(vault)), 0);
        assertEq(t1.balanceOf(address(vault)), 0);
        assertEq(t0.balanceOf(owner), pre0 + 1000 ether);
        assertEq(t1.balanceOf(owner), pre1 + 500 ether);
    }
}
