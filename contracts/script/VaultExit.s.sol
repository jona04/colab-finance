// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/console2.sol";
import "forge-std/Script.sol";
import { ISingleUserVault } from "../src/interfaces/ISingleUserVault.sol";

/// @notice Exits the Uniswap V3 position to the vault (decrease+collect+burn).
/// @dev Expects env vars:
///      - PRIVATE_KEY
///      - VAULT_ADDRESS
contract VaultExit is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address vault = vm.envAddress("VAULT_ADDRESS");

        vm.startBroadcast(pk);
        // must match the function you added in the vault
        ISingleUserVault(vault).exitPositionToVault();
        vm.stopBroadcast();

        console2.log("Exited position to vault for:");
        console2.logAddress(vault);
    }
}
