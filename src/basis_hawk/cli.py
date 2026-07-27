from __future__ import annotations

import argparse
import asyncio
import getpass
import logging

import uvicorn

from basis_hawk.accounts import create_account_client
from basis_hawk.auth import AuthenticationError, AuthService
from basis_hawk.config import get_config
from basis_hawk.credentials import CredentialService
from basis_hawk.crypto import SecretCipher
from basis_hawk.exchanges import (
    BinanceAdapter,
    BitgetAdapter,
    BybitAdapter,
    GateAdapter,
    MexcAdapter,
    OkxAdapter,
)
from basis_hawk.models import Exchange
from basis_hawk.notifications import (
    NotificationDeliveryService,
    NotificationProjectionService,
)
from basis_hawk.private_stream import (
    DynamicPrivateStreamManager,
    PrivateStreamRegistry,
    PrivateStreamSupervisor,
)
from basis_hawk.private_stream_factory import create_private_stream_connection
from basis_hawk.reconciliation import ReconciliationService, WorkerLockUnavailable
from basis_hawk.storage import Database

MINIMUM_ADMIN_PASSWORD_LENGTH = 12


async def doctor() -> int:
    config = get_config()
    database = Database(config.database_url)
    await database.initialize()
    print("database: ok")
    adapters = {
        Exchange.BINANCE: BinanceAdapter(timeout=config.http_timeout_seconds),
        Exchange.OKX: OkxAdapter(timeout=config.http_timeout_seconds),
        Exchange.MEXC: MexcAdapter(timeout=config.http_timeout_seconds),
        Exchange.BYBIT: BybitAdapter(timeout=config.http_timeout_seconds),
        Exchange.BITGET: BitgetAdapter(timeout=config.http_timeout_seconds),
        Exchange.GATE: GateAdapter(timeout=config.http_timeout_seconds),
    }
    failed = False
    for exchange, adapter in adapters.items():
        try:
            pairs = await adapter.instruments()
            quotes = await adapter.quotes(pairs[:3])
            if not pairs or not quotes:
                raise RuntimeError("no matching instruments or quotes")
            print(f"{exchange.value}: ok ({len(pairs)} common pairs)")
        except Exception as exc:
            failed = True
            print(f"{exchange.value}: failed ({exc})")
        finally:
            await adapter.close()
    await database.close()
    return int(failed)


def prompt_admin_password() -> str:
    print(
        "Administrator password must contain at least "
        f"{MINIMUM_ADMIN_PASSWORD_LENGTH} characters."
    )
    while True:
        password = getpass.getpass("Administrator password: ")
        if len(password) < MINIMUM_ADMIN_PASSWORD_LENGTH:
            print(
                "admin-create: password must contain at least "
                f"{MINIMUM_ADMIN_PASSWORD_LENGTH} characters; try again"
            )
            continue
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("admin-create: passwords do not match; try again")
            continue
        return password


async def create_admin(username: str) -> int:
    config = get_config()
    if config.credential_master_key is None:
        print("admin-create: BASIS_HAWK_CREDENTIAL_MASTER_KEY is required")
        return 1
    password = prompt_admin_password()
    database = Database(config.database_url)
    await database.initialize()
    auth = AuthService(
        database,
        SecretCipher(config.credential_master_key.get_secret_value()),
        session_hours=config.session_hours,
    )
    try:
        provisioning_uri = await auth.bootstrap_admin(username, password)
    except (RuntimeError, ValueError) as exc:
        print(f"admin-create: {exc}")
        return 1
    finally:
        await database.close()
    print("Administrator created. Add this URI to your authenticator:")
    print(provisioning_uri)
    return 0


async def rotate_admin_totp(username: str) -> int:
    config = get_config()
    if config.credential_master_key is None:
        print(
            "admin-rotate-totp: BASIS_HAWK_CREDENTIAL_MASTER_KEY is required"
        )
        return 1
    print(
        "This rotates the administrator TOTP and signs out every active session."
    )
    password = getpass.getpass("Current administrator password: ")
    database = Database(config.database_url)
    await database.initialize()
    auth = AuthService(
        database,
        SecretCipher(config.credential_master_key.get_secret_value()),
        session_hours=config.session_hours,
    )
    try:
        provisioning_uri = await auth.rotate_admin_totp(username, password)
    except (AuthenticationError, RuntimeError) as exc:
        print(f"admin-rotate-totp: {exc}")
        return 1
    finally:
        await database.close()
    print("Administrator TOTP rotated. Existing sessions are now invalid.")
    print("Add this one-time URI to your authenticator:")
    print(provisioning_uri)
    print("This URI will not be displayed again; store it securely.")
    return 0


