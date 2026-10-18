// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "v4-core/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/interfaces/IPoolManager.sol";
import {Hooks} from "v4-core/libraries/Hooks.sol";
import {LPFeeLibrary} from "v4-core/libraries/LPFeeLibrary.sol";
import {PoolKey} from "v4-core/types/PoolKey.sol";
import {PoolId} from "v4-core/types/PoolId.sol";
import {SwapParams, ModifyLiquidityParams} from "v4-core/types/PoolOperation.sol";
import {BalanceDelta} from "v4-core/types/BalanceDelta.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "v4-core/types/BeforeSwapDelta.sol";
import {Currency} from "v4-core/types/Currency.sol";
import {AutomatedSet} from "./AutomatedSet.sol";

/// @title CrowdingHook
/// @notice A Uniswap v4 hook that charges flow joining a forming herd of automated wallets a surcharge, pays flow
///         leaning against the herd a rebate, and leaves everyone else on the pool's base fee.
///
///         Who is "automated" comes from the public `AutomatedSet`. The hook keeps, per pool, a ring of the last
///         RING flagged swaps (block, wallet, side) and counts distinct flagged wallets per side inside a window
///         of W blocks. A herd exists on a side when that side has at least H flagged wallets and the other side has
///         at most `oppositeShareBps` of that. The wallet is `tx.origin`, which is what the off-chain labelling
///         keys on (the `sender` a hook receives is the router).
///
///         Fees are in Uniswap pips (1e6 = 100%, so 30 bp = 3000). The surcharge and rebate are set per swap
///         through the dynamic-fee override, so there is no pool-state churn. Tethera's share is a fixed fraction
///         of the surcharge only, never of the base fee, taken on the unspecified side of the swap after it
///         executes (the pattern v4 ships a reference test for) and sent straight to `treasury`.
contract CrowdingHook is IHooks {
    using LPFeeLibrary for uint24;

    /// @dev Tethera's share of the surcharge, in basis points of the surcharge. Cannot exceed MAX in code.
    uint16 public constant TETHERA_SHARE_BPS = 2000;
    uint16 public constant MAX_TETHERA_SHARE_BPS = 2000;

    uint8 public constant RING = 32; // flagged swaps remembered per pool; also the ceiling on H
    uint24 public constant MAX_BASE_FEE = 100_000; // 10%
    uint24 public constant MAX_SURCHARGE = 10_000; // 100 bp
    uint24 public constant FEE_FLOOR = 100; // 1 bp
    uint32 public constant MAX_WINDOW_BLOCKS = 10_000;
    uint16 public constant MAX_OPPOSITE_SHARE_BPS = 5_000;

    struct PoolParams {
        uint24 baseFee; // pips
        uint24 surcharge; // pips added for herd-side flow
        uint24 rebate; // pips removed for leaning flow
        uint32 windowBlocks; // W
        uint8 herdThreshold; // H
        uint16 oppositeShareBps; // other side at most this share of the herd side
        bool initialized;
    }

    struct Entry {
        address wallet;
        uint64 blockNumber;
        bool zeroForOne;
    }

    struct Pending {
        uint8 kind; // 0 calm, 1 herd side, 2 leaning side
        uint24 lpFee;
        uint16 herdCount;
    }

    event CrowdFee(
        PoolId indexed poolId, address indexed wallet, bool zeroForOne, uint8 kind, uint24 lpFee, uint256 tetheraCut, uint16 herdCount
    );
    event PoolParamsSet(PoolId indexed poolId, PoolParams params);
    event TreasuryChanged(address indexed treasury);
    event OwnershipTransferred(address indexed from, address indexed to);

    error NotPoolManager();
    error NotOwner();
    error NotPoolCreator();
    error NotDynamicFee();
    error ParamOutOfBounds();
    error HookNotImplemented();
    error ZeroAddress();

    IPoolManager public immutable manager;
    AutomatedSet public immutable automatedSet;
    address public owner;
    address public treasury;

    mapping(PoolId => PoolParams) public params;
    mapping(PoolId => address) public creator;
    mapping(PoolId => Entry[RING]) internal ring;
    mapping(PoolId => uint8) internal head;
    Pending internal pending;

    modifier onlyPoolManager() {
        if (msg.sender != address(manager)) revert NotPoolManager();
        _;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(IPoolManager manager_, AutomatedSet set_, address owner_, address treasury_) {
        if (owner_ == address(0) || treasury_ == address(0)) revert ZeroAddress();
        assert(TETHERA_SHARE_BPS <= MAX_TETHERA_SHARE_BPS);
        manager = manager_;
        automatedSet = set_;
        owner = owner_;
        treasury = treasury_;
        Hooks.validateHookPermissions(this, getHookPermissions());
    }

    function getHookPermissions() public pure returns (Hooks.Permissions memory) {
        return Hooks.Permissions({
            beforeInitialize: false,
            afterInitialize: true,
            beforeAddLiquidity: false,
            afterAddLiquidity: false,
            beforeRemoveLiquidity: false,
            afterRemoveLiquidity: false,
            beforeSwap: true,
            afterSwap: true,
            beforeDonate: false,
            afterDonate: false,
            beforeSwapReturnDelta: false,
            afterSwapReturnDelta: true,
            afterAddLiquidityReturnDelta: false,
            afterRemoveLiquidityReturnDelta: false
        });
    }

    // ----------------------------------------------------------------------------------------------- admin

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        treasury = treasury_;
        emit TreasuryChanged(treasury_);
    }

    function transferOwnership(address to) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, to);
        owner = to;
    }

    /// @notice The pool's creator (whoever initialised it) tunes the schedule within the hook's bounds.
    function setPoolParams(PoolKey calldata key, PoolParams memory p) external {
        PoolId id = key.toId();
        if (msg.sender != creator[id]) revert NotPoolCreator();
        _setParams(id, p);
    }

    function _setParams(PoolId id, PoolParams memory p) internal {
        if (
            p.baseFee > MAX_BASE_FEE || p.surcharge > MAX_SURCHARGE || p.rebate > p.baseFee || p.windowBlocks == 0
                || p.windowBlocks > MAX_WINDOW_BLOCKS || p.herdThreshold == 0 || p.herdThreshold > RING
                || p.oppositeShareBps > MAX_OPPOSITE_SHARE_BPS
        ) revert ParamOutOfBounds();
        p.initialized = true;
        params[id] = p;
        emit PoolParamsSet(id, p);
    }

    function defaultParams() public pure returns (PoolParams memory) {
        return PoolParams({
            baseFee: 3000, surcharge: 2000, rebate: 1000, windowBlocks: 600, herdThreshold: 10, oppositeShareBps: 1500, initialized: true
        });
    }

    // ----------------------------------------------------------------------------------------------- views

    /// @notice Distinct flagged wallets per side inside the window, and which side (if any) is a herd.
    /// @return zeroForOneCount flagged wallets selling token0, oneForZeroCount flagged wallets buying token0,
    ///         herdSide 0 none, 1 the zeroForOne side, 2 the oneForZero side.
    function herdState(PoolId id) public view returns (uint16 zeroForOneCount, uint16 oneForZeroCount, uint8 herdSide) {
        PoolParams memory p = params[id];
        (zeroForOneCount, oneForZeroCount) = _count(id, p.windowBlocks);
        herdSide = _herdSide(zeroForOneCount, oneForZeroCount, p);
    }

    /// @notice The LP fee a swap in this direction would pay right now, and why (0 calm, 1 herd side, 2 leaning).
    function feeFor(PoolId id, bool zeroForOne) external view returns (uint24 lpFee, uint8 kind, uint16 herdCount) {
        PoolParams memory p = params[id];
        (uint16 c0, uint16 c1, uint8 side) = herdState(id);
        (lpFee, kind) = _schedule(p, side, zeroForOne);
        herdCount = side == 1 ? c0 : side == 2 ? c1 : 0;
    }

    /// @notice Pure decision rule, exposed for tests and for off-chain replication.
    function classify(uint16 countSame, uint16 countOpp, uint8 herdThreshold, uint16 oppositeShareBps) public pure returns (bool herd) {
        return countSame >= herdThreshold && uint256(countOpp) * 10_000 <= uint256(oppositeShareBps) * countSame;
    }

    // ----------------------------------------------------------------------------------------------- hooks

    function afterInitialize(address sender, PoolKey calldata key, uint160, int24) external onlyPoolManager returns (bytes4) {
        if (!key.fee.isDynamicFee()) revert NotDynamicFee();
        PoolId id = key.toId();
        creator[id] = sender;
        _setParams(id, defaultParams());
        return IHooks.afterInitialize.selector;
    }

    function beforeSwap(address, PoolKey calldata key, SwapParams calldata swapParams, bytes calldata)
        external
        onlyPoolManager
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        PoolId id = key.toId();
        PoolParams memory p = params[id];
        address wallet = tx.origin;
        bool flagged = automatedSet.labelledAt(wallet) != 0;
        if (flagged) _record(id, wallet, swapParams.zeroForOne);
        (uint16 c0, uint16 c1) = _count(id, p.windowBlocks);
        uint8 side = _herdSide(c0, c1, p);
        (uint24 lpFee, uint8 kind) = _schedule(p, side, swapParams.zeroForOne);
        pending = Pending({kind: kind, lpFee: lpFee, herdCount: side == 1 ? c0 : side == 2 ? c1 : 0});
        return (IHooks.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, lpFee | LPFeeLibrary.OVERRIDE_FEE_FLAG);
    }

    function afterSwap(address, PoolKey calldata key, SwapParams calldata swapParams, BalanceDelta delta, bytes calldata)
        external
        onlyPoolManager
        returns (bytes4, int128)
    {
        Pending memory pd = pending;
        delete pending;
        PoolId id = key.toId();
        uint256 cut;
        if (pd.kind == 1) {
            // Tethera's share: TETHERA_SHARE_BPS of the surcharge, measured on the unspecified side of the swap
            // (output for exact-input, input for exact-output), the side a hook can take from after the swap.
            bool specifiedIs0 = (swapParams.amountSpecified < 0) == swapParams.zeroForOne;
            (Currency feeCurrency, int128 amt) = specifiedIs0 ? (key.currency1, delta.amount1()) : (key.currency0, delta.amount0());
            if (amt < 0) amt = -amt;
            cut = _tetheraCut(uint256(uint128(amt)), params[id].surcharge);
            if (cut > 0) manager.take(feeCurrency, treasury, cut);
        }
        if (pd.kind != 0) emit CrowdFee(id, tx.origin, swapParams.zeroForOne, pd.kind, pd.lpFee, cut, pd.herdCount);
        return (IHooks.afterSwap.selector, int128(uint128(cut)));
    }

    /// @notice Tethera's cut of one swap: amount × surcharge × share. Pure, so the arithmetic can be tested alone.
    function _tetheraCut(uint256 amount, uint24 surcharge) internal pure returns (uint256) {
        return amount * surcharge * TETHERA_SHARE_BPS / (1e6 * 10_000);
    }

    function tetheraCut(uint256 amount, uint24 surcharge) external pure returns (uint256) {
        return _tetheraCut(amount, surcharge);
    }

    // ----------------------------------------------------------------------------------------------- internals

    function _schedule(PoolParams memory p, uint8 herdSide, bool zeroForOne) internal pure returns (uint24 lpFee, uint8 kind) {
        if (herdSide == 0) return (p.baseFee, 0);
        bool sameSide = (herdSide == 1) == zeroForOne;
        if (sameSide) {
            // LPs receive base plus the part of the surcharge Tethera does not take
            uint256 lpSurcharge = uint256(p.surcharge) * (10_000 - TETHERA_SHARE_BPS) / 10_000;
            return (uint24(p.baseFee + lpSurcharge), 1);
        }
        uint24 rebated = p.baseFee > p.rebate ? p.baseFee - p.rebate : 0;
        return (rebated < FEE_FLOOR ? FEE_FLOOR : rebated, 2);
    }

    function _herdSide(uint16 c0, uint16 c1, PoolParams memory p) internal pure returns (uint8) {
        if (classify(c0, c1, p.herdThreshold, p.oppositeShareBps)) return 1;
        if (classify(c1, c0, p.herdThreshold, p.oppositeShareBps)) return 2;
        return 0;
    }

    /// @dev Remember a flagged swap. A wallet already in the window on either side has its entry refreshed, so the
    ///      ring holds up to RING distinct wallets rather than RING swaps.
    function _record(PoolId id, address wallet, bool zeroForOne) internal {
        Entry[RING] storage r = ring[id];
        for (uint256 i = 0; i < RING; ++i) {
            if (r[i].wallet == wallet) {
                r[i].blockNumber = uint64(block.number);
                r[i].zeroForOne = zeroForOne;
                return;
            }
        }
        uint8 h = head[id];
        r[h] = Entry({wallet: wallet, blockNumber: uint64(block.number), zeroForOne: zeroForOne});
        head[id] = uint8((h + 1) % RING);
    }

    function _count(PoolId id, uint32 windowBlocks) internal view returns (uint16 c0, uint16 c1) {
        Entry[RING] storage r = ring[id];
        uint256 cutoff = block.number > windowBlocks ? block.number - windowBlocks : 0;
        for (uint256 i = 0; i < RING; ++i) {
            Entry memory e = r[i];
            if (e.wallet == address(0) || e.blockNumber < cutoff) continue;
            if (e.zeroForOne) ++c0;
            else ++c1;
        }
    }

    // ----------------------------------------------------------------------------------------------- unused hooks

    function beforeInitialize(address, PoolKey calldata, uint160) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function beforeAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function afterAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, BalanceDelta, BalanceDelta, bytes calldata)
        external
        pure
        returns (bytes4, BalanceDelta)
    {
        revert HookNotImplemented();
    }

    function beforeRemoveLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function afterRemoveLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, BalanceDelta, BalanceDelta, bytes calldata)
        external
        pure
        returns (bytes4, BalanceDelta)
    {
        revert HookNotImplemented();
    }

    function beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function afterDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }
}
