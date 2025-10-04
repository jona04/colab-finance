// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import { SingleUserVault } from "../src/core/SingleUserVault.sol";

/// @notice Deploy script for SingleUserVault.
/// @dev Uses PRIVATE_KEY and RPC from environment.
contract Deploy is Script {
    function run() external {
        uint pk = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(pk);

        // Read NFPM from env or constants; for now, require env var
        address nfpm = vm.envAddress("NFPM_ADDRESS");
        SingleUserVault vault = new SingleUserVault(nfpm);

        vm.stopBroadcast();

        console2.log("SingleUserVault deployed at:", address(vault));
        console2.log("NFPM:", nfpm);
    }
}
