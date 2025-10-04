// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Errors used by SingleUserVault
/// @notice Custom errors are cheaper than string reverts.
library VaultErrors {
    error PoolAlreadySet();
    error PoolNotSet();
    error NotOwner();
    error InvalidWidth();
    error CooldownNotPassed();
    error TwapDeviationTooHigh();
    error ZeroAddress();
    error InvalidFactory();
    error PositionNotInitialized();
    error NotImplemented();
    error PositionAlreadyOpened();
    error InvalidTickSpacing();
}
