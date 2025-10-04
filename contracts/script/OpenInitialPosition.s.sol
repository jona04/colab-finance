// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import { ISingleUserVault } from "../src/interfaces/ISingleUserVault.sol";

/// @notice Opens the initial Uniswap v3 LP position on the vault.
/// @dev Expects env vars:
///      - PRIVATE_KEY: deployer/operator key
///      - VAULT_ADDRESS: deployed vault
///      - LOWER_TICK / UPPER_TICK: integers (int24 range)
contract OpenInitialPosition is Script {
    function run() external {
        uint pk = vm.envUint("PRIVATE_KEY");
        address vault = vm.envAddress("VAULT_ADDRESS");
        int24 lower = int24(int(vm.envInt("LOWER_TICK")));
        int24 upper = int24(int(vm.envInt("UPPER_TICK")));

        vm.startBroadcast(pk);
        ISingleUserVault(vault).openInitialPosition(lower, upper);
        vm.stopBroadcast();

        console2.log("Opened initial position on:");
        console2.logAddress(vault);
        console2.log("Range (lower, upper):");
        console2.logInt(int(lower));
        console2.logInt(int(upper));
    }
}
