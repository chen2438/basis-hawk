from __future__ import annotations

from basis_hawk.binance_private_stream import BinancePrivateStreamConnection
from basis_hawk.bitget_private_stream import BitgetPrivateStreamConnection
from basis_hawk.bybit_private_stream import BybitPrivateStreamConnection
from basis_hawk.credentials import CredentialService, CredentialSummary
from basis_hawk.gate_private_stream import GatePrivateStreamConnection
from basis_hawk.mexc_private_stream import MexcPrivateStreamConnection
from basis_hawk.models import Exchange
from basis_hawk.okx_private_stream import OkxPrivateStreamConnection
from basis_hawk.private_stream import PrivateStreamConnection


async def create_private_stream_connections(
    credentials: CredentialService,
    *,
    timeout_seconds: float,
) -> list[PrivateStreamConnection]:
    connections: list[PrivateStreamConnection] = []
    for summary in await credentials.list():
        if not summary.is_default:
            continue
        connections.append(
            await create_private_stream_connection(
                credentials,
                summary,
                timeout_seconds=timeout_seconds,
            )
        )
    return connections


async def create_private_stream_connection(
    credentials: CredentialService,
    summary: CredentialSummary,
    *,
    timeout_seconds: float,
) -> PrivateStreamConnection:
    secrets = await credentials.load_by_id(summary.id)
    if secrets is None:
        raise ValueError("private stream credentials were removed")
    connection_types = {
        Exchange.BINANCE: BinancePrivateStreamConnection,
        Exchange.OKX: OkxPrivateStreamConnection,
        Exchange.BYBIT: BybitPrivateStreamConnection,
        Exchange.BITGET: BitgetPrivateStreamConnection,
        Exchange.GATE: GatePrivateStreamConnection,
        Exchange.MEXC: MexcPrivateStreamConnection,
    }
    return connection_types[summary.exchange](
        secrets,
        summary.environment,
        timeout_seconds=timeout_seconds,
    )
