from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pytest

from basis_hawk.accounts import (
    BinanceAccountClient,
    BitgetAccountClient,
    BybitAccountClient,
    GateAccountClient,
    MexcAccountClient,
    OkxAccountClient,
    PositionMode,
    PrivateRequestError,
    UnsupportedEnvironmentError,
    _hmac_base64,
    _hmac_hex,
)
from basis_hawk.credentials import ExchangeEnvironment, ExchangeSecrets

SECRETS = ExchangeSecrets(
    api_key="test-api-key",
    api_secret="test-api-secret",
    passphrase="test-passphrase",
)


def _query_without_signature(request: httpx.Request) -> str:
    values = [
        (key, value)
        for key, value in request.url.params.multi_items()
        if key != "signature"
    ]
    return urlencode(sorted(values))


async def test_binance_account_snapshot_and_signature() -> None:
    def spot_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-MBX-APIKEY"] == SECRETS.api_key
        assert request.url.params["signature"] == _hmac_hex(
            SECRETS.api_secret,
            _query_without_signature(request),
        )
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "accountType": "SPOT",
                "balances": [{"asset": "USDT", "free": "12.5", "locked": "1"}],
            },
        )

    def perp_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["signature"] == _hmac_hex(
            SECRETS.api_secret,
            _query_without_signature(request),
        )
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "assets": [
                    {
                        "asset": "USDT",
                        "availableBalance": "8.5",
                        "walletBalance": "9.5",
                    }
                ],
            },
        )

    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(spot_handler),
        base_url="https://spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(perp_handler),
        base_url="https://perp.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        spot_client=spot,
        perp_client=perp,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 12.5
    assert snapshot.perp_usdt_available == 8.5
    assert snapshot.position_mode == PositionMode.ONE_WAY
    assert snapshot.trade_permission is True
    await spot.aclose()
    await perp.aclose()


async def test_okx_account_snapshot_and_signature() -> None:
    clock = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        timestamp = "2026-07-26T12:00:00.000Z"
        assert request.headers["OK-ACCESS-TIMESTAMP"] == timestamp
        assert request.headers["OK-ACCESS-SIGN"] == _hmac_base64(
            SECRETS.api_secret,
            f"{timestamp}GET{request.url.raw_path.decode()}",
        )
        if request.url.path == "/api/v5/account/config":
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode"}]},
            )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "details": [
                            {"ccy": "USDT", "availBal": "25", "eq": "27"}
                        ]
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://okx.test",
    )
    client = OkxAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        clock=lambda: clock,
        client=http,
    )
    snapshot = await client.snapshot()
    assert snapshot.shared_balance is True
    assert snapshot.spot_usdt_available == 25
    assert snapshot.position_mode == PositionMode.ONE_WAY
    await http.aclose()


async def test_bybit_account_snapshot_and_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        assert request.headers["X-BAPI-SIGN"] == _hmac_hex(
            SECRETS.api_secret,
            f"1700000000000{SECRETS.api_key}5000{query}",
        )
        if request.url.path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [{"positionIdx": 0, "size": "0"}],
                        "nextPageCursor": "",
                    },
                },
            )
        if request.url.path == "/v5/account/info":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "unifiedMarginStatus": 5,
                        "marginMode": "ISOLATED_MARGIN",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "totalAvailableBalance": "30",
                            "coin": [
                                {
                                    "coin": "USDT",
                                    "equity": "31",
                                    "walletBalance": "29",
                                    "locked": "2",
                                    "spotBorrow": "1",
                                }
                            ],
                        }
                    ]
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    )
    client = BybitAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        client=http,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 26
    assert snapshot.perp_usdt_equity == 31
    assert snapshot.account_mode == "unified:5:ISOLATED_MARGIN"
    assert snapshot.position_mode == PositionMode.ONE_WAY
    await http.aclose()


async def test_bitget_account_snapshot_and_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        request_path = request.url.path + (f"?{query}" if query else "")
        assert request.headers["ACCESS-SIGN"] == _hmac_base64(
            SECRETS.api_secret,
            f"1700000000000GET{request_path}",
        )
        if request.url.path.endswith("/spot/account/assets"):
            return httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": [{"coin": "USDT", "available": "14"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": {
                    "available": "15",
                    "accountEquity": "16",
                    "assetMode": "single",
                    "marginMode": "isolated",
                    "posMode": "one_way_mode",
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        client=http,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 14
    assert snapshot.perp_usdt_available == 15
    assert snapshot.position_mode == PositionMode.ONE_WAY
    await http.aclose()


async def test_gate_account_snapshot_and_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        body_hash = __import__("hashlib").sha512(b"").hexdigest()
        assert request.headers["SIGN"] == _hmac_hex(
            SECRETS.api_secret,
            f"GET\n{request.url.path}\n{query}\n{body_hash}\n1700000000",
            "sha512",
        )
        if request.url.path.endswith("/spot/accounts"):
            return httpx.Response(
                200,
                json=[{"currency": "USDT", "available": "21"}],
            )
        return httpx.Response(
            200,
            json={
                "available": "22",
                "total": "23",
                "in_dual_mode": False,
                "enable_evolved_classic": False,
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gate.test",
    )
    client = GateAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_s=lambda: 1_700_000_000,
        client=http,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 21
    assert snapshot.perp_usdt_equity == 23
    assert snapshot.position_mode == PositionMode.ONE_WAY
    await http.aclose()

    with pytest.raises(UnsupportedEnvironmentError):
        GateAccountClient(SECRETS, ExchangeEnvironment.SANDBOX)


async def test_mexc_account_snapshot_and_signature() -> None:
    def spot_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["signature"] == _hmac_hex(
            SECRETS.api_secret,
            _query_without_signature(request),
        )
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "accountType": "SPOT",
                "balances": [{"asset": "USDT", "free": "4"}],
            },
        )

    def perp_handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        assert request.headers["Signature"] == _hmac_hex(
            SECRETS.api_secret,
            f"{SECRETS.api_key}1700000000000{query}",
        )
        if request.url.path.endswith("position_mode"):
            return httpx.Response(200, json={"success": True, "code": 0, "data": 2})
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "data": {"availableBalance": "5", "equity": "6"},
            },
        )

    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(spot_handler),
        base_url="https://mexc-spot.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(perp_handler),
        base_url="https://mexc-perp.test",
    )
    client = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        spot_client=spot,
        perp_client=perp,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 4
    assert snapshot.perp_usdt_available == 5
    assert snapshot.position_mode == PositionMode.ONE_WAY
    await spot.aclose()
    await perp.aclose()

    with pytest.raises(UnsupportedEnvironmentError):
        MexcAccountClient(SECRETS, ExchangeEnvironment.SANDBOX)


async def test_private_error_never_contains_signed_url_or_secret() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"msg": "denied"})),
        base_url="https://private.test",
    )
    client = BybitAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        client=http,
    )
    with pytest.raises(PrivateRequestError) as caught:
        await client.snapshot()
    message = str(caught.value)
    assert SECRETS.api_secret not in message
    assert "X-BAPI-SIGN" not in message
    assert "signature" not in message.lower()
    await http.aclose()
