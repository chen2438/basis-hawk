from __future__ import annotations

import argparse
import asyncio
import getpass
import logging

import uvicorn

from basis_hawk.auth import AuthService
from basis_hawk.config import get_config
from basis_hawk.crypto import SecretCipher
from basis_hawk.exchanges import BinanceAdapter, BybitAdapter, MexcAdapter, OkxAdapter
from basis_hawk.models import Exchange
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="basis-hawk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("serve")
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
    uvicorn.run(
        "basis_hawk.api:app",
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
