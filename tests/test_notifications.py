from datetime import UTC, datetime, timedelta

import pytest

from basis_hawk.storage import Database


async def test_notification_outbox_deduplicates_each_channel() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)

    first = await database.enqueue_notification(
        dedupe_key="trade:abc:filled",
        event_type="trade.filled",
        severity="info",
        channels={"telegram", "email"},
        subject="Trade filled",
        body="Paired trade abc filled.",
        now=now,
    )
    second = await database.enqueue_notification(
        dedupe_key="trade:abc:filled",
        event_type="trade.filled",
        severity="info",
        channels={"telegram", "email"},
        subject="Ignored duplicate",
        body="This duplicate must not replace the original.",
        now=now + timedelta(seconds=1),
    )

    assert len(first) == len(second) == 2
    assert {item.channel for item in first} == {"telegram", "email"}
    assert {item.id for item in first} == {item.id for item in second}
    assert {item.subject for item in second} == {"Trade filled"}
    await database.close()


async def test_notification_claim_retries_with_exponential_backoff() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    [created] = await database.enqueue_notification(
        dedupe_key="risk:paused:1",
        event_type="risk.paused",
        severity="critical",
        channels={"telegram"},
        subject="Trading paused",
        body="Execution is paused.",
        now=now,
    )

    [claimed] = await database.claim_notifications(now=now)
    assert claimed.id == created.id
    assert claimed.status == "sending"
    assert claimed.attempts == 1

    retry = await database.mark_notification_failed(
        claimed.id,
        error_code="network_error",
        now=now,
    )
    assert retry is not None
    assert retry.status == "retry"
    assert retry.next_attempt_at == now + timedelta(seconds=30)
    assert await database.claim_notifications(now=now + timedelta(seconds=29)) == []

    [claimed_again] = await database.claim_notifications(
        now=now + timedelta(seconds=30)
    )
    assert claimed_again.attempts == 2
    retry_again = await database.mark_notification_failed(
        claimed.id,
        error_code="remote_rejected",
        now=now + timedelta(seconds=30),
    )
    assert retry_again is not None
    assert retry_again.next_attempt_at == now + timedelta(seconds=90)
    await database.close()


async def test_notification_claim_recovers_stale_sending_and_can_settle() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    [created] = await database.enqueue_notification(
        dedupe_key="account:bybit:down",
        event_type="account.unavailable",
        severity="critical",
        channels={"email"},
        subject="Bybit unavailable",
        body="Account reconciliation failed.",
        now=now,
    )
    [first_claim] = await database.claim_notifications(now=now)

    assert (
        await database.claim_notifications(
            now=now + timedelta(minutes=4, seconds=59)
        )
        == []
    )
    [recovered] = await database.claim_notifications(
        now=now + timedelta(minutes=5)
    )
    assert recovered.id == first_claim.id == created.id
    assert recovered.attempts == 2
    assert await database.mark_notification_sent(
        recovered.id,
        now=now + timedelta(minutes=5),
    )
    [settled] = await database.notification_outbox()
    assert settled.status == "sent"
    assert settled.sent_at == now + timedelta(minutes=5)
    assert (
        await database.mark_notification_failed(
            settled.id,
            error_code="late_failure",
            now=now + timedelta(minutes=6),
        )
        is None
    )
    await database.close()


async def test_notification_failure_becomes_dead_and_rejects_unsafe_error() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    [created] = await database.enqueue_notification(
        dedupe_key="risk:dead:1",
        event_type="risk.alert",
        severity="warning",
        channels={"email"},
        subject="Risk warning",
        body="A risk threshold was reached.",
        now=now,
    )
    [claimed] = await database.claim_notifications(now=now)

    with pytest.raises(ValueError, match="safe lowercase identifier"):
        await database.mark_notification_failed(
            claimed.id,
            error_code="https://secret.example/?token=secret",
            now=now,
        )
    dead = await database.mark_notification_failed(
        claimed.id,
        error_code="authentication_failed",
        now=now,
        max_attempts=1,
    )
    assert dead is not None
    assert dead.status == "dead"
    assert dead.last_error_code == "authentication_failed"
    assert await database.claim_notifications(now=now + timedelta(days=1)) == []
    assert created.id == dead.id
    await database.close()
