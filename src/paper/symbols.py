"""Ticker spelling: the warehouse (Yahoo) writes share classes as BF-B, Alpaca as BF.B."""


def to_alpaca(symbol: str) -> str:
    return symbol.replace("-", ".")


def from_alpaca(symbol: str) -> str:
    return symbol.replace(".", "-")
