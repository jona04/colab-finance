// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import { IUniV3PoolMinimal } from "../../interfaces/IUniV3PoolMinimal.sol";

/// @title Uniswap V3 TWAP helper
/// @notice Encapsula o cálculo de TWAP usando `observe`.
library UniV3TwapOracle {
    /// @notice Retorna o tick médio aritmético (TWAP) para a janela `window`.
    /// @dev Reverte se a pool não tiver observações suficientes para a janela solicitada.
    /// @param pool Pool Uniswap v3
    /// @param window Janela em segundos (deve ser > 0)
    /// @return twapTick Tick médio na janela
    function consultTick(IUniV3PoolMinimal pool, uint32 window)
        internal
        view
        returns (int24 twapTick)
    {
        require(window > 0, "TWAP_WINDOW_ZERO");

        // Constrói o vetor [window, 0] em memória e evita colisões de nome.
        uint32[] memory secondsAgos = _buildSecondsAgos(window);

        (int56[] memory tickCumulatives,) = pool.observe(secondsAgos);

        int56 delta = tickCumulatives[1] - tickCumulatives[0];
        int56 avg = delta / int56(uint56(window));
        twapTick = int24(avg); // Uniswap garante que cabe em int24
    }

    /// @notice Constrói o vetor exigido por `observe()`: [window, 0].
    /// @dev Função separada para evitar qualquer ambiguidade de identificador na função principal.
    function _buildSecondsAgos(uint32 window) private pure returns (uint32[] memory arr) {
        arr = new uint32[](2);
        arr[0] = window; // window segundos atrás
        arr[1] = 0; // agora
    }
}
