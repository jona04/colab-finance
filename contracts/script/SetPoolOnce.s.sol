// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import { ISingleUserVault } from "../src/interfaces/ISingleUserVault.sol";

/// @notice Utility script to set the pool (direct) or via factory (later).
contract SetPoolOnce is Script {
    function run() external {
        uint pk = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(pk);

        address vault = vm.envAddress("VAULT_ADDRESS");
        address pool = vm.envAddress("POOL_ADDRESS");

        ISingleUserVault(vault).setPoolOnce(pool);

        vm.stopBroadcast();
        console2.log("Pool set on vault", vault, "->", pool);
    }
}
