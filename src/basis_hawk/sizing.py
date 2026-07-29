from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import gcd

from basis_hawk.models import InstrumentPair


class OrderSizingError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        minimum_notional: Decimal | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.minimum_notional = minimum_notional


@dataclass(frozen=True)
class PairedOrderSize:
    base_quantity: Decimal
    spot_quantity: Decimal
    perp_quantity: Decimal
    common_base_increment: Decimal
    spot_notional: Decimal
    perp_notional: Decimal


def protective_limit_price(
    *,
    reference_price: Decimal,
    maximum_slippage: Decimal,
    side: str,
    price_increment: Decimal,
) -> Decimal:
    if reference_price <= 0 or price_increment <= 0:
        raise OrderSizingError("reference price and increment must be positive")
    if maximum_slippage < 0 or maximum_slippage >= 1:
        raise OrderSizingError("maximum slippage must be between zero and one")
    if side == "buy":
        boundary = reference_price * (Decimal("1") + maximum_slippage)
        rounding = ROUND_FLOOR
    elif side == "sell":
        boundary = reference_price * (Decimal("1") - maximum_slippage)
        rounding = ROUND_CEILING
    else:
        raise OrderSizingError("order side must be buy or sell")
    increments = (boundary / price_increment).to_integral_value(rounding=rounding)
    result = increments * price_increment
    if result <= 0:
        raise OrderSizingError("protective limit price is not positive")
    return result


def compensation_quantity(
    *,
    requested_quantity: Decimal,
    quantity_increment: Decimal,
    side: str,
) -> Decimal:
    """Return a tradable protection size that leaves only spot-side dust.

    Buying back an excess leg rounds up; selling an excess leg rounds down.
    This keeps the remaining perpetual exposure no greater than the remaining
    spot inventory after either an opening or closing compensation.
    """
    if requested_quantity <= 0 or quantity_increment <= 0:
        raise OrderSizingError(
            "compensation quantity and increment must be positive"
        )
    # SQLite exposes NUMERIC exchange metadata through binary floats in tests.
    # The shortest decimal round-trip restores the exchange-declared grid
    # (for example 0.001 instead of 0.00100000000000000002).
    quantity_increment = Decimal(str(float(quantity_increment)))
    if side == "buy":
        rounding = ROUND_CEILING
    elif side == "sell":
        rounding = ROUND_FLOOR
    else:
        raise OrderSizingError("compensation side must be buy or sell")
    ratio = requested_quantity / quantity_increment
    nearest = ratio.to_integral_value()
    if abs(ratio - nearest) <= Decimal("1e-9"):
        ratio = nearest
    increments = ratio.to_integral_value(rounding=rounding)
    result = (increments * quantity_increment).quantize(
        quantity_increment.normalize()
    )
    if result <= 0:
        raise OrderSizingError(
            "compensation quantity is below the exchange increment"
        )
    return result


