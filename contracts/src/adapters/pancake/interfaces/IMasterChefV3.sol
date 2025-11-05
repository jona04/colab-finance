// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IMasterChefV3 {
    // leitura opcional
    function CAKE() external view returns (address);
    function v3PoolAddressPid(address v3Pool) external view returns (uint256);
    function pendingCake(uint256 tokenId) external view returns (uint256);

    // ações
    function harvest(uint256 tokenId, address to) external returns (uint256 reward);
    function withdraw(uint256 tokenId, address to) external returns (uint256 reward);
}
