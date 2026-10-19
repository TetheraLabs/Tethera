// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IPoolManager} from "v4-core/interfaces/IPoolManager.sol";
import {Hooks} from "v4-core/libraries/Hooks.sol";
import {AutomatedSet} from "../src/AutomatedSet.sol";
import {CrowdingHook} from "../src/CrowdingHook.sol";

/// @notice Deploys AutomatedSet, then CrowdingHook at a CREATE2 address whose low 14 bits are exactly the
///         permission flags the hook declares (the pool manager reads permissions from the address).
///
/// Environment:
///   POOL_MANAGER   address of the Uniswap v4 PoolManager on the target chain (required)
///   TREASURY       address that receives Tethera's share of the surcharge (required)
///   OWNER          owner of both contracts (default: the broadcasting sender)
///   SETTER         the labelling job's hot wallet, allowed to add/remove on AutomatedSet (default: OWNER)
///   AUTOMATED_SET  reuse an existing AutomatedSet instead of deploying one (optional)
///
/// Run:  forge script script/Deploy.s.sol --rpc-url $RPC --ledger --sender <owner> --broadcast
contract Deploy is Script {
    /// The deterministic CREATE2 factory that `new X{salt: s}()` uses inside forge scripts.
    address internal constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    uint160 internal constant FLAG_MASK = Hooks.ALL_HOOK_MASK; // low 14 bits
    uint256 internal constant MAX_LOOP = 300_000;

    function run() external {
        address poolManager = vm.envAddress("POOL_MANAGER");
        address treasury = vm.envAddress("TREASURY");
        address owner = vm.envOr("OWNER", msg.sender);
        address setter = vm.envOr("SETTER", owner);
        address existingSet = vm.envOr("AUTOMATED_SET", address(0));
        require(CREATE2_DEPLOYER.code.length > 0, "CREATE2 deployer 0x4e59b4...956C is not on this chain");

        // The flags the hook declares in getHookPermissions(); the constructor reverts if the address disagrees.
        uint160 flags = uint160(Hooks.AFTER_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG | Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG);

        vm.startBroadcast();
        AutomatedSet set = existingSet == address(0) ? new AutomatedSet(owner, setter) : AutomatedSet(existingSet);
        vm.stopBroadcast();

        bytes memory initcode = abi.encodePacked(type(CrowdingHook).creationCode, abi.encode(IPoolManager(poolManager), set, owner, treasury));
        (address predicted, bytes32 salt) = mine(flags, initcode);

        vm.startBroadcast();
        CrowdingHook hook = new CrowdingHook{salt: salt}(IPoolManager(poolManager), set, owner, treasury);
        vm.stopBroadcast();
        require(address(hook) == predicted, "hook landed at an unexpected address");
        require(flagsOf(hook.getHookPermissions()) == flags, "hook permissions differ from the flags mined");

        console2.log("AutomatedSet :", address(set));
        console2.log("CrowdingHook :", address(hook));
        console2.log("salt         :", vm.toString(salt));
        console2.log("owner        :", owner);
        console2.log("setter       :", setter);
        console2.log("treasury     :", treasury);
        console2.log("pool manager :", poolManager);
    }

    /// Bits of the hook address that encode the permissions, derived from the contract itself.
    function flagsOf(Hooks.Permissions memory p) internal pure returns (uint160 f) {
        if (p.beforeInitialize) f |= Hooks.BEFORE_INITIALIZE_FLAG;
        if (p.afterInitialize) f |= Hooks.AFTER_INITIALIZE_FLAG;
        if (p.beforeAddLiquidity) f |= Hooks.BEFORE_ADD_LIQUIDITY_FLAG;
        if (p.afterAddLiquidity) f |= Hooks.AFTER_ADD_LIQUIDITY_FLAG;
        if (p.beforeRemoveLiquidity) f |= Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG;
        if (p.afterRemoveLiquidity) f |= Hooks.AFTER_REMOVE_LIQUIDITY_FLAG;
        if (p.beforeSwap) f |= Hooks.BEFORE_SWAP_FLAG;
        if (p.afterSwap) f |= Hooks.AFTER_SWAP_FLAG;
        if (p.beforeDonate) f |= Hooks.BEFORE_DONATE_FLAG;
        if (p.afterDonate) f |= Hooks.AFTER_DONATE_FLAG;
        if (p.beforeSwapReturnDelta) f |= Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG;
        if (p.afterSwapReturnDelta) f |= Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG;
        if (p.afterAddLiquidityReturnDelta) f |= Hooks.AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG;
        if (p.afterRemoveLiquidityReturnDelta) f |= Hooks.AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG;
    }

    /// Finds a salt such that the CREATE2 address has exactly `flags` in its low 14 bits.
    function mine(uint160 flags, bytes memory initcode) internal pure returns (address addr, bytes32 salt) {
        bytes32 initHash = keccak256(initcode);
        for (uint256 i = 0; i < MAX_LOOP; i++) {
            salt = bytes32(i);
            addr = address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), CREATE2_DEPLOYER, salt, initHash)))));
            if (uint160(addr) & FLAG_MASK == flags) return (addr, salt);
        }
        revert("no salt found; raise MAX_LOOP");
    }
}
