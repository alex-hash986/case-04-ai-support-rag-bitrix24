import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from src.api.main import create_app
from src.core.config import ROOT, Settings
from src.core.llm_gateway import CycleUsage, LLMGateway
from src.domain.ports import BitrixPort
from src.domain.service import SupportService
from src.ui.presets import PRESETS

PAYLOAD = {
    "session_id": "session", "dialog_id": "chat35", "user_id": "42",
    "channel": "telegram", "deal_id": 12, "message_text": PRESETS["Тарифы и SLA"],
}
MESSAGE = "/api/v1/support/message"


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "support.db"


@pytest.fixture
def client(database_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_path=database_path, llm_mode="mock"))) as api:
        yield api


def send(client: TestClient, key: str = "test", **updates: object) -> httpx.Response:
    return cast(httpx.Response, client.post(MESSAGE, headers={"Idempotency-Key": key}, json=PAYLOAD | updates))


def test_hybrid_auto_reply_and_metrics(client: TestClient, database_path: Path) -> None:
    assert client.get("/health").json() == {"status": "ok", "index_ready": True}
    assert client.post("/api/v1/kb/reindex").json()["chunks"] == 11
    decision = send(client).json()
    assert decision["action"] == "auto_reply"
    assert decision["bitrix_sync_status"] == "synced"
    assert "2 900 ₽" in decision["reply_text"]
    assert decision["citations"][0]["doc_id"] == "tariffs_and_billing"
    assert decision["retrieved_chunks"][0]["score"] > 0.45
    assert client.get("/api/v1/metrics").json()[0]["deflection_rate"] == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT method FROM bitrix_steps ORDER BY ordinal",
        ).fetchall() == [("imbot.message.add",), ("crm.timeline.comment.add",)]
        assert connection.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0] == 11


def test_sla_reply_uses_sla_source(client: TestClient) -> None:
    response = send(client, message_text="Какое время первого ответа по SLA на критический инцидент?")
    decision = response.json()
    assert decision["action"] == "auto_reply"
    assert "2 рабочих часа" in decision["reply_text"]
    assert decision["citations"][0]["doc_id"] == "sla_and_support"


@pytest.mark.parametrize(("question", "reason"), [
    (PRESETS["Вне базы знаний"], "low_retrieval_score"),
    (PRESETS["Жалоба и возврат"], "complaint_or_refund"),
    ("Я хочу поговорить с оператором", "operator_requested"),
    ("Это отвратительно!", "complaint_or_refund"),
    ("Оператора, пожалуйста!", "operator_requested"),
])
def test_escalation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, question: str, reason: str,
) -> None:
    service: SupportService = cast(FastAPI, client.app).state.support

    async def unexpected(*args: object) -> None:
        pytest.fail("Explicit escalation must bypass retrieval and all escalations bypass generation")

    monkeypatch.setattr(service, "_draft", unexpected)
    if reason != "low_retrieval_score":
        monkeypatch.setattr(service.retriever, "search", unexpected)
    data = send(client, message_text=question, deal_id=None).json()
    assert data["action"] == "escalate_to_operator"
    assert data["escalation_reason"] == reason
    assert data["need_human"] is True
    assert data["summary_for_operator"]
    assert data["bitrix_sync_status"] == "synced"


def test_idempotency_concurrent_and_validation(client: TestClient, database_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(client), range(2)))
    assert responses[0].json() == responses[1].json()
    assert send(client, message_text="Другой текст").status_code == 409
    assert send(client, "invalid", extra_field=True).status_code == 422
    assert client.post(MESSAGE, json=PAYLOAD).status_code == 422
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 2


def test_reconcile_after_ambiguous_delivery_and_restart(database_path: Path) -> None:
    settings = Settings(database_path=database_path, llm_mode="mock")
    with TestClient(create_app(settings)) as client:
        service = cast(FastAPI, client.app).state.support
        adapter: BitrixPort = service.outbox.adapter

        class InterruptedDelivery:
            async def execute(self, operation_id: str, method: str, payload: str) -> None:
                await adapter.execute(operation_id, method, payload)
                raise TimeoutError

        service.outbox.adapter = InterruptedDelivery()
        assert send(client).json()["bitrix_sync_status"] == "pending"
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 1
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/support/reconcile")
        assert response.json() == {"synced": 1, "pending": 0}
        assert send(client).json()["bitrix_sync_status"] == "synced"
        assert client.post("/api/v1/support/reconcile").json()["synced"] == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 2
        assert connection.execute("SELECT SUM(attempts) FROM bitrix_steps").fetchone()[0] == 3


