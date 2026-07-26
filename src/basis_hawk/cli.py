from __future__ import annotations

import argparse
import asyncio
import getpass
import logging

import uvicorn

from basis_hawk.accounts import create_account_client
from basis_hawk.auth import AuthService
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
from basis_hawk.notifications import NotificationDeliveryService
from basis_hawk.private_stream import PrivateStreamRegistry, PrivateStreamSupervisor
from basis_hawk.private_stream_factory import create_private_stream_connections
from basis_hawk.reconciliation import ReconciliationService, WorkerLockUnavailable
from basis_hawk.storage import Database


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


async def create_admin(username: str) -> int:
    config = get_config()
    if config.credential_master_key is None:
        print("admin-create: BASIS_HAWK_CREDENTIAL_MASTER_KEY is required")
        return 1
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("admin-create: passwords do not match")
        return 1
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
    try:
        if once:
            result = await reconciler.run_once_exclusive()
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
        connections = await create_private_stream_connections(
            credentials,
            timeout_seconds=config.http_timeout_seconds,
        )

        async def reconcile_private_event(
            _connection: object,
            _event: object,
        ) -> None:
            reconciler.request_reconciliation()

        supervisor = PrivateStreamSupervisor(
            PrivateStreamRegistry(database),
            event_handler=reconcile_private_event,
        )
        tasks = [
            asyncio.create_task(supervisor.run(connections)),
            asyncio.create_task(reconciler.run_forever()),
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
    args = parser.parse_args()
    config = get_config()
    logging.basicConfig(level=config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "doctor":
        raise SystemExit(asyncio.run(doctor()))
    if args.command == "admin-create":
        raise SystemExit(asyncio.run(create_admin(args.username)))
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
