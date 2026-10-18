// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, Vm} from "forge-std/Test.sol";
import {Deployers} from "v4-core-test/utils/Deployers.sol";
import {IHooks} from "v4-core/interfaces/IHooks.sol";
import {Hooks} from "v4-core/libraries/Hooks.sol";
import {LPFeeLibrary} from "v4-core/libraries/LPFeeLibrary.sol";
import {PoolKey} from "v4-core/types/PoolKey.sol";
import {PoolId} from "v4-core/types/PoolId.sol";
import {Currency} from "v4-core/types/Currency.sol";
import {BalanceDelta} from "v4-core/types/BalanceDelta.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {AutomatedSet} from "../src/AutomatedSet.sol";
import {CrowdingHook} from "../src/CrowdingHook.sol";

contract CrowdingHookTest is Test, Deployers {
    AutomatedSet set;
    CrowdingHook hook;
    PoolId id;
    address owner = makeAddr("owner");
    address setter = makeAddr("setter");
    address treasury = makeAddr("treasury");
    bytes32 constant CROWD_FEE_SIG = keccak256("CrowdFee(bytes32,address,bool,uint8,uint24,uint256,uint16)");

    function setUp() public {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();
        set = new AutomatedSet(owner, setter);
        address hookAddr = address(
            uint160(Hooks.AFTER_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG | Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG)
        );
        deployCodeTo("CrowdingHook.sol:CrowdingHook", abi.encode(manager, set, owner, treasury), hookAddr);
        hook = CrowdingHook(hookAddr);
        (key,) = initPoolAndAddLiquidity(currency0, currency1, IHooks(hookAddr), LPFeeLibrary.DYNAMIC_FEE_FLAG, SQRT_PRICE_1_1);
        id = key.toId();
        seedMoreLiquidity(key, 1_000 ether, 1_000 ether);
    }

    // -------------------------------------------------------------------------------------------- helpers

    function _wallet(uint256 i) internal returns (address) {
        return makeAddr(string(abi.encodePacked("bot", i)));
    }

    function _label(address w) internal {
        address[] memory arr = new address[](1);
        arr[0] = w;
        vm.prank(setter);
        set.add(arr);
    }

    /// swap as `origin`: msg.sender to the router stays this contract (it holds the tokens), tx.origin is the wallet
    function _swapAs(address origin, bool zeroForOne, int256 amount) internal returns (BalanceDelta) {
        vm.prank(address(this), origin);
        return swap(key, zeroForOne, amount, ZERO_BYTES);
    }

    function _formHerd(uint256 n, bool zeroForOne) internal {
        for (uint256 i = 0; i < n; ++i) {
            address w = _wallet(i);
            _label(w);
            _swapAs(w, zeroForOne, -1e15);
        }
    }

    function _crowdFeeLogs(Vm.Log[] memory logs) internal pure returns (uint256 n, uint8 kind, uint24 lpFee, uint256 cut, uint16 herdCount) {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics[0] == CROWD_FEE_SIG) {
                ++n;
                (, kind, lpFee, cut, herdCount) = abi.decode(logs[i].data, (bool, uint8, uint24, uint256, uint16));
            }
        }
    }

    // -------------------------------------------------------------------------------------------- init and params

    function test_initSetsDefaultsAndCreator() public view {
        (uint24 base, uint24 sur, uint24 reb, uint32 w, uint8 h, uint16 opp, bool init) = hook.params(id);
        assertEq(base, 3000);
        assertEq(sur, 2000);
        assertEq(reb, 1000);
        assertEq(w, 600);
        assertEq(h, 10);
        assertEq(opp, 1500);
        assertTrue(init);
        assertEq(hook.creator(id), address(this));
    }

    function test_rejectsStaticFeePool() public {
        vm.expectRevert();
        initPool(currency0, currency1, IHooks(address(hook)), 3000, SQRT_PRICE_1_1);
    }

    function test_paramBounds() public {
        CrowdingHook.PoolParams memory p = hook.defaultParams();
        p.surcharge = 10_001;
        vm.expectRevert(CrowdingHook.ParamOutOfBounds.selector);
        hook.setPoolParams(key, p);
        p = hook.defaultParams();
        p.rebate = p.baseFee + 1;
        vm.expectRevert(CrowdingHook.ParamOutOfBounds.selector);
        hook.setPoolParams(key, p);
        p = hook.defaultParams();
        p.herdThreshold = 33;
        vm.expectRevert(CrowdingHook.ParamOutOfBounds.selector);
        hook.setPoolParams(key, p);
        p = hook.defaultParams();
        p.windowBlocks = 0;
        vm.expectRevert(CrowdingHook.ParamOutOfBounds.selector);
        hook.setPoolParams(key, p);
        p = hook.defaultParams();
        p.baseFee = 100_001;
        vm.expectRevert(CrowdingHook.ParamOutOfBounds.selector);
        hook.setPoolParams(key, p);
        p = hook.defaultParams();
        p.surcharge = 5000;
        p.herdThreshold = 3;
        hook.setPoolParams(key, p);
        (, uint24 sur,,, uint8 h,,) = hook.params(id);
        assertEq(sur, 5000);
        assertEq(h, 3);
    }

    function test_onlyCreatorSetsParams() public {
        CrowdingHook.PoolParams memory p = hook.defaultParams();
        vm.prank(makeAddr("stranger"));
        vm.expectRevert(CrowdingHook.NotPoolCreator.selector);
        hook.setPoolParams(key, p);
    }

    // -------------------------------------------------------------------------------------------- share maths

    function test_shareConstantCapped() public view {
        assertLe(hook.TETHERA_SHARE_BPS(), hook.MAX_TETHERA_SHARE_BPS());
        assertEq(hook.TETHERA_SHARE_BPS(), 2000);
    }

    function test_tetheraCutMaths() public view {
        // 1e18 at a 20 bp surcharge: the surcharge is 2e15, Tethera's fifth of it is 4e14
        assertEq(hook.tetheraCut(1e18, 2000), 4e14);
        assertEq(hook.tetheraCut(0, 2000), 0);
        assertEq(hook.tetheraCut(1e18, 0), 0);
    }

    function test_classifyRule() public view {
        assertTrue(hook.classify(10, 0, 10, 1500));
        assertTrue(hook.classify(10, 1, 10, 1500)); // 10% opposite
        assertFalse(hook.classify(10, 2, 10, 1500)); // 20% opposite
        assertFalse(hook.classify(9, 0, 10, 1500));
        assertTrue(hook.classify(20, 3, 10, 1500)); // exactly 15%
    }

    // -------------------------------------------------------------------------------------------- herd replay

    function test_calmUntilThreshold() public {
        _formHerd(9, true);
        (uint16 c0, uint16 c1, uint8 side) = hook.herdState(id);
        assertEq(c0, 9);
        assertEq(c1, 0);
        assertEq(side, 0);
        (uint24 fee, uint8 kind,) = hook.feeFor(id, true);
        assertEq(fee, 3000);
        assertEq(kind, 0);
    }

    function test_herdFormsAndSchedules() public {
        _formHerd(10, true);
        (uint16 c0,, uint8 side) = hook.herdState(id);
        assertEq(c0, 10);
        assertEq(side, 1);

        (uint24 feeSame, uint8 kindSame, uint16 count) = hook.feeFor(id, true);
        assertEq(feeSame, 3000 + 1600); // base + the LPs' four fifths of a 20 bp surcharge
        assertEq(kindSame, 1);
        assertEq(count, 10);

        (uint24 feeLean, uint8 kindLean,) = hook.feeFor(id, false);
        assertEq(feeLean, 2000); // base minus the 10 bp rebate
        assertEq(kindLean, 2);
    }

    function test_unflaggedFlowJoiningHerdPaysSurchargeAndTethera() public {
        _formHerd(10, true);
        address human = makeAddr("human");
        uint256 before = MockERC20(Currency.unwrap(currency1)).balanceOf(treasury);
        vm.recordLogs();
        BalanceDelta d = _swapAs(human, true, -1e15);
        (uint256 n, uint8 kind, uint24 lpFee, uint256 cut, uint16 herdCount) = _crowdFeeLogs(vm.getRecordedLogs());
        assertEq(n, 1);
        assertEq(kind, 1);
        assertEq(lpFee, 4600);
        assertEq(herdCount, 10);
        uint256 got = MockERC20(Currency.unwrap(currency1)).balanceOf(treasury) - before;
        assertEq(got, cut);
        assertGt(cut, 0);
        // the cut is a fifth of 20 bp of the gross output: 4 bp of (user output + cut)
        uint256 userOut = uint256(int256(d.amount1()));
        uint256 gross = userOut + cut;
        assertApproxEqRel(cut, gross * 2000 * 2000 / 1e10, 1e15);
        // the herd still counts the ten flagged wallets, not the human
        (uint16 c0,,) = hook.herdState(id);
        assertEq(c0, 10);
    }

    function test_leaningFlowGetsRebateAndNoCut() public {
        _formHerd(10, true);
        address leaner = makeAddr("leaner");
        uint256 before = MockERC20(Currency.unwrap(currency0)).balanceOf(treasury);
        vm.recordLogs();
        _swapAs(leaner, false, -1e15);
        (uint256 n, uint8 kind, uint24 lpFee, uint256 cut,) = _crowdFeeLogs(vm.getRecordedLogs());
        assertEq(n, 1);
        assertEq(kind, 2);
        assertEq(lpFee, 2000);
        assertEq(cut, 0);
        assertEq(MockERC20(Currency.unwrap(currency0)).balanceOf(treasury), before);
    }

    function test_calmSwapEmitsNothing() public {
        vm.recordLogs();
        _swapAs(makeAddr("human"), true, -1e15);
        (uint256 n,,,,) = _crowdFeeLogs(vm.getRecordedLogs());
        assertEq(n, 0);
        assertEq(MockERC20(Currency.unwrap(currency1)).balanceOf(treasury), 0);
    }

    function test_twoSidedCrowdIsNotAHerd() public {
        _formHerd(10, true);
        address x = _wallet(100);
        address y = _wallet(101);
        _label(x);
        _label(y);
        _swapAs(x, false, -1e15);
        _swapAs(y, false, -1e15);
        (uint16 c0, uint16 c1, uint8 side) = hook.herdState(id);
        assertEq(c0, 10);
        assertEq(c1, 2); // 20% of the herd side, above the 15% rule
        assertEq(side, 0);
    }

    function test_herdExpiresWithWindow() public {
        _formHerd(10, true);
        vm.roll(block.number + 599);
        (,, uint8 side) = hook.herdState(id);
        assertEq(side, 1);
        vm.roll(block.number + 2);
        (uint16 c0,, uint8 side2) = hook.herdState(id);
        assertEq(c0, 0);
        assertEq(side2, 0);
    }

    function test_sameWalletCountsOnce() public {
        address w = _wallet(0);
        _label(w);
        for (uint256 i = 0; i < 12; ++i) _swapAs(w, true, -1e14);
        (uint16 c0,, uint8 side) = hook.herdState(id);
        assertEq(c0, 1);
        assertEq(side, 0);
    }

    function test_switchingSideMovesTheWallet() public {
        _formHerd(10, true);
        _swapAs(_wallet(0), false, -1e15);
        (uint16 c0, uint16 c1,) = hook.herdState(id);
        assertEq(c0, 9);
        assertEq(c1, 1);
    }

    function test_ringHoldsAtMostRingDistinctWallets() public {
        _formHerd(40, true);
        (uint16 c0,,) = hook.herdState(id);
        assertEq(c0, 32);
    }

    function test_rebateFloor() public {
        CrowdingHook.PoolParams memory p = hook.defaultParams();
        p.baseFee = 1000;
        p.rebate = 1000;
        hook.setPoolParams(key, p);
        _formHerd(10, true);
        (uint24 fee, uint8 kind,) = hook.feeFor(id, false);
        assertEq(fee, 100); // 1 bp floor
        assertEq(kind, 2);
    }

    function test_treasuryAndOwnership() public {
        vm.expectRevert(CrowdingHook.NotOwner.selector);
        hook.setTreasury(makeAddr("t2"));
        vm.prank(owner);
        hook.setTreasury(makeAddr("t2"));
        assertEq(hook.treasury(), makeAddr("t2"));
        vm.prank(owner);
        hook.transferOwnership(makeAddr("o2"));
        assertEq(hook.owner(), makeAddr("o2"));
    }
}
