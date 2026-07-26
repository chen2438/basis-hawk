from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.accounts import (
    AccountSnapshot,
    PositionMode,
    PrivateRequestError,
)
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange
from basis_hawk.reconciliation import ReconciliationService
from basis_hawk.storage import Database


class FakeAccountClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    async def snapshot(self) -> AccountSnapshot:
        if self.fail:
            raise PrivateRequestError("signed URL contained a sensitive value")
        return AccountSnapshot(
            exchange=Exchange.BINANCE,
            environment=ExchangeEnvironment.LIVE,
            observed_at=datetime(2026, 7, 26, 18, 0, tzinfo=UTC),
            spot_usdt_available=Decimal("100.25"),
            perp_usdt_available=Decimal("80.5"),
            perp_usdt_equity=Decimal("82"),
            shared_balance=False,
            account_mode="spot+usdt_futures",
            position_mode=PositionMode.ONE_WAY,
            trade_permission=True,
        )

    async def close(self) -> None:
        self.closed = True


async def _credentials(database: Database) -> CredentialService:
    service = CredentialService(database, SecretCipher(SecretCipher.generate_key()))
    await service.save(
        exchange=Exchange.BINANCE,
        environment=ExchangeEnvironment.LIVE,
        label="primary",
        secrets=ExchangeSecrets(
            api_key="test-api-key",
            api_secret="test-api-secret",
        ),
        actor="test",
    )
    return service


async def test_startup_reconciliation_persists_snapshot_but_keeps_execution_blocked() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    client = FakeAccountClient()
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await reconciler.run_once()

    assert result.accounts_checked == 1
    assert result.accounts_blocked == 1
    assert result.accounts_failed == 0
    assert result.execution_state == "blocked"
    assert client.closed is True
    control = await database.execution_control()
    assert control is not None
    assert control.state == "blocked"
    states = await database.reconciliation_states()
    assert len(states) == 1
    assert states[0].status == "blocked"
    assert states[0].snapshot_id is not None
    assert "orders" in states[0].reason
    await database.close()


async def test_reconciliation_failure_is_persisted_without_exception_details() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    credentials = await _credentials(database)
    client = FakeAccountClient(fail=True)
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: client,
    )

    result = await reconciler.run_once()

    assert result.accounts_failed == 1
    states = await database.reconciliation_states()
    assert states[0].status == "error"
    assert states[0].reason == "private account reconciliation failed"
    assert "sensitive" not in states[0].reason
    assert client.closed is True
    await database.close()


async def test_sqlite_executor_lock_is_available_for_tests() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    async with database.executor_lock() as acquired:
        assert acquired is True
    await database.close()
