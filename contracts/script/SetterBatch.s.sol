// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {AutomatedSet} from "../src/AutomatedSet.sol";

/// @notice Adds or removes a batch of wallets on AutomatedSet from the setter wallet.
///
/// Environment:
///   AUTOMATED_SET  the set's address (required)
///   WALLETS        comma-separated wallet addresses (required)
///   REMOVE         "true" to remove instead of add (default: add)
///
/// Run:  forge script script/SetterBatch.s.sol --rpc-url $RPC --private-key $SETTER_KEY --broadcast
contract SetterBatch is Script {
    function run() external {
        AutomatedSet set = AutomatedSet(vm.envAddress("AUTOMATED_SET"));
        address[] memory wallets = vm.envAddress("WALLETS", ",");
        bool remove = vm.envOr("REMOVE", false);
        require(wallets.length > 0, "WALLETS is empty");

        vm.startBroadcast();
        if (remove) set.remove(wallets); else set.add(wallets);
        vm.stopBroadcast();

        console2.log(remove ? "removed" : "added", wallets.length, "wallets; set size now", set.size());
    }
}
