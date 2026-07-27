from __future__ import annotations

from decimal import Decimal

from basis_hawk.models import MarketQuote, Opportunity


def market_quote_from_opportunity(
    opportunity: Opportunity,
) -> MarketQuote:
    return MarketQuote(
        exchange=opportunity.exchange,
        base_asset=opportunity.base_asset,
        observed_at=opportunity.observed_at,
        spot_bid=opportunity.spot_bid,
        spot_bid_qty=Decimal("0"),
        spot_ask=opportunity.spot_ask,
        spot_ask_qty=(
            opportunity.spot_ask_notional / opportunity.spot_ask
            if opportunity.spot_ask > 0
            else Decimal("0")
        ),
        perp_bid=opportunity.perp_bid,
        perp_bid_qty=(
            opportunity.perp_bid_notional / opportunity.perp_bid
            if opportunity.perp_bid > 0
            else Decimal("0")
        ),
        perp_ask=opportunity.perp_ask,
        perp_ask_qty=Decimal("0"),
        spot_quote_volume_24h=opportunity.spot_quote_volume_24h,
        perp_quote_volume_24h=opportunity.perp_quote_volume_24h,
    )


def opportunity_with_executable_quote(
    opportunity: Opportunity,
    quote: MarketQuote,
) -> Opportunity:
    spot_ask_notional = quote.spot_ask * quote.spot_ask_qty
    perp_bid_notional = quote.perp_bid * quote.perp_bid_qty
    return opportunity.model_copy(
        update={
            "observed_at": quote.observed_at,
            "spot_bid": quote.spot_bid,
            "spot_ask": quote.spot_ask,
            "perp_bid": quote.perp_bid,
            "perp_ask": quote.perp_ask,
            "executable_basis": (
                quote.perp_bid / quote.spot_ask - Decimal("1")
            ),
            "top_book_notional": min(
                spot_ask_notional,
                perp_bid_notional,
            ),
            "close_top_book_notional": min(
                quote.spot_bid * quote.spot_bid_qty,
                quote.perp_ask * quote.perp_ask_qty,
            ),
            "spot_ask_notional": spot_ask_notional,
            "perp_bid_notional": perp_bid_notional,
        }
    )
