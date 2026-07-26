from decimal import Decimal

import pytest

from basis_hawk.models import Exchange, InstrumentPair
from basis_hawk.sizing import (
    OrderSizingError,
    protective_limit_price,
    size_paired_order,
)


def _pair(**updates: Decimal) -> InstrumentPair:
    values = {
        "exchange": Exchange.OKX,
        "base_asset": "ORDER",
        "spot_symbol": "ORDER-USDT",
        "perp_symbol": "ORDER-USDT-SWAP",
        "spot_price_increment": Decimal("0.00001"),
        "spot_quantity_increment": Decimal("0.1"),
        "spot_min_quantity": Decimal("1"),
        "spot_min_notional": Decimal("5"),
        "perp_price_increment": Decimal("0.0001"),
        "perp_quantity_increment": Decimal("1"),
        "perp_min_quantity": Decimal("1"),
        "perp_min_notional": Decimal("5"),
        "perp_contract_size": Decimal("10"),
    }
    values.update(updates)
    return InstrumentPair(**values)


def test_paired_order_size_uses_a_common_base_quantity_grid() -> None:
    result = size_paired_order(
        _pair(),
        requested_notional=Decimal("101"),
        spot_price=Decimal("0.51"),
        perp_price=Decimal("0.52"),
    )

    assert result.common_base_increment == Decimal("10")
    assert result.base_quantity == Decimal("190")
    assert result.spot_quantity == Decimal("190")
    assert result.perp_quantity == Decimal("19")
    assert result.spot_notional == Decimal("96.90")
    assert result.perp_notional == Decimal("98.80")


def test_paired_order_size_handles_fractional_contract_grids() -> None:
    result = size_paired_order(
        _pair(
            spot_quantity_increment=Decimal("0.03"),
            spot_min_quantity=Decimal("0"),
            spot_min_notional=Decimal("0"),
            perp_quantity_increment=Decimal("0.2"),
            perp_min_quantity=Decimal("0"),
            perp_min_notional=Decimal("0"),
            perp_contract_size=Decimal("0.1"),
        ),
        requested_notional=Decimal("1"),
        spot_price=Decimal("1"),
        perp_price=Decimal("1"),
    )

    assert result.common_base_increment == Decimal("0.06")
    assert result.base_quantity == Decimal("0.96")
    assert result.perp_quantity == Decimal("9.6")


def test_paired_order_size_rejects_incomplete_or_below_minimum_orders() -> None:
    with pytest.raises(OrderSizingError, match="incomplete"):
        size_paired_order(
            InstrumentPair(
                exchange=Exchange.BINANCE,
                base_asset="ORDER",
                spot_symbol="ORDERUSDT",
                perp_symbol="ORDERUSDT",
            ),
            requested_notional=Decimal("10"),
            spot_price=Decimal("1"),
            perp_price=Decimal("1"),
        )

    with pytest.raises(OrderSizingError, match="spot notional"):
        size_paired_order(
            _pair(
                spot_quantity_increment=Decimal("1"),
                spot_min_quantity=Decimal("0"),
                perp_quantity_increment=Decimal("1"),
                perp_min_quantity=Decimal("0"),
                perp_contract_size=Decimal("1"),
            ),
            requested_notional=Decimal("4"),
            spot_price=Decimal("1"),
            perp_price=Decimal("1"),
        )


def test_protective_prices_stay_inside_the_slippage_boundary() -> None:
    buy = protective_limit_price(
        reference_price=Decimal("100"),
        maximum_slippage=Decimal("0.001"),
        side="buy",
        price_increment=Decimal("0.03"),
    )
    sell = protective_limit_price(
        reference_price=Decimal("100"),
        maximum_slippage=Decimal("0.001"),
        side="sell",
        price_increment=Decimal("0.03"),
    )

    assert buy == Decimal("100.08")
    assert buy <= Decimal("100.1")
    assert sell == Decimal("99.90")
    assert sell >= Decimal("99.9")
