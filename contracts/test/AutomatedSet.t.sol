// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {AutomatedSet} from "../src/AutomatedSet.sol";

contract AutomatedSetTest is Test {
    AutomatedSet set;
    address owner = makeAddr("owner");
    address setter = makeAddr("setter");
    address a = makeAddr("a");
    address b = makeAddr("b");

    function setUp() public {
        set = new AutomatedSet(owner, setter);
    }

    function _arr(address x) internal pure returns (address[] memory r) {
        r = new address[](1);
        r[0] = x;
    }

    function test_addAndRemove() public {
        address[] memory both = new address[](2);
        both[0] = a;
        both[1] = b;
        vm.prank(setter);
        set.add(both);
        assertTrue(set.isAutomated(a));
        assertTrue(set.isAutomated(b));
        assertEq(set.size(), 2);
        assertEq(set.labelledAt(a), uint64(block.timestamp));

        vm.prank(setter);
        set.add(_arr(a)); // refresh, size unchanged
        assertEq(set.size(), 2);

        vm.prank(setter);
        set.remove(_arr(a));
        assertFalse(set.isAutomated(a));
        assertEq(set.size(), 1);

        vm.prank(setter);
        set.remove(_arr(a)); // not in the set: silently skipped
        assertEq(set.size(), 1);
    }

    function test_onlySetterCanWrite() public {
        vm.expectRevert(AutomatedSet.NotSetter.selector);
        set.add(_arr(a));
        vm.prank(owner);
        vm.expectRevert(AutomatedSet.NotSetter.selector);
        set.remove(_arr(a));
    }

    function test_pause() public {
        vm.prank(owner);
        set.setPaused(true);
        vm.prank(setter);
        vm.expectRevert(AutomatedSet.Paused.selector);
        set.add(_arr(a));
        vm.prank(owner);
        set.setPaused(false);
        vm.prank(setter);
        set.add(_arr(a));
        assertTrue(set.isAutomated(a));
    }

    function test_setterChange() public {
        vm.prank(owner);
        set.setSetter(b);
        vm.prank(setter);
        vm.expectRevert(AutomatedSet.NotSetter.selector);
        set.add(_arr(a));
        vm.prank(b);
        set.add(_arr(a));
        assertTrue(set.isAutomated(a));
        vm.prank(owner);
        vm.expectRevert(AutomatedSet.ZeroAddress.selector);
        set.setSetter(address(0));
    }

    function test_twoStepOwnership() public {
        vm.prank(owner);
        set.transferOwnership(a);
        assertEq(set.owner(), owner);
        vm.expectRevert(AutomatedSet.NotPendingOwner.selector);
        set.acceptOwnership();
        vm.prank(a);
        set.acceptOwnership();
        assertEq(set.owner(), a);
        assertEq(set.pendingOwner(), address(0));
        vm.prank(owner);
        vm.expectRevert(AutomatedSet.NotOwner.selector);
        set.setPaused(true);
    }
}
