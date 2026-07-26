from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from basis_hawk.accounts import (
    PrivateAccountClient,
    PrivateRequestError,
    UnsupportedEnvironmentError,
    create_account_client,
)
from basis_hawk.auth import AuthenticationError, AuthService, LoginAttemptLimiter
from basis_hawk.config import get_config
from basis_hawk.credentials import (
    CredentialService,
    ExchangeEnvironment,
    ExchangeSecrets,
)
from basis_hawk.crypto import SecretCipher
from basis_hawk.models import Exchange, Quality, ScannerSettings
from basis_hawk.service import ScannerService, default_adapters
from basis_hawk.storage import Database
from basis_hawk.trading import IdempotencyConflict, TradeLedger, TradeValidationError

SESSION_COOKIE = "basis_hawk_session"
CSRF_COOKIE = "basis_hawk_csrf"


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str


class CredentialRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr | None = None


class PaperOpenRequest(BaseModel):
    exchange: Exchange
    base_asset: str = Field(min_length=1, max_length=40)
    notional_usdt: Decimal = Field(gt=0)


class LiveOpenPreviewRequest(BaseModel):
    exchange: Exchange
    environment: ExchangeEnvironment
    base_asset: str = Field(min_length=1, max_length=40)
    notional_usdt: Decimal = Field(gt=0)
    leverage: int = Field(default=1, ge=1, le=10)
    maximum_slippage: Decimal = Field(
        default=Decimal("0.001"),
        gt=0,
        le=Decimal("0.1"),
    )


class LiveOpenConfirmRequest(BaseModel):
    preview_id: UUID
    confirmed: Literal[True]


class LiveClosePreviewRequest(BaseModel):
    maximum_slippage: Decimal = Field(
        default=Decimal("0.001"),
        gt=0,
        le=Decimal("0.1"),
    )


class LiveCloseConfirmRequest(BaseModel):
    preview_id: UUID
    confirmed: Literal[True]


