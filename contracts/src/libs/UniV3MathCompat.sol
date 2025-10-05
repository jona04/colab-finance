// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// OpenZeppelin mulDiv para precisão 512-bit sem assembly manual
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Interface mínima da pool v3 para leitura
interface IUniV3PoolMinimalCompat {
    function slot0() external view returns (
        uint160 sqrtPriceX96,
        int24 tick,
        uint16 observationIndex,
        uint16 observationCardinality,
        uint16 observationCardinalityNext,
        uint8 feeProtocol,
        bool unlocked
    );
    function tickSpacing() external view returns (int24);
}

/// @title UniV3MathCompat
/// @notice Helpers compatíveis com Solidity 0.8.x para calcular sqrtPrice@tick e amounts de uma posição.
library UniV3MathCompat {
    uint256 internal constant Q96  = 2**96;
    uint256 internal constant Q192 = 2**192;
    int24   internal constant MIN_TICK = -887272;
    int24   internal constant MAX_TICK =  887272;

    /// @notice Retorna sqrt(1.0001^tick) * 2^96 (equivalente ao TickMath.getSqrtRatioAtTick).
    function getSqrtRatioAtTick(int24 tick) internal pure returns (uint160 sqrtPriceX96) {
        require(tick >= MIN_TICK && tick <= MAX_TICK, "tick out of range");
        uint256 absTick = uint256(int256(tick < 0 ? -tick : tick));

        // acumulador em Q128.128 (implícito via produto de constantes)
        uint256 ratio = 0x100000000000000000000000000000000; // 1 << 128

        // Cada constante é sqrt(1.0001^2^i) * 2^128, i = {0..19}
        if (absTick & 0x1 != 0)   ratio = (ratio * 0xfffcb933bd6fad37aa2d162d1a594001) >> 128;
        if (absTick & 0x2 != 0)   ratio = (ratio * 0xfff97272373d413259a46990580e213a) >> 128;
        if (absTick & 0x4 != 0)   ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdcc) >> 128;
        if (absTick & 0x8 != 0)   ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0) >> 128;
        if (absTick & 0x10 != 0)  ratio = (ratio * 0xffcb9843d60f6159c9db58835c926644) >> 128;
        if (absTick & 0x20 != 0)  ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0) >> 128;
        if (absTick & 0x40 != 0)  ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861) >> 128;
        if (absTick & 0x80 != 0)  ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053) >> 128;
        if (absTick & 0x100 != 0) ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4) >> 128;
        if (absTick & 0x200 != 0) ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54) >> 128;
        if (absTick & 0x400 != 0) ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3) >> 128;
        if (absTick & 0x800 != 0) ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9) >> 128;
        if (absTick & 0x1000 != 0)ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825) >> 128;
        if (absTick & 0x2000 != 0)ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5) >> 128;
        if (absTick & 0x4000 != 0)ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7) >> 128;
        if (absTick & 0x8000 != 0)ratio = (ratio * 0x31be135f97d08fd981231505542fcfa6) >> 128;
        if (absTick & 0x10000 != 0)ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9) >> 128;
        if (absTick & 0x20000 != 0)ratio = (ratio * 0x5d6af8dedb81196699c329225ee604) >> 128;
        if (absTick & 0x40000 != 0)ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98) >> 128;
        if (absTick & 0x80000 != 0)ratio = (ratio * 0x48a170391f7dc42444e8fa2) >> 128;

        if (tick > 0) {
            // ratio = type(uint256).max / ratio  (com arredondamento)
            ratio = Math.mulDiv(type(uint256).max, 1, ratio);
        }

        // converte de Q128.128 para Q64.96 com arredondamento
        // sqrtPriceX96 = uint160((ratio >> 32) + (ratio % (1<<32) == 0 ? 0 : 1));
        uint256 rShift = ratio >> 32;
        if (ratio & ((1 << 32) - 1) != 0) rShift += 1;
        require(rShift <= type(uint160).max, "overflow");
        sqrtPriceX96 = uint160(rShift);
    }

    /// @notice Equivalente ao LiquidityAmounts.getAmountsForLiquidity
    /// @dev Assume sqrtRatioAX96 <= sqrtRatioBX96
    function getAmountsForLiquidity(
        uint160 sqrtRatioX96,
        uint160 sqrtRatioAX96,
        uint160 sqrtRatioBX96,
        uint128 liquidity
    ) internal pure returns (uint256 amount0, uint256 amount1) {
        require(sqrtRatioAX96 < sqrtRatioBX96, "A<B");

        if (sqrtRatioX96 <= sqrtRatioAX96) {
            // Tudo em token0
            amount0 = _getAmount0ForLiquidity(sqrtRatioAX96, sqrtRatioBX96, liquidity);
        } else if (sqrtRatioX96 < sqrtRatioBX96) {
            // Parte em token0 e parte em token1
            amount0 = _getAmount0ForLiquidity(sqrtRatioX96, sqrtRatioBX96, liquidity);
            amount1 = _getAmount1ForLiquidity(sqrtRatioAX96, sqrtRatioX96, liquidity);
        } else {
            // Tudo em token1
            amount1 = _getAmount1ForLiquidity(sqrtRatioAX96, sqrtRatioBX96, liquidity);
        }
    }

    function _getAmount0ForLiquidity(
        uint160 sqrtRatioAX96,
        uint160 sqrtRatioBX96,
        uint128 liquidity
    ) private pure returns (uint256 amount0) {
        // amount0 = L * (sqrtB - sqrtA) / (sqrtB * sqrtA) * Q96
        uint256 numerator1 = uint256(liquidity) * (sqrtRatioBX96 - sqrtRatioAX96);
        uint256 denom = uint256(sqrtRatioBX96) * uint256(sqrtRatioAX96);
        amount0 = Math.mulDiv(numerator1, Q96, denom);
    }

    function _getAmount1ForLiquidity(
        uint160 sqrtRatioAX96,
        uint160 sqrtRatioBX96,
        uint128 liquidity
    ) private pure returns (uint256 amount1) {
        // amount1 = L * (sqrtB - sqrtA) / Q96
        amount1 = Math.mulDiv(uint256(liquidity), (sqrtRatioBX96 - sqrtRatioAX96), 1);
        // divide por Q96
        amount1 = amount1 / Q96;
    }

    /// @notice Atalho: calcula amounts em range lendo `slot0()` da pool.
    function amountsInRangeView(
        address pool,
        int24 tickLower,
        int24 tickUpper,
        uint128 liquidity
    ) internal view returns (uint256 amount0, uint256 amount1) {
        (uint160 sqrtP, , , , , , ) = IUniV3PoolMinimalCompat(pool).slot0();
        uint160 sqrtA = getSqrtRatioAtTick(tickLower);
        uint160 sqrtB = getSqrtRatioAtTick(tickUpper);
        if (sqrtA > sqrtB) {
            (sqrtA, sqrtB) = (sqrtB, sqrtA);
        }
        return getAmountsForLiquidity(sqrtP, sqrtA, sqrtB, liquidity);
    }
}
