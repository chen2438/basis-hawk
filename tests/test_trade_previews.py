import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from basis_hawk.storage import Database


def _preview(
    *,
    preview_id: str | None = None,
    actor: str = "admin",
    expires_at: datetime | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": preview_id or str(uuid.uuid4()),
        "actor": actor,
        "request_fingerprint": "a" * 64,
        "exchange": "binance",
        "environment": "live",
        "base_asset": "ORDER",
        "requested_notional": Decimal("100"),
        "leverage": 2,
        "maximum_slippage": Decimal("0.001"),
        "market_observed_at": now,
        "confirmation_idempotency_key": None,
        "created_at": now,
        "expires_at": expires_at or now + timedelta(seconds=15),
        "confirmed_at": None,
    }


async def test_trade_preview_reserves_one_confirmation_idempotently() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    created = await database.create_trade_preview(preview=_preview())
    key = str(uuid.uuid4())

    reserved = await database.reserve_trade_preview(
        preview_id=created.id,
        actor="admin",
        request_fingerprint="a" * 64,
        idempotency_key=key,
    )
    repeated = await database.reserve_trade_preview(
        preview_id=created.id,
        actor="admin",
        request_fingerprint="different-after-confirmation",
        idempotency_key=key,
    )

    assert reserved.confirmation_idempotency_key == key
    assert reserved.confirmed_at is not None
    assert repeated.confirmation_idempotency_key == key
    with pytest.raises(ValueError, match="another idempotency key"):
        await database.reserve_trade_preview(
            preview_id=created.id,
            actor="admin",
            request_fingerprint="a" * 64,
            idempotency_key=str(uuid.uuid4()),
        )
    await database.close()


@pytest.mark.parametrize(
    ("preview_actor", "actor", "fingerprint", "expired", "message"),
    [
        ("first", "second", "a" * 64, False, "another administrator"),
        ("admin", "admin", "b" * 64, False, "changed after trade preview"),
        ("admin", "admin", "a" * 64, True, "expired"),
    ],
)
async def test_trade_preview_rejects_unsafe_confirmation(
    preview_actor: str,
    actor: str,
    fingerprint: str,
    expired: bool,
    message: str,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    preview = _preview(
        actor=preview_actor,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if expired
            else None
        ),
    )
    created = await database.create_trade_preview(preview=preview)

    with pytest.raises(ValueError, match=message):
        await database.reserve_trade_preview(
            preview_id=created.id,
            actor=actor,
            request_fingerprint=fingerprint,
            idempotency_key=str(uuid.uuid4()),
        )
    await database.close()
