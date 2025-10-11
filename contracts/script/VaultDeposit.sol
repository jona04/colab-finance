// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title VaultDeposit
 * @notice Forge script to transfer ERC20 tokens from the deployer EOA to a vault address.
 * @dev Reads env vars via vm.env*: VAULT_ADDRESS, TOKEN_ADDRESS, AMOUNT_RAW.
 *      Broadcasts from the private key passed by `forge script --private-key`.
 *
 * Safety:
 * - This script calls ERC20.transfer(vault, amount). No approvals are needed because
 *   the transfer is from msg.sender to the vault (EOA -> vault).
 * - Ensure AMOUNT_RAW already accounts for token decimals.
 */
import "forge-std/Script.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

contract VaultDeposit is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY"); // passed by --private-key (required by forge)
        address vault = vm.envAddress("VAULT_ADDRESS");
        address token = vm.envAddress("TOKEN_ADDRESS");
        uint256 amountRaw = vm.envUint("AMOUNT_RAW");

        vm.startBroadcast(pk);

        bool ok = IERC20(token).transfer(vault, amountRaw);
        require(ok, "ERC20.transfer failed");

        vm.stopBroadcast();

        // Pretty console logs for the bot to pick up
        try IERC20(token).symbol() returns (string memory sym) {
            console2.log("Deposited:", amountRaw, sym);
        } catch {
            console2.log("Deposited (raw):", amountRaw);
        }
        console2.log("Vault:", vault);
        console2.log("Token:", token);
    }
}