// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface ISlipstreamGauge {
    function deposit(uint256 tokenId) external;
    function withdraw(uint256 tokenId) external;
    function getReward(uint256 tokenId) external;           // se a sua instância usar essa variante
    function getReward(address account) external;           // ou essa
    function stakedContains(address depositor, uint256 tokenId) external view returns (bool);
    function rewardToken() external view returns (address);
}
