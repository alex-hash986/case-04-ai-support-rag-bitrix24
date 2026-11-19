import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from filelock import FileLock

from src.core.config import Settings
from src.core.database import Database
from src.core.llm_gateway import BudgetExceeded, LLMGateway
from src.domain.ports import BitrixOutbox
from src.domain.retriever import HybridRetriever
from src.domain.schemas import (
    ChatMessageIn,
    ConfirmClosureIn,
    IndexResult,
    MetricGroup,
    ReconcileResult,
    SupportDecisionOut,
)
from src.domain.service import IdempotencyConflict, SupportService
from src.integrations.bitrix_connector import MockBitrixConnector
from src.integrations.bitrix_events import authorized, event_payload, normalize
from src.integrations.bitrix_live import BitrixLiveAdapter, chat_id


def service(request: Request) -> SupportService:
    instance: SupportService = request.app.state.support
    return instance


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        path = config.database_path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(f"{path}.publisher.lock", timeout=0):
            database = Database(path)
            async with httpx.AsyncClient(timeout=30, transport=transport) as client:
                gateway = LLMGateway(config, client)
                retriever = HybridRetriever(config.knowledge_base_path, gateway, database)
                live = BitrixLiveAdapter(config, client) if config.bitrix_mode == "live" else None
                outbox = BitrixOutbox(database, MockBitrixConnector(database), live)
                instance = SupportService(config, database, gateway, retriever, outbox)
                app.state.support = instance
                # Live indexing is explicit to avoid spending provider budget on startup.
                if config.llm_mode == "mock":
                    await instance.reindex()
                else:
                    try:
                        await retriever.load_cached()
                    except ValueError:
                        pass
                yield

    app = FastAPI(title="Case 04 · AI Support", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str | bool]:
        instance = service(request)
        with instance.database.connect() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok", "index_ready": bool(instance.retriever.index)}

    @app.post("/api/v1/support/message")
    async def message(
        payload: ChatMessageIn, request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
        ],
    ) -> SupportDecisionOut:
        instance = service(request)
        if instance.settings.bitrix_mode == "live":
            try:
                chat_id(payload.dialog_id)
            except ValueError as error:
                raise HTTPException(422, "Invalid OpenLines dialog_id") from error
        try:
            return await instance.message(idempotency_key, payload)
        except IdempotencyConflict as error:
            raise HTTPException(409, "Idempotency-Key is already bound to another payload") from error

    @app.post("/api/v1/bitrix/openlines/webhook")
    async def webhook(request: Request) -> SupportDecisionOut | dict[str, bool]:
        instance = service(request)
        token = instance.settings.bitrix_app_token
        try:
            body = await request.body()
            if len(body) > 262_144:
                raise HTTPException(413, "Event too large")
            event = event_payload(body, request.headers.get("content-type", ""))
            auth = event.get("auth")
            submitted = auth.get("application_token") if isinstance(auth, dict) else None
            if token is None or not authorized(token.get_secret_value(), submitted):
                raise HTTPException(403, "Invalid application token")
            normalized = normalize(event, instance.settings.bitrix_bot_id)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise HTTPException(422, "Invalid OpenLines event") from error
        if normalized is None:
            return {"ignored": True}
        try:
            return await instance.message(*normalized)
        except IdempotencyConflict as error:
            raise HTTPException(409, "Event conflicts with the saved message") from error

    @app.post("/api/v1/support/confirm-closure")
    async def confirm_closure(
        payload: ConfirmClosureIn, request: Request,
        token: Annotated[str | None, Header(alias="X-Bitrix-App-Token")] = None,
    ) -> dict[str, bool]:
        instance = service(request)
        expected = instance.settings.bitrix_app_token
        if expected is None or not authorized(expected.get_secret_value(), token):
            raise HTTPException(403, "Invalid application token")
        if not await instance.confirm_closure(payload.request_id):
            raise HTTPException(409, "Closure is ineligible or baseline is unset")
        return {"confirmed": True}

    @app.post("/api/v1/support/reconcile")
    async def reconcile(request: Request) -> ReconcileResult:
        return await service(request).reconcile()

    @app.post("/api/v1/kb/reindex")
    async def reindex(request: Request) -> IndexResult:
        try:
            return await service(request).reindex()
        except BudgetExceeded as error:
            raise HTTPException(409, "Indexing budget exceeded or token rates missing") from error
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise HTTPException(503, "Knowledge base indexing failed") from error

    @app.get("/api/v1/metrics")
    async def metrics(request: Request) -> list[MetricGroup]:
        return service(request).metrics()

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-llm", action="store_true")
    args = parser.parse_args()
    settings = Settings(llm_mode="mock") if args.mock_llm else Settings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.api_port)


if __name__ == "__main__":
    main()

