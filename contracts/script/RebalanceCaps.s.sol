// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {ISingleUserVault} from "../src/interfaces/ISingleUserVault.sol";

contract RebalanceCaps is Script {
    function run() external {
        address vault = vm.envAddress("VAULT_ADDRESS");
        int24 lower = int24(vm.envInt("LOWER_TICK"));
        int24 upper = int24(vm.envInt("UPPER_TICK"));
        uint256 cap0 = vm.envUint("CAP0_RAW");
        uint256 cap1 = vm.envUint("CAP1_RAW");

        vm.startBroadcast();
        ISingleUserVault(vault).rebalanceWithCaps(lower, upper, cap0, cap1);
        vm.stopBroadcast();
    }
}
