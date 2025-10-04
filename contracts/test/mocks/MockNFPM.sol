// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal NFPM mock exposing only `factory()` used by setPool* paths.
contract MockNFPM {
    address internal _factory;

    constructor(address factory_) {
        _factory = factory_;
    }

    function factory() external view returns (address) {
        return _factory;
    }
}
