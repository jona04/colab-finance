// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import { IUniV3PoolMinimal } from "../../src/interfaces/IUniV3PoolMinimal.sol";

/// @notice Minimal pool mock implementing the required getters.
contract MockPool is IUniV3PoolMinimal {
    address internal _factory;
    address internal _token0;
    address internal _token1;
    uint24 internal _fee;
    int24 internal _tickSpacing;
    int24 internal _tick;

    constructor(
        address factory_,
        address token0_,
        address token1_,
        uint24 fee_,
        int24 tickSpacing_,
        int24 tick_
    ) {
        _factory = factory_;
        _token0 = token0_;
        _token1 = token1_;
        _fee = fee_;
        _tickSpacing = tickSpacing_;
        _tick = tick_;
    }

    function factory() external view returns (address) {
        return _factory;
    }

    function token0() external view returns (address) {
        return _token0;
    }

    function token1() external view returns (address) {
        return _token1;
    }

    function fee() external view returns (uint24) {
        return _fee;
    }

    function tickSpacing() external view returns (int24) {
        return _tickSpacing;
    }

    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24 tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8 feeProtocol,
            bool unlocked
        )
    {
        // Valores dummy suficientes para chamadas de leitura; TWAP não é usado nestes testes.
        return (uint160(79_228_162_514_264_337_593_543_950_336), _tick, 0, 0, 0, 0, true);
    }

    function observe(uint32[] calldata secondsAgos)
        external
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulativeX128)
    {
        // Retorna arrays do MESMO tamanho de secondsAgos, como o contrato real faz.
        uint n = secondsAgos.length;
        tickCumulatives = new int56[](n);
        secondsPerLiquidityCumulativeX128 = new uint160[](n);

        // Para unit tests que não dependem de TWAP real, valores zerados bastam.
        // (Tests de TWAP real devem ser em fork.)
        for (uint i = 0; i < n; i++) {
            tickCumulatives[i] = 0;
            secondsPerLiquidityCumulativeX128[i] = 0;
        }

        return (tickCumulatives, secondsPerLiquidityCumulativeX128);
    }

    function ticks(int24)
        external
        pure
        returns (
            uint128 liquidityGross,
            int128 liquidityNet,
            uint feeGrowthOutside0X128,
            uint feeGrowthOutside1X128,
            int56 tickCumulativeOutside,
            uint160 secondsPerLiquidityOutsideX128,
            uint32 secondsOutside,
            bool initialized
        )
    {
        return (0, 0, 0, 0, 0, 0, 0, false);
    }
}
