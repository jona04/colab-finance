// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/console2.sol";
import "forge-std/Script.sol";
import { ISingleUserVault } from "../src/interfaces/ISingleUserVault.sol";

/// @notice Exits the position and withdraws all vault balances to the owner.
/// @dev Expects env vars:
///      - PRIVATE_KEY
///      - VAULT_ADDRESS
contract VaultExitWithdraw is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address vault = vm.envAddress("VAULT_ADDRESS");

        vm.startBroadcast(pk);
        // single atomic call that: decrease+collect+burn and then transfer all balances
        ISingleUserVault(vault).exitAndWithdrawAll();
        vm.stopBroadcast();

        console2.log("Exited position and withdrew all to owner for:");
        console2.logAddress(vault);
    }
}
