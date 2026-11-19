import asyncio
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ops.runners.benchmark_ab import benchmark, load_cases, write_report
from src.api.main import create_app
from src.core.config import Settings
from src.core.database import Database
from src.domain.guardrails import grounded
from src.domain.retriever import read_chunks
from src.domain.schemas import Draft


def test_dataset_schema_and_balance() -> None:
    cases = load_cases()
    assert len(cases) == len({case.case_id for case in cases}) == 60
    assert Counter(case.category for case in cases) == {
        "auto_reply": 24, "out_of_scope": 14, "sentiment_legal_human": 12, "adversarial": 10,
    }
    doc_ids = {chunk.doc_id for chunk in read_chunks(Settings().knowledge_base_path)}
    assert {doc for case in cases for doc in case.expected_doc_ids} == doc_ids
    assert any(case.input.history for case in cases)
    for case in cases:
        auto = case.expected_action == "auto_reply"
        assert auto == bool(case.expected_doc_ids)
        assert auto == (case.expected_escalation_reason is None)


def test_mock_benchmark(tmp_path: Path) -> None:
    report = asyncio.run(benchmark(Settings(llm_mode="mock")))
    for variant, summary in report.variants.items():
        assert summary.escalation_recall == summary.action_accuracy == 1
        assert summary.grounding_pass_rate == summary.faithfulness == summary.citation_precision == 1
        assert summary.hallucination_rate == 0
        assert summary.avg_cost_rub == Decimal(0)
        assert len(report.results[variant]) == 60
        assert all(item.escalation_reason == item.expected_escalation_reason for item in report.results[variant])
    path = tmp_path / "ab.json"
    write_report(report, path)
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "mock"
    assert "Escalation Recall" in path.with_suffix(".md").read_text(encoding="utf-8")


def test_structured_numeric_guard_rejects_altered_amount() -> None:
    chunk = next(item for item in read_chunks(Settings().knowledge_base_path) if "2 900 ₽" in item.text)
    draft = Draft(
        need_human=False, reply_text="Здравствуйте! По базе знаний:\n• Тариф «Старт» стоит 1 000 ₽ в месяц.",
        citation_ids=[chunk.chunk_id],
    )
    assert grounded(draft, [chunk], structured=True) is None


def live_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "live.db", llm_mode="mock", bitrix_mode="live",
        bitrix_webhook_url=SecretStr("https://bitrix.example/rest/1/sample"),
        bitrix_bot_id=41, bitrix_app_token=SecretStr("event-token"),
        bitrix_client_id=SecretStr("registered-bot-client"),
        baseline_manual_minutes=Decimal("4.5"),
    )


def make_event(text: str, message_id: int, *, deal_id: int = 23) -> dict[str, str]:
    return {
        "event": "ONIMBOTMESSAGEADD",
        "auth[application_token]": "event-token",
        "data[PARAMS][MESSAGE]": text,
        "data[PARAMS][MESSAGE_ID]": str(message_id),
        "data[PARAMS][DIALOG_ID]": "chat345",
        "data[PARAMS][CHAT_ENTITY_TYPE]": "LINES",
        "data[PARAMS][CHAT_ENTITY_ID]": "telegrambot|1|345|17",
        "data[PARAMS][CHAT_ENTITY_DATA_1]": f"Y|DEAL|{deal_id}|N|N|678",
        "data[PARAMS][FROM_USER_ID]": "17",
    }