def size_paired_order(
    pair: InstrumentPair,
    *,
    requested_notional: Decimal,
    spot_price: Decimal,
    perp_price: Decimal,
    spot_base_fee_rate: Decimal = Decimal("0"),
) -> PairedOrderSize:
    if not pair.trading_rules_complete:
        raise OrderSizingError("instrument trading rules are incomplete")
    if requested_notional <= 0 or spot_price <= 0 or perp_price <= 0:
        raise OrderSizingError("notional and prices must be positive")
    if spot_base_fee_rate < 0 or spot_base_fee_rate >= 1:
        raise OrderSizingError("spot base fee rate must be between zero and one")

    common_increment = _decimal_lcm(
        pair.spot_quantity_increment,
        pair.perp_base_quantity_increment,
    )
    minimum_notional = minimum_paired_order_notional(
        pair,
        spot_price=spot_price,
        perp_price=perp_price,
        spot_base_fee_rate=spot_base_fee_rate,
    )
    if requested_notional < minimum_notional:
        raise OrderSizingError(
            "requested notional is below the minimum executable amount",
            code="notional_below_minimum",
            minimum_notional=minimum_notional,
        )
    target_base_quantity = requested_notional / spot_price
    increments = (target_base_quantity / common_increment).to_integral_value(
        rounding=ROUND_FLOOR
    )
    spot_quantity = increments * common_increment
    if spot_quantity <= 0:
        raise OrderSizingError("requested notional is below the common quantity step")

    expected_net_spot = spot_quantity * (
        Decimal("1") - spot_base_fee_rate
    )
    perp_base_increment = pair.perp_base_quantity_increment
    perp_increments = (
        expected_net_spot / perp_base_increment
    ).to_integral_value(rounding=ROUND_FLOOR)
    base_quantity = perp_increments * perp_base_increment
    perp_quantity = base_quantity / pair.perp_contract_size
    if spot_quantity < pair.spot_min_quantity:
        raise OrderSizingError("spot quantity is below the exchange minimum")
    if perp_quantity < pair.perp_min_quantity:
        raise OrderSizingError("perpetual quantity is below the exchange minimum")
    if not _is_multiple(spot_quantity, pair.spot_quantity_increment):
        raise OrderSizingError("spot quantity is not aligned to its increment")
    if not _is_multiple(perp_quantity, pair.perp_quantity_increment):
        raise OrderSizingError("perpetual quantity is not aligned to its increment")

    spot_notional = spot_quantity * spot_price
    perp_notional = base_quantity * perp_price
    if pair.spot_min_notional > 0 and spot_notional < pair.spot_min_notional:
        raise OrderSizingError("spot notional is below the exchange minimum")
    if pair.perp_min_notional > 0 and perp_notional < pair.perp_min_notional:
        raise OrderSizingError("perpetual notional is below the exchange minimum")
    return PairedOrderSize(
        base_quantity=base_quantity,
        spot_quantity=spot_quantity,
        perp_quantity=perp_quantity,
        common_base_increment=common_increment,
        spot_notional=spot_notional,
        perp_notional=perp_notional,
    )


def minimum_paired_order_notional(
    pair: InstrumentPair,
    *,
    spot_price: Decimal,
    perp_price: Decimal,
    spot_base_fee_rate: Decimal = Decimal("0"),
) -> Decimal:
    if not pair.trading_rules_complete:
        raise OrderSizingError("instrument trading rules are incomplete")
    if spot_price <= 0 or perp_price <= 0:
        raise OrderSizingError("prices must be positive")
    if spot_base_fee_rate < 0 or spot_base_fee_rate >= 1:
        raise OrderSizingError("spot base fee rate must be between zero and one")
    common_increment = _decimal_lcm(
        pair.spot_quantity_increment,
        pair.perp_base_quantity_increment,
    )
    required_spot_quantity = max(
        common_increment,
        pair.spot_min_quantity,
        (
            pair.spot_min_notional / spot_price
            if pair.spot_min_notional > 0
            else Decimal("0")
        ),
    )
    required_perp_base_quantity = max(
        pair.perp_base_quantity_increment,
        pair.perp_min_quantity * pair.perp_contract_size,
        (
            pair.perp_min_notional / perp_price
            if pair.perp_min_notional > 0
            else Decimal("0")
        ),
    )
    required_perp_increments = (
        required_perp_base_quantity / pair.perp_base_quantity_increment
    ).to_integral_value(rounding=ROUND_CEILING)
    required_perp_base_quantity = (
        required_perp_increments * pair.perp_base_quantity_increment
    )
    required_base_quantity = max(
        required_spot_quantity,
        required_perp_base_quantity
        / (Decimal("1") - spot_base_fee_rate),
    )
    increments = (
        required_base_quantity / common_increment
    ).to_integral_value(rounding=ROUND_CEILING)
    return increments * common_increment * spot_price


def _decimal_lcm(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        raise OrderSizingError("quantity increments must be positive")
    scale = max(-left.as_tuple().exponent, -right.as_tuple().exponent, 0)
    factor = 10**scale
    left_units = int(left * factor)
    right_units = int(right * factor)
    units = abs(left_units * right_units) // gcd(left_units, right_units)
    return Decimal(units) / Decimal(factor)


def _is_multiple(value: Decimal, increment: Decimal) -> bool:
    return value % increment == 0
