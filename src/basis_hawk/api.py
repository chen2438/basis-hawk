from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from basis_hawk.config import get_config
from basis_hawk.models import Exchange, Quality, ScannerSettings
from basis_hawk.service import ScannerService, default_adapters
from basis_hawk.storage import Database


def create_app(service: ScannerService | None = None, *, manage_lifecycle: bool = True) -> FastAPI:
    config = get_config()
    scanner = service or ScannerService(
        Database(config.database_url), default_adapters(config.http_timeout_seconds)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_lifecycle:
            await scanner.start()
        yield
        if manage_lifecycle:
            await scanner.stop()

    app = FastAPI(title="Basis Hawk", version="0.1.0", lifespan=lifespan)
    app.state.scanner = scanner

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

    @app.get("/api/settings", response_model=ScannerSettings)
    async def settings() -> ScannerSettings:
        return scanner.settings

    @app.put("/api/settings", response_model=ScannerSettings)
    async def update_settings(value: ScannerSettings) -> ScannerSettings:
        return await scanner.update_settings(value)

    @app.websocket("/api/ws/opportunities")
    async def opportunity_stream(websocket: WebSocket) -> None:
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
