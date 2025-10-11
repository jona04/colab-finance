// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {SingleUserVault} from "../src/core/SingleUserVault.sol";

/// @notice Minimal, safe deploy script for SingleUserVault (no `address(this)` usage).
/// @dev Env vars:
///      - NFPM_ADDRESS (required)
///      - POOL_ADDRESS (optional) -> if present, calls setPoolOnce()
contract VaultCreate is Script {
    function run() external {
        // required
        address nfpm = vm.envAddress("NFPM_ADDRESS");

        // optional POOL_ADDRESS (if not set, keep as zero)
        address pool = _envAddressOrZero("POOL_ADDRESS");

        vm.startBroadcast();

        // deploy; owner = your EOA (broadcaster)
        SingleUserVault vault = new SingleUserVault(nfpm);

        if (pool != address(0)) {
            vault.setPoolOnce(pool);
        }

        vm.stopBroadcast();

        console2.log("Deployed SingleUserVault at:", address(vault));
    }

    /// @dev Reads an address env if present; returns address(0) if missing.
    function _envAddressOrZero(string memory key) internal returns (address) {
        // IMPORTANT: no use of `this` here; direct try/catch on cheatcode call
        try vm.envAddress(key) returns (address a) {
            return a;
        } catch {
            return address(0);
        }
    }
}
