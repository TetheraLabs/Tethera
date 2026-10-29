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
    # added 28 Oct 2026: official tokens with at least 5,000 USDG-pool swaps in 14 days and daily history from 2016 or earlier
    "MSTR":  "0xec262a75e413fAfD0dF80480274532C79D42da09",
    "GLD":   "0xC9a981FEE1F9DEc688bb123ccDeCc63D0deBFC4e",
    "AMC":   "0x05a3d1Cd21d0C88145E82600E62e7E496e0F222B",
    "GME":   "0x1b0E319c6A659F002271B69dB8A7df2F911c153E",
    "MU":    "0xfF080c8ce2E5feadaCa0Da81314Ae59D232d4afD",
    "INTC":  "0xc72b96e0E48ecd4DC75E1e45396e26300BC39681",
    "USO":   "0xa30FA36Db767ad9eD3f7a60fC79526fB4d56D344",
    "BABA":  "0xad25Ac6C84D497db898fa1E8387bf6Af3532a1c4",
    "TTWO":  "0x5e81213613b6B86EaB4c6c50d718d34359459786",
    "LLY":   "0x8005d266423c7ea827372c9c864491e5786600ea",
    "COST":  "0x4EA005168D7F09a7A0Ba9D1DEf21a479950E44C2",
    "NFLX":  "0xE0444EF8BF4eD74f74FD73686e2ddF4C1c5591E8",
    "IBM":   "0x980dcf6766FA79f5Cf0c4AAdb3ab477ff15a9619",
}
