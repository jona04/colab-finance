// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ISingleUserVault} from "../src/interfaces/ISingleUserVault.sol";
import {INonfungiblePositionManagerMinimal as NFPM} from "../src/interfaces/INonfungiblePositionManagerMinimal.sol";
import {IUniV3PoolMinimal} from "../src/interfaces/IUniV3PoolMinimal.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/// @title ViewState
/// @notice Script de leitura (sem broadcast) para inspecionar o estado do SingleUserVault e da pool.
/// @dev Espera VAULT_ADDRESS no ambiente.
contract ViewState is Script {
    struct TokenMeta {
        string symbol;
        uint8 decimals;
    }

    function run() external view {
        address vault = vm.envAddress("VAULT_ADDRESS");

        // Vault
        ISingleUserVault V = ISingleUserVault(vault);
        address owner = V.owner();
        address pool = V.pool();
        uint256 tokenId = V.positionTokenId();

        console2.log("=== Vault ===");
        console2.log("vault:           ", vault);
        console2.log("owner:           ", owner);
        console2.log("pool:            ", pool);
        console2.log("positionTokenId: ", tokenId);
        console2.log("");

        // Guards (leitura genérica sem usar address(this))
        console2.log("=== Guards ===");

        bool okBool; bool twapOkVal;
        (okBool, twapOkVal) = _readBool(vault, "twapOk()");
        if (okBool) console2.log("twapOk():        ", twapOkVal);

        bool okU; uint256 minCooldown;
        (okU, minCooldown) = _readUint256(vault, "minCooldown()");
        if (okU) console2.log("minCooldown (s): ", minCooldown);

        uint256 lastRebalance_;
        (okU, lastRebalance_) = _readUint256(vault, "lastRebalance()");
        if (okU) console2.log("lastRebalance:   ", lastRebalance_);

        bool okI; int24 minWidth; int24 maxWidth; int24 maxTwapDev;
        (okI, minWidth) = _readInt24(vault, "minWidth()");
        if (okI) console2.log("minWidth (ticks):", minWidth);

        (okI, maxWidth) = _readInt24(vault, "maxWidth()");
        if (okI) console2.log("maxWidth (ticks):", maxWidth);

        (okI, maxTwapDev) = _readInt24(vault, "maxTwapDeviationTicks()");
        if (okI) console2.log("maxTwapDev (ticks):", maxTwapDev);

        bool okU32; uint32 twapWindow;
        (okU32, twapWindow) = _readUint32(vault, "twapWindow()");
        if (okU32) console2.log("twapWindow (s):  ", twapWindow);

        console2.log("");

        if (pool == address(0)) {
            console2.log("pool not set; nothing else to show.");
            return;
        }

        // Pool data
        IUniV3PoolMinimal P = IUniV3PoolMinimal(pool);
        address token0 = P.token0();
        address token1 = P.token1();
        uint24 fee = P.fee();
        int24 spacing = P.tickSpacing();
        (uint160 sqrtPX96, int24 spotTick, , , , , ) = P.slot0();

        TokenMeta memory m0 = _meta(token0);
        TokenMeta memory m1 = _meta(token1);

        console2.log("=== Pool ===");
        console2.log("token0: ", token0);
        console2.log("token0 symbol: ", m0.symbol, ", decimals: ", uint256(m0.decimals));
        console2.log("token1: ", token1);
        console2.log("token1 symbol: ", m1.symbol, ", decimals: ", uint256(m1.decimals));
        console2.log("fee:    ", fee);
        console2.log("spacing:", spacing);
        console2.log("spotTick:", spotTick);
        console2.log("sqrtPriceX96:", sqrtPX96);

        // Preço atual (token1 por token0), ajustado por decimais
        (uint256 priceNum, uint256 priceDen, uint256 priceScaled) = _priceFromSqrtX96(sqrtPX96, m0.decimals, m1.decimals);
        console2.log("price (raw frac) token1/token0 = ", priceNum, "/", priceDen);
        console2.log("price (scaled)   token1/token0 = ", priceScaled); // 1e18 scale
        console2.log("");

        // Saldos do vault
        uint256 bal0 = IERC20(token0).balanceOf(vault);
        uint256 bal1 = IERC20(token1).balanceOf(vault);

        console2.log("=== Vault balances ===");
        _printAmount("token0", bal0, m0.decimals);
        _printAmount("token1", bal1, m1.decimals);
        console2.log("");

        // Posição
        console2.log("=== Position ===");
        if (tokenId == 0) {
            console2.log("no position opened yet");
        } else {
            (int24 lower, int24 upper, uint128 liq) = V.currentRange();
            console2.log("lower:   ", lower);
            console2.log("upper:   ", upper);
            console2.log("liquidity", uint256(liq));

            bool inRange = (spotTick >= lower && spotTick < upper);
            console2.log("inRange: ", inRange);

            // Detalhes no NFPM
            address nfpm = _readAddressOrRevert(vault, "nfpm()");
            console2.log("nfpm:    ", nfpm);

            (
                ,
                ,
                address posToken0,
                address posToken1,
                uint24 posFee,
                int24 posLower,
                int24 posUpper,
                uint128 posLiq,
                ,
                ,
                uint128 owed0,
                uint128 owed1
            ) = NFPM(nfpm).positions(tokenId);

            console2.log("pos token0: ", posToken0);
            console2.log("pos token1: ", posToken1);
            console2.log("pos fee:    ", posFee);
            console2.log("pos lower:  ", posLower);
            console2.log("pos upper:  ", posUpper);
            console2.log("pos liq:    ", uint256(posLiq));
            console2.log("tokensOwed0:", uint256(owed0));
            console2.log("tokensOwed1:", uint256(owed1));
        }
    }

    // ========= Helpers (internos; sem address(this)) =========

    function _meta(address token) internal view returns (TokenMeta memory m) {
        m.symbol = _trySymbol(token);
        m.decimals = _tryDecimals(token);
    }

    function _trySymbol(address token) internal view returns (string memory s) {
        try IERC20Metadata(token).symbol() returns (string memory sym) {
            return sym;
        } catch {
            return "?";
        }
    }

    function _tryDecimals(address token) internal view returns (uint8 d) {
        try IERC20Metadata(token).decimals() returns (uint8 dec) {
            return dec;
        } catch {
            return 18; // fallback comum
        }
    }

    function _printAmount(string memory label, uint256 raw, uint8 decimals_) internal pure {
        console2.log(string(abi.encodePacked(label, " (raw): ")), raw);
        uint256 denom = 10 ** decimals_;
        uint256 intPart = denom == 0 ? raw : raw / denom;
        uint256 frac = denom == 0 ? 0 : (raw % denom) * 10_000 / denom; // 4 casas
        console2.log(string(abi.encodePacked(label, " (human): ")), intPart, ".", frac);
    }

    // ---- generic staticcall helpers (retornam ok + valor) ----

    function _readAddressOrRevert(address target, string memory sig) internal view returns (address a) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        a = abi.decode(data, (address));
    }

    function _readUint256(address target, string memory sig) internal view returns (bool ok, uint256 v) {
        (ok, v) = _readUint256Raw(target, sig);
    }

    function _readUint32(address target, string memory sig) internal view returns (bool ok, uint32 v) {
        bytes memory data;
        (ok, data) = target.staticcall(abi.encodeWithSignature(sig));
        if (ok) v = abi.decode(data, (uint32));
    }

    function _readInt24(address target, string memory sig) internal view returns (bool ok, int24 v) {
        bytes memory data;
        (ok, data) = target.staticcall(abi.encodeWithSignature(sig));
        if (ok) v = abi.decode(data, (int24));
    }

    function _readBool(address target, string memory sig) internal view returns (bool ok, bool v) {
        bytes memory data;
        (ok, data) = target.staticcall(abi.encodeWithSignature(sig));
        if (ok) v = abi.decode(data, (bool));
    }

    function _readUint256Raw(address target, string memory sig) internal view returns (bool ok, uint256 v) {
        bytes memory dataOut;
        (ok, dataOut) = target.staticcall(abi.encodeWithSignature(sig));
        if (ok) v = abi.decode(dataOut, (uint256));
    }

    // ---- preço a partir de sqrtPriceX96 ----
    /// @notice Retorna (numerador, denominador, priceScaled1e18) para token1/token0.
    function _priceFromSqrtX96(uint160 sqrtPX96, uint8 dec0, uint8 dec1)
        internal
        pure
        returns (uint256 num, uint256 den, uint256 scaled)
    {
        // price = (sqrtPX96^2 / 2^192) * 10^(dec0 - dec1)  [token1 por token0]
        // Para evitar overflow: faz-se em 256-bit com divisão passo a passo.
        uint256 sq = uint256(sqrtPX96) * uint256(sqrtPX96);        // até 160*2 = 320 bits, cabe em 256? não, mas sqrtPX96 <= 2^96 => quadrado <= 2^192 (cabe!)
        uint256 Q192 = 2**192;

        // Ajuste por decimais
        if (dec0 >= dec1) {
            uint256 scale = 10 ** (dec0 - dec1);
            num = sq * scale;
            den = Q192;
        } else {
            uint256 scale = 10 ** (dec1 - dec0);
            num = sq;
            den = Q192 * scale;
        }

        // Scaled para 1e18 (apresentação)
        // scaled = (num * 1e18) / den
        if (den == 0) {
            scaled = 0;
        } else {
            // prevenir overflow: num*(1e18) cabe em 256? num <= ~2^192 * 1e12 => ainda cabe.
            scaled = den == 0 ? 0 : Math.mulDiv(num, 1e18, den);
        }
    }
}
