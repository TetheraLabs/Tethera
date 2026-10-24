"""The instruments the desk trades: one row per name, with the official Robinhood stock token on the chain. Prices on
the chain are USDG per share. Private history files behind the chain feed are configured outside this package."""
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"          # the real USDG, 6 decimals
CHAIN_START = "2026-07-01"                                       # first month with enough on-chain fills to make bars

TOKENS = {
    "SPY":   "0x117cc2133c37B721F49dE2A7a74833232B3B4C0C",
    "QQQ":   "0xD5f3879160bc7c32ebb4dC785F8a4F505888de68",
    "NVDA":  "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
    "TSLA":  "0x322F0929c4625eD5bAd873c95208D54E1c003b2d",
    "AAPL":  "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
    "META":  "0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35",
    "GOOGL": "0x2e0847E8910a9732eB3fb1bb4b70a580ADAD4FE3",
    "MSFT":  "0xe93237C50D904957Cf27E7B1133b510C669c2e74",
    "AMZN":  "0x12f190a9F9d7D37a250758b26824B97CE941bF54",
    "AMD":   "0x86923f96303D656E4aa86D9d42D1e57ad2023fdC",
}