def webhook(client: TestClient, form: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/bitrix/openlines/webhook", content=urlencode(form),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return response


def test_live_webhook_reconcile_remote_receipt_and_confirmed_roi(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []
    comments: list[dict[str, object]] = []

    def portal(request: httpx.Request) -> httpx.Response:
        method = request.url.path.split("/")[-1].removesuffix(".json")
        params = json.loads(request.content)
        calls.append((method, params, monotonic()))
        if method == "crm.timeline.comment.add":
            comments.append({"ID": "501", "COMMENT": params["fields"]["COMMENT"]})
            raise httpx.ReadTimeout("Portal committed the comment", request=request)
        if method == "crm.timeline.comment.list":
            assert params["filter"] == {"ENTITY_TYPE": "deal", "ENTITY_ID": 23}
            return httpx.Response(200, json={"result": comments})
        if method == "imbot.message.add":
            assert params["BOT_ID"] == 41
            assert params["CLIENT_ID"] == "registered-bot-client"
            assert params["DIALOG_ID"] == "chat345"
            return httpx.Response(200, json={"result": 201})
        if method == "imopenlines.bot.session.operator":
            assert params == {"CHAT_ID": 345}
            return httpx.Response(200, json={"result": True})
        raise AssertionError(method)

    settings = live_settings(tmp_path)
    transport = httpx.MockTransport(portal)
    form = make_event("Сколько стоит тариф Старт в месяц?", 1)
    with TestClient(create_app(settings, transport=transport)) as api:
        wrong_token = {**form, "auth[application_token]": "incorrect"}
        assert webhook(api, wrong_token).status_code == 403
        assert webhook(api, {**form, "data[USER][IS_BOT]": "Y"}).json() == {"ignored": True}
        first = webhook(api, form)
        assert first.status_code == 200
        decision = first.json()
        assert decision["bitrix_sync_status"] == "pending"
        assert webhook(api, form).json()["request_id"] == decision["request_id"]
        assert len(calls) == 2

    with TestClient(create_app(settings, transport=transport)) as api:
        assert api.post("/api/v1/support/reconcile").json() == {"synced": 1, "pending": 0}
        assert api.post("/api/v1/support/reconcile").json() == {"synced": 0, "pending": 0}
        assert webhook(api, form).json()["bitrix_sync_status"] == "synced"
        with Database(settings.database_path).connect() as connection:
            receipts = connection.execute("SELECT receipt FROM bitrix_steps ORDER BY ordinal").fetchall()
            assert [row["receipt"] for row in receipts] == ["201", "501"]
        assert api.get("/api/v1/metrics").json()[0]["saved_minutes"] is None
        for _ in range(2):
            confirmed = api.post(
                "/api/v1/support/confirm-closure", json={"request_id": decision["request_id"]},
                headers={"X-Bitrix-App-Token": "event-token"},
            )
            assert confirmed.json() == {"confirmed": True}
        metrics = api.get("/api/v1/metrics").json()[0]
        assert metrics["saved_minutes"] == "4.5"
        assert metrics["confirmed_auto_resolutions"] == 1
        # A repeat escalation clears ROI for the same OpenLines session.
        escalation = make_event("Позовите оператора", 2)
        del escalation["data[PARAMS][CHAT_ENTITY_DATA_1]"]
        escalation["data[PARAMS][SESSION_ID]"] = "678"
        assert webhook(api, escalation).json()["action"] == "escalate_to_operator"
        assert api.get("/api/v1/metrics").json()[0]["saved_minutes"] is None
    assert [name for name, _, _ in calls] == [
        "imbot.message.add", "crm.timeline.comment.add", "crm.timeline.comment.list",
        "imopenlines.bot.session.operator", "imbot.message.add",
    ]
    assert calls[-1][1]["SYSTEM"] == calls[-1][1]["SKIP_CONNECTOR"] == "Y"
    assert all(b[2] - a[2] >= 0.5 for a, b in zip(calls, calls[1:]))


def test_live_retries_explicit_rate_limit_and_checks_ambiguous_chat_write(tmp_path: Path) -> None:
    calls: list[str] = []
    message = ""

    def portal(request: httpx.Request) -> httpx.Response:
        nonlocal message
        method = request.url.path.split("/")[-1].removesuffix(".json")
        calls.append(method)
        if len(calls) == 1:
            return httpx.Response(429)
        if len(calls) == 2:
            return httpx.Response(503, json={"error": "QUERY_LIMIT_EXCEEDED"})
        if method == "imbot.message.add":
            message = json.loads(request.content)["MESSAGE"]
            return httpx.Response(503)
        assert method == "imopenlines.session.history.get"
        assert json.loads(request.content) == {"SESSION_ID": 678}
        if len(calls) <= 6:
            return httpx.Response(503)
        return httpx.Response(200, json={"result": {"message": {"109": {"id": "109", "text": message}}}})

    with TestClient(create_app(live_settings(tmp_path), transport=httpx.MockTransport(portal))) as api:
        form = make_event("Сколько стоит тариф Команда в месяц?", 1)
        del form["data[PARAMS][CHAT_ENTITY_DATA_1]"]
        form["data[PARAMS][SESSION_ID]"] = "678"
        assert webhook(api, form).json()["bitrix_sync_status"] == "pending"
        assert api.post("/api/v1/support/reconcile").json() == {"synced": 0, "pending": 1}
        assert api.post("/api/v1/support/reconcile").json() == {"synced": 1, "pending": 0}
    assert calls == ["imbot.message.add"] * 3 + ["imopenlines.session.history.get"] * 4

