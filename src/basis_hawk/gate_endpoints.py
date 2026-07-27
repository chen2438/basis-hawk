from __future__ import annotations

from dataclasses import dataclass

from basis_hawk.credentials import ExchangeEnvironment


@dataclass(frozen=True)
class GateEndpoints:
    rest: str
    spot_private_websocket: str
    futures_private_websocket: str


def gate_endpoints(environment: ExchangeEnvironment) -> GateEndpoints:
    if environment == ExchangeEnvironment.SANDBOX:
        return GateEndpoints(
            rest="https://api-testnet.gateapi.io",
            spot_private_websocket="wss://ws-testnet.gate.com/v4/ws/spot",
            futures_private_websocket=(
                "wss://ws-testnet.gate.com/v4/ws/futures/usdt"
            ),
        )
    return GateEndpoints(
        rest="https://api.gateio.ws",
        spot_private_websocket="wss://api.gateio.ws/ws/v4/",
        futures_private_websocket="wss://fx-ws.gateio.ws/v4/ws/usdt",
    )
