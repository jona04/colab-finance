// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/console2.sol";
import "forge-std/Script.sol";
import { ISingleUserVault } from "../src/interfaces/ISingleUserVault.sol";

/// @notice Performs a manual rebalance on the vault.
/// @dev Expects env vars:
///      - PRIVATE_KEY
///      - VAULT_ADDRESS
///      - LOWER_TICK / UPPER_TICK
contract RebalanceManual is Script {
    function run() external {
        uint pk = vm.envUint("PRIVATE_KEY");
        address vault = vm.envAddress("VAULT_ADDRESS");
        int24 lower = int24(int(vm.envInt("LOWER_TICK")));
        int24 upper = int24(int(vm.envInt("UPPER_TICK")));

        vm.startBroadcast(pk);
        ISingleUserVault(vault).rebalance(lower, upper);
        vm.stopBroadcast();

        console2.log("Rebalanced vault:");
        console2.logAddress(vault);
        console2.log("New range (lower, upper):");
        console2.logInt(int(lower));
        console2.logInt(int(upper));
    }
}
