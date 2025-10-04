// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {ISingleUserVault} from "../src/interfaces/ISingleUserVault.sol";
import {INonfungiblePositionManagerMinimal as NFPM} from "../src/interfaces/INonfungiblePositionManagerMinimal.sol";
import {IUniV3PoolMinimal} from "../src/interfaces/IUniV3PoolMinimal.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/// @title ViewState
/// @notice Script de leitura (sem broadcast) para inspecionar o estado do SingleUserVault e da pool.
/// @dev Espera a variável de ambiente: VAULT_ADDRESS. Opcionalmente aceita RPC via CLI (--rpc-url).
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
        address pool  = V.pool();
        uint256 tokenId = V.positionTokenId();

        console2.log("=== Vault ===");
        console2.log("vault:           ", vault);
        console2.log("owner:           ", owner);
        console2.log("pool:            ", pool);
        console2.log("positionTokenId: ", tokenId);
        console2.log("");

        // Guard params
        console2.log("=== Guards ===");
        try this.readUint256(vault, "minCooldown()") returns (uint256 mc) {
            console2.log("minCooldown (s): ", mc);
        } catch {}
        try this.readInt24(vault, "minWidth()") returns (int24 mw) {
            console2.log("minWidth (ticks):", mw);
        } catch {}
        try this.readInt24(vault, "maxWidth()") returns (int24 Mw) {
            console2.log("maxWidth (ticks):", Mw);
        } catch {}
        try this.readInt24(vault, "maxTwapDeviationTicks()") returns (int24 d) {
            console2.log("maxTwapDev (ticks):", d);
        } catch {}
        try this.readUint32(vault, "twapWindow()") returns (uint32 tw) {
            console2.log("twapWindow (s):  ", tw);
        } catch {}
        try this.readUint256(vault, "lastRebalance()") returns (uint256 lr) {
            console2.log("lastRebalance:   ", lr);
        } catch {}
        try this.readBool(vault, "twapOk()") returns (bool ok) {
            console2.log("twapOk():        ", ok);
        } catch {}
        console2.log("");

        if (pool == address(0)) {
            console2.log("pool not set; nothing else to show.");
            return;
        }

        // Pool
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
        console2.log("token0 symbol: ", m0.symbol,", decimals: ", uint256(m0.decimals));
        console2.log("token1: ", token1);
        console2.log("token1 symbol: ", m1.symbol," decimals:", uint256(m1.decimals));
        console2.log("fee:    ", fee);
        console2.log("spacing:", spacing);
        console2.log("spotTick:", spotTick);
        console2.log("sqrtPriceX96:", sqrtPX96);
        console2.log("");

        // Saldos do vault
        uint256 bal0 = IERC20(token0).balanceOf(vault);
        uint256 bal1 = IERC20(token1).balanceOf(vault);

        console2.log("=== Vault balances ===");
        _printAmount("token0", bal0, m0.decimals);
        _printAmount("token1", bal1, m1.decimals);
        console2.log("");

        // currentRange (pode reverter se tokenId==0)
        console2.log("=== Position ===");
        if (tokenId == 0) {
            console2.log("no position opened yet");
        } else {
            try V.currentRange() returns (int24 lower, int24 upper, uint128 liq) {
                console2.log("lower:   ", lower);
                console2.log("upper:   ", upper);
                console2.log("liquidity", uint256(liq));
            } catch {
                console2.log("currentRange(): revert (position not available)");
            }

            // Detalhes no NFPM
            address nfpm = this.readAddress(vault, "nfpm()");
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

    // --------
    // Helpers
    // --------

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
        // Imprime quantidade crua e aproximada em unidades humanas (com 4 casas)
        console2.log(string(abi.encodePacked(label, " (raw): ")), raw);
        uint256 denom = 10 ** decimals_;
        uint256 intPart = raw / denom;
        uint256 frac = (raw % denom) * 10_000 / denom; // 4 casas
        console2.log(string(abi.encodePacked(label, " (human): ")), intPart, ".", frac);
    }

    // Generic read helpers (evita precisar de interfaces extras)
    function readAddress(address target, string memory sig) external view returns (address a) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        a = abi.decode(data, (address));
    }

    function readUint256(address target, string memory sig) external view returns (uint256 v) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        v = abi.decode(data, (uint256));
    }

    function readUint32(address target, string memory sig) external view returns (uint32 v) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        v = abi.decode(data, (uint32));
    }

    function readInt24(address target, string memory sig) external view returns (int24 v) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        v = abi.decode(data, (int24));
    }

    function readBool(address target, string memory sig) external view returns (bool v) {
        (bool ok, bytes memory data) = target.staticcall(abi.encodeWithSignature(sig));
        require(ok, "staticcall fail");
        v = abi.decode(data, (bool));
    }
}