def live_settings(path: Path, **overrides: object) -> Settings:
    return Settings.model_validate({
        "database_path": path, "llm_mode": "live",
        "embedding_model": "bge-m3", "smart_llm_model": "qwen2.5:32b",
        "embedding_rub_per_million_input_tokens": Decimal(0),
        "llm_rub_per_million_input_tokens": Decimal(0),
        "llm_rub_per_million_output_tokens": Decimal(0),
    } | overrides)


@pytest.mark.parametrize("invented", [
    "Тариф «Старт» стоит 99 ₽ в месяц.",
    "Тариф «Старт» стоит 2 900 ₽ в месяц. Скидка составляет 15%.",
    "Тариф «Старт» стоит 2 900 ₽ в месяц. По SLA ответят за 24 часа.",
])
def test_grounding_rejects_invented_numbers(database_path: Path, invented: str) -> None:
    completions = []

    def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert body["model"] == "bge-m3"
            return httpx.Response(200, json={
                "usage": {"prompt_tokens": 8}, "data": [{"embedding": [1, 0]}],
            })
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "qwen2.5:32b"
        context = json.loads(body["messages"][1]["content"])["context"]
        completions.append(body)
        draft = {
            "need_human": False, "reply_text": invented,
            "citation_ids": [context[0]["chunk_id"]], "extra_from_provider": True,
        }
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 50, "completion_tokens": 15},
            "choices": [{"message": {"content": "```json\n" + json.dumps(draft) + "\n```"}}],
        })

    app = create_app(live_settings(database_path), transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        assert client.get("/health").json()["index_ready"] is False
        assert client.post("/api/v1/kb/reindex").status_code == 200
        result = send(client).json()
        assert result["escalation_reason"] == "grounding_failed"
        assert result["reply_text"] == ""
        assert len(completions) == 1


@pytest.mark.parametrize("limit", ["max_cycle_cost_rub", "max_session_cost_rub"])
def test_budget_stops_generation_before_network(database_path: Path, limit: str) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings"), "Paid generation must not be called"
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 8}, "data": [{"embedding": [1, 0]}],
        })

    settings = live_settings(database_path, **{
        limit: Decimal(0), "llm_rub_per_million_output_tokens": Decimal(1),
    })
    with TestClient(create_app(settings, transport=httpx.MockTransport(provider))) as client:
        assert client.post("/api/v1/kb/reindex").status_code == 200
        assert send(client).json()["escalation_reason"] == "budget_exceeded"


def test_cost_rounding_and_tokens(database_path: Path) -> None:
    usage = CycleUsage(cost_rub=Decimal("0.005"))
    assert usage.rounded_cost == Decimal("0.01")

    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 123}, "data": [{"embedding": [1, 0]}],
        })

    settings = live_settings(database_path, embedding_rub_per_million_input_tokens=Decimal(1))
    with TestClient(create_app(settings, transport=httpx.MockTransport(provider))) as client:
        service: SupportService = cast(FastAPI, client.app).state.support
        assert isinstance(service.gateway, LLMGateway)
        assert client.post("/api/v1/kb/reindex").status_code == 200
        result = send(client, message_text=PRESETS["Вне базы знаний"]).json()
        assert result["cost_complete"]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT cost_rub, tokens_in FROM cycles").fetchone() == (
            "0.000123", 123,
        )


def test_streamlit_live_demo_and_dashboard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def post(url: str, **kwargs: Any) -> httpx.Response:
        return cast(httpx.Response, client.post(httpx.URL(url).path,
                                         json=kwargs["json"], headers=kwargs["headers"]))

    def get(url: str, **kwargs: Any) -> httpx.Response:
        return cast(httpx.Response, client.get(httpx.URL(url).path))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    app = AppTest.from_file(str(ROOT / "src/ui/app.py"), default_timeout=10).run()
    assert [tab.label for tab in app.tabs] == ["Live Demo", "Executive ROI-дашборд"]
    app.button[0].click().run()
    assert not app.exception
    assert len(app.chat_message) == 2
    assert app.expander[0].label == "Найденные фрагменты"
    assert app.metric[0].value == "100.0%"

