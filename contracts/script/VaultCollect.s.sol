// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {ISingleUserVault} from "../src/interfaces/ISingleUserVault.sol";

contract VaultCollect is Script {
    /// @notice Reads VAULT_ADDRESS from env and calls collectFees().
    function run() external {
        address VAULT = vm.envAddress("VAULT_ADDRESS");
        require(VAULT != address(0), "VAULT_ADDRESS not set");

        vm.startBroadcast();
        ISingleUserVault(VAULT).collectFees();
        vm.stopBroadcast();
    }
}
