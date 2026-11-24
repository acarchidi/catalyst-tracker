"""Which of a company's tickers is its main one (ADR 0070).

SEC lists every ticker a company has: its shares, and also warrants, units, rights, contingent
value rights and over-the-counter lines. The main ticker is the first on Nasdaq, NYSE, Cboe or
the Texas Stock Exchange without such a suffix; an over-the-counter ticker never wins while an
exchange-listed one exists. Ties keep SEC's order.
"""

from __future__ import annotations

from collections.abc import Sequence

EXCHANGES = ("NASDAQ", "NYSE", "CBOE", "TXSE", "TEXAS")
# Nasdaq's fifth letters for a warrant, unit, right, or other (a CVR, say); NYSE writes "-WT" and such.
_FIFTH = frozenset("WURZ")


def suffixed(ticker: str) -> bool:
    """Whether the ticker is a class, warrant, unit, right or other line, not the plain shares."""
    return "-" in ticker or "." in ticker or (len(ticker) == 5 and ticker[-1] in _FIFTH)


def listed(exchange: str | None) -> bool:
    """Whether the exchange is one of the US stock exchanges, not over the counter."""
    return exchange is not None and exchange.upper().replace(" ", "").startswith(EXCHANGES)


def main_ticker(tickers: Sequence[tuple[str, str | None]]) -> str:
    """Return the main ticker of (ticker, exchange) pairs, best first by the rule above."""
    if not tickers:
        raise ValueError("a company with no ticker has no main ticker")

    def rank(item: tuple[int, tuple[str, str | None]]) -> tuple[int, bool, int]:
        index, (ticker, exchange) = item
        over_the_counter = (exchange or "").upper() == "OTC"
        return (0 if listed(exchange) else 2 if over_the_counter else 1, suffixed(ticker), index)

    return min(enumerate(tickers), key=rank)[1][0]
