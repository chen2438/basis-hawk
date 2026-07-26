from __future__ import annotations

from basis_hawk.binance_private_stream import BinancePrivateStreamConnection
from basis_hawk.credentials import CredentialService
from basis_hawk.models import Exchange
from basis_hawk.private_stream import PrivateStreamConnection


async def create_private_stream_connections(
    credentials: CredentialService,
    *,
    timeout_seconds: float,
) -> list[PrivateStreamConnection]:
    connections: list[PrivateStreamConnection] = []
    for summary in await credentials.list():
        if summary.exchange != Exchange.BINANCE:
            continue
        secrets = await credentials.load(summary.exchange, summary.environment)
        if secrets is None:
            continue
        connections.append(
            BinancePrivateStreamConnection(
                secrets,
                summary.environment,
                timeout_seconds=timeout_seconds,
            )
        )
    return connections
