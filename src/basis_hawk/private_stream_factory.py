from __future__ import annotations

from basis_hawk.binance_private_stream import BinancePrivateStreamConnection
from basis_hawk.bitget_private_stream import BitgetPrivateStreamConnection
from basis_hawk.bybit_private_stream import BybitPrivateStreamConnection
from basis_hawk.credentials import CredentialService
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
        secrets = await credentials.load(summary.exchange, summary.environment)
        if secrets is None:
            continue
        if summary.exchange == Exchange.BINANCE:
            connections.append(
                BinancePrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif summary.exchange == Exchange.OKX:
            connections.append(
                OkxPrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif summary.exchange == Exchange.BYBIT:
            connections.append(
                BybitPrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif summary.exchange == Exchange.BITGET:
            connections.append(
                BitgetPrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif summary.exchange == Exchange.GATE:
            connections.append(
                GatePrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif summary.exchange == Exchange.MEXC:
            connections.append(
                MexcPrivateStreamConnection(
                    secrets,
                    summary.environment,
                    timeout_seconds=timeout_seconds,
                )
            )
    return connections
