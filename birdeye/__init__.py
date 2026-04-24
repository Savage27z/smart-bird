"""Birdeye integration package.

Contains the shared async HTTP client and the four intelligence layers built
on top of the Birdeye Data API:

    * ``client``        — low-level aiohttp wrapper with retries + logging.
    * ``new_listings``  — Layer 1 graduation predictor.
    * ``smart_money``   — Layer 2 alpha-wallet entry detector.
    * ``liquidity``     — Layer 3 liquidity stress monitor.
    * ``sentiment``     — Layer 4 social sentiment analyzer.
"""
