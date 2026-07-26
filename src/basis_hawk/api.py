from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

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


def create_app(
    service: ScannerService | None = None,
    *,
    manage_lifecycle: bool = True,
    auth_required: bool | None = None,
    auth_service: AuthService | None = None,
    credential_service: CredentialService | None = None,
) -> FastAPI:
    config = get_config()
    scanner = service or ScannerService(
        Database(config.database_url), default_adapters(config.http_timeout_seconds)
    )
    require_auth = config.auth_required if auth_required is None else auth_required
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
    login_limiter = LoginAttemptLimiter()

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
