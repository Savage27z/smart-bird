"""Persistence layer (SQLite).

Holds all cross-run state: tracked tokens and their pipeline status, the
rolling window of liquidity snapshots, smart-money entries we've observed,
and a log of alerts we've already fired (for dedup).
"""