async def request_post_update_reconciliation(*, required: bool) -> int:
    config = get_config()
    database = Database(config.database_url)
    await database.initialize()
    try:
        accepted = await database.request_post_update_reconciliation()
        if not accepted:
            print(
                "update-reconcile: no software-update safety pause was found"
            )
            return int(required)
        await database.append_audit(
            "software.update_reconciliation_requested",
            actor="system:update-agent",
            details={},
        )
    finally:
        await database.close()
    print("update-reconcile: fresh safety reconciliation requested")
    return 0


async def run_worker(*, once: bool) -> int:
    config = get_config()
    if config.credential_master_key is None:
        print("worker: BASIS_HAWK_CREDENTIAL_MASTER_KEY is required")
        return 1
    database = Database(config.database_url)
    await database.initialize()
    await database.reset_private_stream_states()
    credentials = CredentialService(
        database,
        SecretCipher(config.credential_master_key.get_secret_value()),
    )
    reconciler = ReconciliationService(
        database,
        credentials,
        account_client_factory=lambda exchange, secrets, environment: (
            create_account_client(
                exchange,
                secrets,
                environment,
                timeout=config.http_timeout_seconds,
            )
        ),
    )
    notifications = NotificationDeliveryService.from_config(database, config)
    notification_projection = NotificationProjectionService(database)
    try:
        await notification_projection.run_once(emit_initial_alerts=False)
        if once:
            result = await reconciler.run_once_exclusive()
            await notification_projection.run_once()
            delivered = await notifications.run_once()
            print(
                "worker: reconciliation "
                f"checked={result.accounts_checked} "
                f"blocked={result.accounts_blocked} "
                f"failed={result.accounts_failed} "
                f"execution={result.execution_state} "
                f"notifications={delivered}"
            )
            return 0
        async def reconcile_private_event(
            _connection: object,
            _event: object,
        ) -> None:
            reconciler.request_reconciliation()

        supervisor = PrivateStreamSupervisor(
            PrivateStreamRegistry(database),
            event_handler=reconcile_private_event,
            state_handler=lambda _connection, _connected: (
                reconciler.request_reconciliation()
            ),
        )
        stream_manager = DynamicPrivateStreamManager(
            supervisor,
            credentials.list,
            lambda summary: create_private_stream_connection(
                credentials,
                summary,
                timeout_seconds=config.http_timeout_seconds,
            ),
        )
        tasks = [
            asyncio.create_task(stream_manager.run()),
            asyncio.create_task(reconciler.run_forever()),
            asyncio.create_task(notification_projection.run_forever()),
            asyncio.create_task(notifications.run_forever()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except WorkerLockUnavailable as exc:
        print(f"worker: {exc}")
        return 1
    finally:
        await notifications.close()
        await database.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="basis-hawk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("serve")
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--once", action="store_true")
    admin_parser = subparsers.add_parser("admin-create")
    admin_parser.add_argument("--username", default="admin")
    rotate_totp_parser = subparsers.add_parser("admin-rotate-totp")
    rotate_totp_parser.add_argument("--username", default="admin")
    update_reconcile_parser = subparsers.add_parser("update-reconcile")
    update_reconcile_parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    config = get_config()
    logging.basicConfig(level=config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "doctor":
        raise SystemExit(asyncio.run(doctor()))
    if args.command == "admin-create":
        raise SystemExit(asyncio.run(create_admin(args.username)))
    if args.command == "admin-rotate-totp":
        raise SystemExit(asyncio.run(rotate_admin_totp(args.username)))
    if args.command == "update-reconcile":
        raise SystemExit(
            asyncio.run(
                request_post_update_reconciliation(required=args.required)
            )
        )
    if args.command == "worker":
        raise SystemExit(asyncio.run(run_worker(once=args.once)))
    uvicorn.run(
        "basis_hawk.api:app",
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
