// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import { INonfungiblePositionManagerMinimal as NFPM } from
    "../../interfaces/INonfungiblePositionManagerMinimal.sol";

/// @title Thin adapter around NonfungiblePositionManager calls
/// @notice This adapter exists to isolate periphery changes and simplify mocks in tests.
/// @dev No state; pure forwarding (to be implemented in the main contract via composition or direct calls).
library UniV3PositionManagerAdapter {
// Placeholder for potential helper wrappers (e.g., safeMint, safeCollect).
// Intentionally left as a library to avoid storage.
}
