// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IAeroRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        int24  tickSpacing;          // difere do Uniswap (fee)
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}