def create_app(
    service: ScannerService | None = None,
    *,
    manage_lifecycle: bool = True,
    auth_required: bool | None = None,
    auth_service: AuthService | None = None,
    credential_service: CredentialService | None = None,
    account_client_factory: (
        Callable[
            [Exchange, ExchangeSecrets, ExchangeEnvironment],
            PrivateAccountClient,
        ]
        | None
    ) = None,
) -> FastAPI:
    config = get_config()
    scanner = service or ScannerService(
        Database(config.database_url), default_adapters(config.http_timeout_seconds)
    )
    require_auth = config.auth_required if auth_required is None else auth_required
    if account_client_factory is None:
        def default_account_client_factory(
            exchange: Exchange,
            secrets: ExchangeSecrets,
            environment: ExchangeEnvironment,
        ) -> PrivateAccountClient:
            return create_account_client(
                exchange,
                secrets,
                environment,
                timeout=config.http_timeout_seconds,
            )

        resolved_account_client_factory = default_account_client_factory
    else:
        resolved_account_client_factory = account_client_factory
    if config.credential_master_key:
        cipher = SecretCipher(config.credential_master_key.get_secret_value())
        if auth_service is None:
            auth_service = AuthService(
                scanner.database,
                cipher,
                session_hours=config.session_hours,
            )
        if credential_service is None:
            credential_service = CredentialService(scanner.database, cipher)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_lifecycle:
            await scanner.start()
        yield
        if manage_lifecycle:
            await scanner.stop()

    app = FastAPI(title="Basis Hawk", version="0.1.0", lifespan=lifespan)
    app.state.scanner = scanner
    app.state.auth_service = auth_service
    trade_ledger = TradeLedger(scanner.database)
    login_limiter = LoginAttemptLimiter()

    def request_actor(request: Request) -> str:
        admin = getattr(request.state, "admin", None)
        return admin.username if admin is not None else "local"

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        if not require_auth or not request.url.path.startswith("/api/"):
            return await call_next(request)
        if request.url.path in {
            "/api/auth/login",
            "/api/health/live",
            "/api/health/ready",
        }:
            return await call_next(request)
        if auth_service is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "credential master key is not configured"},
            )
        session_token = request.cookies.get(SESSION_COOKIE)
        admin = await auth_service.authenticate(session_token)
        if admin is None:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        request.state.admin = admin
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_cookie = request.cookies.get(CSRF_COOKIE)
            csrf_header = request.headers.get("X-CSRF-Token")
            if (
                not session_token
                or not csrf_cookie
                or csrf_cookie != csrf_header
                or not await auth_service.validate_csrf(session_token, csrf_header)
            ):
                return JSONResponse(status_code=403, content={"detail": "invalid CSRF token"})
        return await call_next(request)

    @app.post("/api/auth/login")
    async def login(value: LoginRequest, request: Request) -> Response:
        if not require_auth:
            raise HTTPException(status_code=409, detail="authentication is disabled")
        if auth_service is None:
            raise HTTPException(
                status_code=503,
                detail="credential master key is not configured",
            )
        remote_address = request.client.host if request.client else "unknown"
        limiter_key = f"{remote_address}:{value.username.strip()}"
        if not login_limiter.allowed(limiter_key):
            raise HTTPException(status_code=429, detail="too many login attempts")
        try:
            session = await auth_service.login(
                value.username,
                value.password,
                value.totp_code,
                remote_address=remote_address,
            )
        except AuthenticationError as exc:
            login_limiter.record_failure(limiter_key)
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        login_limiter.clear(limiter_key)
        response = JSONResponse(
            {
                "username": session.username,
                "expires_at": session.expires_at.isoformat(),
            }
        )
        max_age = config.session_hours * 3600
        response.set_cookie(
            SESSION_COOKIE,
            session.session_token,
            max_age=max_age,
            httponly=True,
            secure=config.secure_cookies,
            samesite="strict",
        )
        response.set_cookie(
            CSRF_COOKIE,
            session.csrf_token,
            max_age=max_age,
            httponly=False,
            secure=config.secure_cookies,
            samesite="strict",
        )
        return response

    @app.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, str]:
        if not require_auth:
            return {"username": "local"}
        return {"username": request.state.admin.username}

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        if not require_auth:
            return Response(status_code=204)
        if auth_service is None:
            raise HTTPException(status_code=503, detail="authentication is unavailable")
        await auth_service.logout(
            request.cookies.get(SESSION_COOKIE),
            actor=request.state.admin.username,
        )
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @app.get("/api/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready")
    async def ready() -> dict[str, object]:
        initialized = any(status.last_catalog_at for status in scanner.statuses.values())
        if not initialized:
            raise HTTPException(status_code=503, detail="market catalog is still initializing")
        return {"status": "ok", "exchanges": len(scanner.statuses)}

    @app.get("/api/opportunities")
    async def opportunities(
        exchange: Exchange | None = None,
        base_asset: str | None = None,
        quality: Quality | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=300),
    ) -> dict[str, object]:
        values = scanner.list_opportunities()
        if exchange:
            values = [item for item in values if item.exchange == exchange]
        if base_asset:
            query = base_asset.strip().upper()
            values = [item for item in values if query in item.base_asset]
        if quality:
            values = [item for item in values if item.quality == quality]
        start = (page - 1) * page_size
        return {
            "items": [item.model_dump(mode="json") for item in values[start : start + page_size]],
            "total": len(values),
            "page": page,
            "page_size": page_size,
            "sequence": scanner.sequence,
        }

    @app.get("/api/opportunities/{exchange}/{base_asset}/history")
    async def history(exchange: Exchange, base_asset: str, range: str = "24h") -> dict[str, object]:
        durations = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if range not in durations:
            raise HTTPException(status_code=422, detail="range must be 24h, 7d, or 30d")
        values = await scanner.database.snapshot_history(
            exchange.value,
            base_asset.upper(),
            since=datetime.now(UTC) - durations[range],
        )
        return {
            "exchange": exchange,
            "base_asset": base_asset.upper(),
            "range": range,
            "items": values,
        }

    @app.get("/api/exchanges/status")
    async def statuses() -> dict[str, object]:
        return {"items": [item.model_dump(mode="json") for item in scanner.statuses.values()]}

    @app.get("/api/system/execution")
    async def execution_status() -> dict[str, object]:
        control = await scanner.database.execution_control()
        reconciliations = await scanner.database.reconciliation_states()
        return {
            "state": control.state if control else "blocked",
            "reason": (
                control.reason
                if control
                else "execution worker has not completed startup reconciliation"
            ),
            "updated_at": control.updated_at.isoformat() if control else None,
            "accounts": [
                {
                    "exchange": item.exchange,
                    "environment": item.environment,
                    "status": item.status,
                    "reason": item.reason,
                    "trading_state_complete": item.trading_state_complete,
                    "order_reconciliation_complete": (
                        item.order_reconciliation_complete
                    ),
                    "fill_reconciliation_complete": (
                        item.fill_reconciliation_complete
                    ),
                    "private_stream_ready": item.private_stream_ready,
                    "open_order_count": item.open_order_count,
                    "position_count": item.position_count,
                    "fill_count": item.fill_count,
                    "recovered_order_count": item.recovered_order_count,
                    "checked_at": item.checked_at.isoformat(),
                }
                for item in reconciliations
            ],
        }

    @app.post("/api/trades/paper/open")
    async def plan_paper_open(
        value: PaperOpenRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        base_asset = value.base_asset.strip().upper()
        opportunity = scanner.opportunities.get(
            f"{value.exchange.value}:{base_asset}"
        )
        if opportunity is None:
            raise HTTPException(status_code=404, detail="opportunity is not available")
        try:
            intent, created = await trade_ledger.plan_paper_open(
                opportunity=opportunity,
                notional_usdt=value.notional_usdt,
                idempotency_key=idempotency_key,
                settings=scanner.settings,
            )
        except TradeValidationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            actor = (
                request.state.admin.username
                if getattr(request.state, "admin", None)
                else "local"
            )
            await scanner.database.append_audit(
                "trade.intent_planned",
                actor=actor,
                details={
                    "intent_id": intent.id,
                    "exchange": intent.exchange.value,
                    "environment": intent.environment,
                    "base_asset": intent.base_asset,
                    "action": intent.action,
                },
            )
        return {
            "created": created,
            "intent": intent.model_dump(mode="json"),
        }

    @app.post("/api/trades/open/preview")
    async def preview_live_open(
        value: LiveOpenPreviewRequest,
        request: Request,
    ) -> dict[str, object]:
        if credential_service is None:
            raise HTTPException(
                status_code=503,
                detail="credential encryption is unavailable",
            )
        if (
            await credential_service.load(value.exchange, value.environment)
            is None
        ):
            raise HTTPException(
                status_code=409,
                detail="exchange credential is not configured",
            )
        base_asset = value.base_asset.strip().upper()
        opportunity = scanner.opportunities.get(
            f"{value.exchange.value}:{base_asset}"
        )
        if opportunity is None:
            raise HTTPException(
                status_code=404,
                detail="opportunity is not available",
            )
        pair = scanner.instrument_pair(value.exchange, base_asset)
        if pair is None:
            raise HTTPException(
                status_code=409,
                detail="instrument trading rules are not available",
            )
        try:
            preview = trade_ledger.preview_live_open(
                opportunity=opportunity,
                pair=pair,
                notional_usdt=value.notional_usdt,
                settings=scanner.settings,
                environment=value.environment,
                leverage=value.leverage,
                maximum_slippage=value.maximum_slippage,
            )
        except TradeValidationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        preview_id = str(uuid4())
        actor = request_actor(request)
        created_at = datetime.now(UTC)
        await scanner.database.create_trade_preview(
            preview={
                "id": preview_id,
                "actor": actor,
                "request_fingerprint": preview.request_fingerprint,
                "action": "open",
                "paired_position_id": None,
                "exchange": preview.exchange.value,
                "environment": preview.environment.value,
                "base_asset": preview.base_asset,
                "requested_notional": preview.requested_notional,
                "leverage": preview.leverage,
                "maximum_slippage": preview.maximum_slippage,
                "market_observed_at": preview.market_observed_at,
                "confirmation_idempotency_key": None,
                "created_at": created_at,
                "expires_at": preview.expires_at,
                "confirmed_at": None,
            }
        )
        await scanner.database.append_audit(
            "trade.preview_created",
            actor=actor,
            details={
                "preview_id": preview_id,
                "exchange": preview.exchange.value,
                "environment": preview.environment.value,
                "base_asset": preview.base_asset,
                "expires_at": preview.expires_at.isoformat(),
            },
        )
        return {
            "preview_id": preview_id,
            "preview": preview.model_dump(mode="json"),
        }

    @app.post("/api/trades/open/confirm")
    async def confirm_live_open(
        value: LiveOpenConfirmRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        control = await scanner.database.execution_control()
        if control is None or control.state != "ready":
            raise HTTPException(
                status_code=409,
                detail="execution is not ready for live confirmation",
            )
        stored = await scanner.database.trade_preview(str(value.preview_id))
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail="trade preview was not found",
            )
        if stored.action != "open" or stored.paired_position_id is not None:
            raise HTTPException(
                status_code=409,
                detail="trade preview is not an opening preview",
            )
        exchange = Exchange(stored.exchange)
        environment = ExchangeEnvironment(stored.environment)
        if credential_service is None:
            raise HTTPException(
                status_code=503,
                detail="credential encryption is unavailable",
            )
        if await credential_service.load(exchange, environment) is None:
            raise HTTPException(
                status_code=409,
                detail="exchange credential is not configured",
            )
        opportunity = scanner.opportunities.get(
            f"{exchange.value}:{stored.base_asset}"
        )
        pair = scanner.instrument_pair(exchange, stored.base_asset)
        if opportunity is None or pair is None:
            raise HTTPException(
                status_code=409,
                detail="current market or trading rules are not available",
            )
        try:
            current_preview = trade_ledger.preview_live_open(
                opportunity=opportunity,
                pair=pair,
                notional_usdt=stored.requested_notional,
                settings=scanner.settings,
                environment=environment,
                leverage=stored.leverage,
                maximum_slippage=stored.maximum_slippage,
            )
            await scanner.database.reserve_trade_preview(
                preview_id=stored.id,
                actor=request_actor(request),
                request_fingerprint=current_preview.request_fingerprint,
                idempotency_key=str(idempotency_key),
            )
            intent, created = await trade_ledger.plan_live_open(
                opportunity=opportunity,
                pair=pair,
                notional_usdt=stored.requested_notional,
                idempotency_key=idempotency_key,
                settings=scanner.settings,
                environment=environment,
                leverage=stored.leverage,
                maximum_slippage=stored.maximum_slippage,
            )
        except (
            IdempotencyConflict,
            TradeValidationError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            await scanner.database.append_audit(
                "trade.intent_confirmed",
                actor=request_actor(request),
                details={
                    "preview_id": stored.id,
                    "intent_id": intent.id,
                    "exchange": intent.exchange.value,
                    "environment": intent.environment,
                    "base_asset": intent.base_asset,
                    "action": intent.action,
                },
            )
        return {
            "created": created,
            "intent": intent.model_dump(mode="json"),
        }

    @app.post("/api/trades/positions/{position_id}/close/preview")
    async def preview_live_close(
        position_id: UUID,
        value: LiveClosePreviewRequest,
        request: Request,
    ) -> dict[str, object]:
        position = await scanner.database.paired_position(str(position_id))
        if position is None:
            raise HTTPException(
                status_code=404,
                detail="paired position was not found",
            )
        if position.environment not in {
            ExchangeEnvironment.SANDBOX.value,
            ExchangeEnvironment.LIVE.value,
        }:
            raise HTTPException(
                status_code=409,
                detail="paired position is not exchange-backed",
            )
        exchange = Exchange(position.exchange)
        environment = ExchangeEnvironment(position.environment)
        if credential_service is None:
            raise HTTPException(
                status_code=503,
                detail="credential encryption is unavailable",
            )
        if await credential_service.load(exchange, environment) is None:
            raise HTTPException(
                status_code=409,
                detail="exchange credential is not configured",
            )
        opportunity = scanner.opportunities.get(
            f"{exchange.value}:{position.base_asset}"
        )
        pair = scanner.instrument_pair(exchange, position.base_asset)
        if opportunity is None or pair is None:
            raise HTTPException(
                status_code=409,
                detail="current market or trading rules are not available",
            )
        try:
            preview = await trade_ledger.preview_live_close(
                position_id=position.id,
                opportunity=opportunity,
                pair=pair,
                settings=scanner.settings,
                environment=environment,
                maximum_slippage=value.maximum_slippage,
            )
        except TradeValidationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        preview_id = str(uuid4())
        actor = request_actor(request)
        created_at = datetime.now(UTC)
        await scanner.database.create_trade_preview(
            preview={
                "id": preview_id,
                "actor": actor,
                "request_fingerprint": preview.request_fingerprint,
                "action": "close",
                "paired_position_id": position.id,
                "exchange": preview.exchange.value,
                "environment": preview.environment.value,
                "base_asset": preview.base_asset,
                "requested_notional": (
                    preview.spot_quantity * preview.spot_limit_price
                ),
                "leverage": preview.leverage,
                "maximum_slippage": preview.maximum_slippage,
                "market_observed_at": preview.market_observed_at,
                "confirmation_idempotency_key": None,
                "created_at": created_at,
                "expires_at": preview.expires_at,
                "confirmed_at": None,
            }
        )
        await scanner.database.append_audit(
            "trade.close_preview_created",
            actor=actor,
            details={
                "preview_id": preview_id,
                "position_id": position.id,
                "exchange": preview.exchange.value,
                "environment": preview.environment.value,
                "base_asset": preview.base_asset,
                "expires_at": preview.expires_at.isoformat(),
            },
        )
        return {
            "preview_id": preview_id,
            "preview": preview.model_dump(mode="json"),
        }

    @app.post("/api/trades/positions/{position_id}/close/confirm")
    async def confirm_live_close(
        position_id: UUID,
        value: LiveCloseConfirmRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        control = await scanner.database.execution_control()
        if control is None or control.state != "ready":
            raise HTTPException(
                status_code=409,
                detail="execution is not ready for live confirmation",
            )
        stored = await scanner.database.trade_preview(str(value.preview_id))
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail="trade preview was not found",
            )
        if (
            stored.action != "close"
            or stored.paired_position_id != str(position_id)
        ):
            raise HTTPException(
                status_code=409,
                detail="trade preview does not match the closing position",
            )
        exchange = Exchange(stored.exchange)
        environment = ExchangeEnvironment(stored.environment)
        if credential_service is None:
            raise HTTPException(
                status_code=503,
                detail="credential encryption is unavailable",
            )
        if await credential_service.load(exchange, environment) is None:
            raise HTTPException(
                status_code=409,
                detail="exchange credential is not configured",
            )
        opportunity = scanner.opportunities.get(
            f"{exchange.value}:{stored.base_asset}"
        )
        pair = scanner.instrument_pair(exchange, stored.base_asset)
        if opportunity is None or pair is None:
            raise HTTPException(
                status_code=409,
                detail="current market or trading rules are not available",
            )
        try:
            if stored.confirmation_idempotency_key is None:
                current_preview = await trade_ledger.preview_live_close(
                    position_id=str(position_id),
                    opportunity=opportunity,
                    pair=pair,
                    settings=scanner.settings,
                    environment=environment,
                    maximum_slippage=stored.maximum_slippage,
                )
                fingerprint = current_preview.request_fingerprint
            else:
                fingerprint = stored.request_fingerprint
            await scanner.database.reserve_trade_preview(
                preview_id=stored.id,
                actor=request_actor(request),
                request_fingerprint=fingerprint,
                idempotency_key=str(idempotency_key),
            )
            intent, created = await trade_ledger.plan_live_close(
                position_id=str(position_id),
                opportunity=opportunity,
                pair=pair,
                idempotency_key=idempotency_key,
                settings=scanner.settings,
                environment=environment,
                maximum_slippage=stored.maximum_slippage,
            )
        except (
            IdempotencyConflict,
            TradeValidationError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            await scanner.database.append_audit(
                "trade.close_intent_confirmed",
                actor=request_actor(request),
                details={
                    "preview_id": stored.id,
                    "position_id": str(position_id),
                    "intent_id": intent.id,
                    "exchange": intent.exchange.value,
                    "environment": intent.environment,
                    "base_asset": intent.base_asset,
                },
            )
        return {
            "created": created,
            "intent": intent.model_dump(mode="json"),
        }

    @app.get("/api/trades/intents/{intent_id}")
    async def trade_intent(intent_id: UUID) -> dict[str, object]:
        intent = await trade_ledger.get(str(intent_id))
        if intent is None:
            raise HTTPException(status_code=404, detail="trade intent was not found")
        return {"intent": intent.model_dump(mode="json")}

    @app.get("/api/trades/intents/{intent_id}/fills")
    async def trade_fills(intent_id: UUID) -> dict[str, object]:
        intent = await trade_ledger.get(str(intent_id))
        if intent is None:
            raise HTTPException(status_code=404, detail="trade intent was not found")
        return {
            "items": [
                item.model_dump(mode="json")
                for item in await trade_ledger.fills(str(intent_id))
            ]
        }

    @app.get("/api/trades/positions")
    async def paired_positions(status: str | None = None) -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in await trade_ledger.positions(status=status)
            ]
        }

    @app.post("/api/trades/paper/positions/{position_id}/close")
    async def plan_paper_close(
        position_id: UUID,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        position = await trade_ledger.position(str(position_id))
        if position is None:
            raise HTTPException(status_code=404, detail="paired position was not found")
        opportunity = scanner.opportunities.get(
            f"{position.exchange.value}:{position.base_asset}"
        )
        if opportunity is None:
            raise HTTPException(status_code=404, detail="opportunity is not available")
        try:
            intent, created = await trade_ledger.plan_paper_close(
                position_id=position.id,
                opportunity=opportunity,
                idempotency_key=idempotency_key,
                settings=scanner.settings,
            )
        except (TradeValidationError, IdempotencyConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created:
            actor = (
                request.state.admin.username
                if getattr(request.state, "admin", None)
                else "local"
            )
            await scanner.database.append_audit(
                "trade.intent_planned",
                actor=actor,
                details={
                    "intent_id": intent.id,
                    "position_id": position.id,
                    "exchange": intent.exchange.value,
                    "environment": intent.environment,
                    "base_asset": intent.base_asset,
                    "action": intent.action,
                },
            )
        return {
            "created": created,
            "intent": intent.model_dump(mode="json"),
        }

    @app.get("/api/accounts/credentials")
    async def credential_summaries() -> dict[str, object]:
        if credential_service is None:
            raise HTTPException(status_code=503, detail="credential encryption is unavailable")
        return {
            "items": [
                {
                    "exchange": item.exchange,
                    "environment": item.environment,
                    "label": item.label,
                    "masked_api_key": item.masked_api_key,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in await credential_service.list()
            ]
        }

    @app.put("/api/accounts/{exchange}/{environment}/credentials")
    async def save_credentials(
        exchange: Exchange,
        environment: ExchangeEnvironment,
        value: CredentialRequest,
        request: Request,
    ) -> dict[str, object]:
        if credential_service is None:
            raise HTTPException(status_code=503, detail="credential encryption is unavailable")
        try:
            summary = await credential_service.save(
                exchange=exchange,
                environment=environment,
                label=value.label,
                secrets=ExchangeSecrets(
                    api_key=value.api_key.get_secret_value(),
                    api_secret=value.api_secret.get_secret_value(),
                    passphrase=(
                        value.passphrase.get_secret_value()
                        if value.passphrase is not None
                        else None
                    ),
                ),
                actor=getattr(request.state, "admin", None).username
                if getattr(request.state, "admin", None)
                else "local",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "exchange": summary.exchange,
            "environment": summary.environment,
            "label": summary.label,
            "masked_api_key": summary.masked_api_key,
            "updated_at": summary.updated_at.isoformat(),
        }

    @app.delete(
        "/api/accounts/{exchange}/{environment}/credentials",
        status_code=204,
    )
    async def delete_credentials(
        exchange: Exchange,
        environment: ExchangeEnvironment,
        request: Request,
    ) -> Response:
        if credential_service is None:
            raise HTTPException(status_code=503, detail="credential encryption is unavailable")
        deleted = await credential_service.delete(
            exchange,
            environment,
            actor=getattr(request.state, "admin", None).username
            if getattr(request.state, "admin", None)
            else "local",
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="credential is not configured")
        return Response(status_code=204)

    @app.get("/api/accounts/{exchange}/{environment}/snapshot")
    async def account_snapshot(
        exchange: Exchange,
        environment: ExchangeEnvironment,
    ) -> dict[str, object]:
        if credential_service is None:
            raise HTTPException(status_code=503, detail="credential encryption is unavailable")
        secrets = await credential_service.load(exchange, environment)
        if secrets is None:
            raise HTTPException(status_code=404, detail="credential is not configured")
        try:
            client = resolved_account_client_factory(exchange, secrets, environment)
        except (UnsupportedEnvironmentError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            snapshot = await client.snapshot()
        except (
            ArithmeticError,
            KeyError,
            PrivateRequestError,
            TypeError,
            ValueError,
        ) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"{exchange.value} private account probe failed",
            ) from exc
        finally:
            await client.close()
        return snapshot.model_dump(mode="json")

    @app.get("/api/settings", response_model=ScannerSettings)
    async def settings() -> ScannerSettings:
        return scanner.settings

    @app.put("/api/settings", response_model=ScannerSettings)
    async def update_settings(value: ScannerSettings) -> ScannerSettings:
        return await scanner.update_settings(value)

    @app.websocket("/api/ws/opportunities")
    async def opportunity_stream(websocket: WebSocket) -> None:
        if require_auth:
            if auth_service is None or await auth_service.authenticate(
                websocket.cookies.get(SESSION_COOKIE)
            ) is None:
                await websocket.close(code=4401)
                return
        await websocket.accept()
        queue = scanner.subscribe()
        await websocket.send_json(
            {
                "type": "snapshot",
                "sequence": scanner.sequence,
                "items": [item.model_dump(mode="json") for item in scanner.list_opportunities()],
            }
        )
        try:
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            scanner.unsubscribe(queue)

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def single_page(path: str) -> FileResponse:
            candidate = frontend / path
            return FileResponse(
                candidate if path and candidate.is_file() else frontend / "index.html"
            )

    return app


app = create_app()
