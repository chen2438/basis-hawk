from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlencode

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
    UnsupportedTradingError,
    _bitget_trade_permission,
    _gate_trade_permission,
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


def test_bitget_permission_requires_both_trade_and_management_writes() -> None:
    assert _bitget_trade_permission(
        {
            "permType": "read-and-write",
            "permissions": ["uta_trade", "uta_mgt"],
        },
        generation="uta",
    ) is True
    assert _bitget_trade_permission(
        {
            "permType": "read-and-write",
            "permissions": ["uta_trade"],
        },
        generation="uta",
    ) is False
    assert _bitget_trade_permission(
        {"authorities": ["stow", "coow", "cpow"]},
        generation="classic",
    ) is True
    assert _bitget_trade_permission(
        {"authorities": ["stor", "coor", "cpor"]},
        generation="classic",
    ) is False


def test_gate_permission_requires_unique_unrestricted_writable_key() -> None:
    writable = {
        "state": 1,
        "key": "test-api-*****",
        "currency_pairs": [],
        "perms": [
            {"name": "spot", "read_only": False},
            {"name": "futures", "read_only": False},
        ],
    }
    assert _gate_trade_permission([writable], SECRETS.api_key) is True
    assert _gate_trade_permission(
        [
            {
                **writable,
                "perms": [
                    {"name": "spot", "read_only": False},
                    {"name": "futures", "read_only": True},
                ],
            }
        ],
        SECRETS.api_key,
    ) is False
    assert _gate_trade_permission(
        [{**writable, "currency_pairs": ["BTC_USDT"]}],
        SECRETS.api_key,
    ) is None
    assert _gate_trade_permission([], SECRETS.api_key) is None


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
        if request.url.path == "/fapi/v1/accountConfig":
            return httpx.Response(200, json={"canTrade": True})
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        return httpx.Response(
            200,
            json={
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


async def test_binance_requires_futures_account_configuration_trade_permission() -> None:
    def spot_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "accountType": "SPOT",
                "balances": [{"asset": "USDT", "free": "12.5"}],
            },
        )

    def perp_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/accountConfig":
            return httpx.Response(200, json={"canTrade": False})
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        return httpx.Response(
            200,
            json={
                "canTrade": True,
                "assets": [{"asset": "USDT", "availableBalance": "8.5"}],
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
        spot_client=spot,
        perp_client=perp,
    )
    snapshot = await client.snapshot()
    assert snapshot.trade_permission is False
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
                json={
                    "code": "0",
                    "data": [
                        {
                            "acctLv": "2",
                            "posMode": "net_mode",
                            "perm": "read_only,trade",
                        }
                    ],
                },
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
    assert snapshot.trade_permission is True
    await http.aclose()


async def test_bybit_account_snapshot_and_signature() -> None:
    position_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        assert request.headers["X-BAPI-SIGN"] == _hmac_hex(
            SECRETS.api_secret,
            f"1700000000000{SECRETS.api_key}5000{query}",
        )
        if request.url.path == "/v5/position/list":
            position_queries.append(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [],
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
        if request.url.path == "/v5/user/query-api":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "readOnly": 0,
                        "permissions": {
                            "ContractTrade": ["Order", "Position"],
                            "Spot": ["SpotTrade"],
                        },
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
                                    "totalOrderIM": "4",
                                    "totalPositionIM": "3",
                                    "bonus": "1",
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
        ExchangeSecrets(
            api_key=SECRETS.api_key,
            api_secret=SECRETS.api_secret,
            position_mode="one_way",
        ),
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        client=http,
    )
    snapshot = await client.snapshot()
    assert snapshot.spot_usdt_available == 26
    assert snapshot.perp_usdt_available == 19
    assert snapshot.perp_usdt_equity == 31
    assert snapshot.account_mode == "unified:5:ISOLATED_MARGIN"
    assert snapshot.position_mode == PositionMode.ONE_WAY
    assert snapshot.trade_permission is True
    assert position_queries == [
        {"category": "linear", "limit": "200", "settleCoin": "USDT"},
        {"category": "linear", "limit": "200", "symbol": "BTCUSDT"},
    ]
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
        if request.url.path.endswith("/spot/account/info"):
            return httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": {
                        "authorities": ["stor", "stow", "coow", "cpow"]
                    },
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
    assert snapshot.trade_permission is True
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
        if request.url.path.endswith("/account/main_keys"):
            return httpx.Response(
                200,
                json=[
                    {
                        "state": 1,
                        "key": "test-api-*****",
                        "currency_pairs": [],
                        "perms": [
                            {"name": "spot", "read_only": False},
                            {"name": "futures", "read_only": False},
                        ],
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "user": 20011,
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
    assert snapshot.trade_permission is True
    assert await client.user_id() == "20011"
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
    assert snapshot.trade_permission is True
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


async def test_binance_internal_transfer_submission_and_confirmation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            values = parse_qs(request.content.decode())
            assert values["type"] == ["MAIN_UMFUTURE"]
            assert values["asset"] == ["USDT"]
            assert values["amount"] == ["12.5"]
            return httpx.Response(200, json={"tranId": 12345})
        assert request.url.params["type"] == "MAIN_UMFUTURE"
        return httpx.Response(
            200,
            json={
                "total": 1,
                "rows": [
                    {
                        "asset": "USDT",
                        "amount": "12.5",
                        "type": "MAIN_UMFUTURE",
                        "status": "CONFIRMED",
                        "tranId": 12345,
                        "timestamp": 1_700_000_000_000,
                    }
                ],
            },
        )

    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://binance.test",
    )
    perp = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://binance-futures.test",
    )
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_100_000,
        spot_client=spot,
        perp_client=perp,
    )

    submitted = await client.submit_internal_transfer(
        transfer_id="local-id",
        direction="spot_to_perp",
        amount=Decimal("12.5"),
    )
    assert submitted.transfer_id == "12345"
    assert submitted.status == "pending"
    confirmed = await client.internal_transfer_status(
        transfer_id="12345",
        client_transfer_id="local-id",
        direction="spot_to_perp",
        amount=Decimal("12.5"),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert confirmed.status == "completed"
    assert len(requests) == 2
    await spot.aclose()
    await perp.aclose()


async def test_binance_internal_transfer_rejects_sandbox() -> None:
    spot = httpx.AsyncClient(base_url="https://binance.test")
    perp = httpx.AsyncClient(base_url="https://binance-futures.test")
    client = BinanceAccountClient(
        SECRETS,
        ExchangeEnvironment.SANDBOX,
        spot_client=spot,
        perp_client=perp,
    )
    with pytest.raises(UnsupportedEnvironmentError):
        await client.submit_internal_transfer(
            transfer_id="local-id",
            direction="perp_to_spot",
            amount=Decimal("1"),
        )
    await spot.aclose()
    await perp.aclose()


async def test_bitget_classic_internal_transfer_is_idempotent_and_confirmed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/account/settings":
            return httpx.Response(200, json={"code": "40000", "msg": "classic"})
        if request.url.path == "/api/v2/mix/account/account":
            return httpx.Response(
                200,
                json={"code": "00000", "data": {"posMode": "one_way_mode"}},
            )
        if request.url.path == "/api/v2/spot/wallet/transfer":
            body = __import__("json").loads(request.content)
            assert body == {
                "amount": "7.25",
                "clientOid": "local-transfer-id",
                "coin": "USDT",
                "fromType": "spot",
                "toType": "usdt_futures",
            }
            return httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": {
                        "transferId": "bitget-remote-id",
                        "clientOid": "local-transfer-id",
                    },
                },
            )
        assert request.url.path == "/api/v2/spot/account/transferRecords"
        assert request.url.params["clientOid"] == "local-transfer-id"
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "transferId": "bitget-remote-id",
                        "clientOid": "local-transfer-id",
                        "coin": "USDT",
                        "fromType": "spot",
                        "toType": "usdt_futures",
                        "size": "7.25",
                        "status": "Successful",
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_800_000_000_000,
        client=http,
    )
    submitted = await client.submit_internal_transfer(
        transfer_id="local-transfer-id",
        direction="spot_to_perp",
        amount=Decimal("7.25"),
    )
    assert submitted.transfer_id == "bitget-remote-id"
    confirmed = await client.internal_transfer_status(
        transfer_id=submitted.transfer_id,
        client_transfer_id="local-transfer-id",
        direction="spot_to_perp",
        amount=Decimal("7.25"),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert confirmed.status == "completed"
    await http.aclose()


async def test_bitget_unified_internal_transfer_is_not_required() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "code": "00000",
                    "data": {"accountMode": "unified"},
                },
            )
        ),
        base_url="https://bitget.test",
    )
    client = BitgetAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        client=http,
    )
    with pytest.raises(
        UnsupportedTradingError,
        match="share spot and futures collateral",
    ):
        await client.submit_internal_transfer(
            transfer_id="local-transfer-id",
            direction="perp_to_spot",
            amount=Decimal("1"),
        )
    await http.aclose()


