from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from basis_hawk.config import get_config
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="basis-hawk")
    parser.add_argument("command", choices=["doctor", "serve"])
    args = parser.parse_args()
    config = get_config()
    logging.basicConfig(level=config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "doctor":
        raise SystemExit(asyncio.run(doctor()))
    uvicorn.run(
        "basis_hawk.api:app", host="127.0.0.1", port=config.port, log_level=config.log_level.lower()
    )


if __name__ == "__main__":
    main()
