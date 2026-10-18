// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title AutomatedSet
/// @notice The public, on-chain set of wallets Tethera has labelled automated. One storage slot per wallet
///         holding the block timestamp of the latest labelling; zero means not in the set.
///         A `setter` (the labelling job's hot wallet) adds and removes; the `owner` (a multisig or hardware
///         wallet) appoints the setter and can pause it. Anyone can read it; the crowding hook is the first consumer.
contract AutomatedSet {
    event Added(address indexed wallet, uint64 labelledAt);
    event Removed(address indexed wallet);
    event SetterChanged(address indexed setter);
    event SetterPaused(bool paused);
    event OwnershipTransferStarted(address indexed from, address indexed to);
    event OwnershipTransferred(address indexed from, address indexed to);

    error NotOwner();
    error NotSetter();
    error NotPendingOwner();
    error Paused();
    error ZeroAddress();

    address public owner;
    address public pendingOwner;
    address public setter;
    bool public paused;
    uint32 public size;

    mapping(address => uint64) public labelledAt;

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlySetter() {
        if (msg.sender != setter) revert NotSetter();
        if (paused) revert Paused();
        _;
    }

    constructor(address owner_, address setter_) {
        if (owner_ == address(0) || setter_ == address(0)) revert ZeroAddress();
        owner = owner_;
        setter = setter_;
        emit OwnershipTransferred(address(0), owner_);
        emit SetterChanged(setter_);
    }

    /// @notice True when the wallet is currently labelled automated.
    function isAutomated(address wallet) external view returns (bool) {
        return labelledAt[wallet] != 0;
    }

    /// @notice Add wallets (or refresh their timestamp). One SSTORE each.
    function add(address[] calldata wallets) external onlySetter {
        uint64 now_ = uint64(block.timestamp);
        uint32 added;
        for (uint256 i = 0; i < wallets.length; ++i) {
            address w = wallets[i];
            if (labelledAt[w] == 0) ++added;
            labelledAt[w] = now_;
            emit Added(w, now_);
        }
        size += added;
    }

    /// @notice Remove wallets that are in the set; wallets not in the set are skipped silently.
    function remove(address[] calldata wallets) external onlySetter {
        uint32 removed;
        for (uint256 i = 0; i < wallets.length; ++i) {
            address w = wallets[i];
            if (labelledAt[w] == 0) continue;
            delete labelledAt[w];
            ++removed;
            emit Removed(w);
        }
        size -= removed;
    }

    function setSetter(address setter_) external onlyOwner {
        if (setter_ == address(0)) revert ZeroAddress();
        setter = setter_;
        emit SetterChanged(setter_);
    }

    function setPaused(bool paused_) external onlyOwner {
        paused = paused_;
        emit SetterPaused(paused_);
    }

    /// @notice Two-step transfer: the new owner must accept, so a typo cannot orphan the set.
    function transferOwnership(address to) external onlyOwner {
        pendingOwner = to;
        emit OwnershipTransferStarted(owner, to);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        emit OwnershipTransferred(owner, msg.sender);
        owner = msg.sender;
        pendingOwner = address(0);
    }
}