async def test_gate_internal_transfer_submission_and_confirmation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = __import__("json").loads(request.content)
            assert body == {
                "amount": "3.5",
                "client_order_id": "local-transfer-id",
                "currency": "USDT",
                "from": "futures",
                "settle": "usdt",
                "to": "spot",
            }
            return httpx.Response(200, json={"tx_id": "gate-remote-id"})
        assert request.url.path == "/api/v4/wallet/order_status"
        assert request.url.params["client_order_id"] == "local-transfer-id"
        assert request.url.params["tx_id"] == "gate-remote-id"
        return httpx.Response(
            200,
            json={"tx_id": "gate-remote-id", "status": "SUCCESS"},
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
    submitted = await client.submit_internal_transfer(
        transfer_id="local-transfer-id",
        direction="perp_to_spot",
        amount=Decimal("3.5"),
    )
    assert submitted.transfer_id == "gate-remote-id"
    confirmed = await client.internal_transfer_status(
        transfer_id=submitted.transfer_id,
        client_transfer_id="local-transfer-id",
        direction="perp_to_spot",
        amount=Decimal("3.5"),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert confirmed.status == "completed"
    await http.aclose()


async def test_mexc_internal_transfer_submission_and_confirmation() -> None:
    def spot_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            values = parse_qs(request.content.decode())
            assert values["fromAccountType"] == ["SPOT"]
            assert values["toAccountType"] == ["FUTURES"]
            assert values["asset"] == ["USDT"]
            assert values["amount"] == ["2.75"]
            return httpx.Response(200, json={"tranId": "mexc-remote-id"})
        assert request.url.params["tranId"] == "mexc-remote-id"
        return httpx.Response(
            200,
            json={
                "tranId": "mexc-remote-id",
                "asset": "USDT",
                "fromAccountType": "SPOT",
                "toAccountType": "FUTURES",
                "amount": "2.75",
                "status": "SUCCESS",
            },
        )

    spot = httpx.AsyncClient(
        transport=httpx.MockTransport(spot_handler),
        base_url="https://mexc.test",
    )
    perp = httpx.AsyncClient(base_url="https://mexc-futures.test")
    client = MexcAccountClient(
        SECRETS,
        ExchangeEnvironment.LIVE,
        clock_ms=lambda: 1_700_000_000_000,
        spot_client=spot,
        perp_client=perp,
    )
    submitted = await client.submit_internal_transfer(
        transfer_id="local-transfer-id",
        direction="spot_to_perp",
        amount=Decimal("2.75"),
    )
    assert submitted.transfer_id == "mexc-remote-id"
    confirmed = await client.internal_transfer_status(
        transfer_id=submitted.transfer_id,
        client_transfer_id="local-transfer-id",
        direction="spot_to_perp",
        amount=Decimal("2.75"),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    assert confirmed.status == "completed"
    await spot.aclose()
    await perp.aclose()
