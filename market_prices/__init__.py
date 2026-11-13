"""Competitor price observations: a separate path that feeds the pricing module and never reaches Medusa.

Nothing here reads or writes products, the ledger or the sync payload. See CONTRACT.md for what it delivers.
"""
