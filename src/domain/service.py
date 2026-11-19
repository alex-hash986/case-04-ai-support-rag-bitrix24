import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from src.core.config import ROOT, Settings
from src.core.database import Database
from src.core.llm_gateway import (
    BudgetExceeded,
    CycleUsage,
    LLMGateway,
    known_fields,
    rubles,
)
from src.domain.guardrails import escalation_trigger, grounded, mock_draft
from src.domain.ports import BitrixOutbox
from src.domain.retriever import HybridRetriever
from src.domain.schemas import (
    ChatMessageIn,
    Draft,
    IndexResult,
    MetricGroup,
    ReconcileResult,
    RetrievedChunk,
    SupportDecisionOut,
)

NOVOSIBIRSK = timezone(timedelta(hours=7))


class IdempotencyConflict(Exception):
    pass


class SupportService:
    def __init__(
        self, settings: Settings, database: Database, gateway: LLMGateway,
        retriever: HybridRetriever, outbox: BitrixOutbox,
    ) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.retriever = retriever
        self.outbox = outbox
        self.lock = asyncio.Lock()
        self.variant = settings.support_variant
        self.structured = self.variant == "variant_b"
        self.prompt = gateway.load_prompt(ROOT / "config/prompts/support.yaml", self.variant)

    async def reindex(self) -> IndexResult:
        async with self.lock:
            usage = CycleUsage()
            count = await self.retriever.reindex(usage)
            return IndexResult(chunks=count, cost_rub=usage.rounded_cost)

    async def message(self, request_id: str, message: ChatMessageIn) -> SupportDecisionOut:
        async with self.lock:
            return await self._message(request_id, message)

    async def _message(self, request_id: str, message: ChatMessageIn) -> SupportDecisionOut:
        start = time.perf_counter()
        payload_hash = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
        now = datetime.now(NOVOSIBIRSK)
        with self.database.connect() as connection:
            previous = connection.execute(
                "SELECT * FROM cycles WHERE request_id=?", (request_id,),
            ).fetchone()
            if previous and previous["payload_hash"] != payload_hash:
                raise IdempotencyConflict
            if previous and previous["response"] is not None:
                return SupportDecisionOut.model_validate_json(previous["response"])
            connection.execute(
                """INSERT OR IGNORE INTO cycles
                (request_id, payload_hash, session_id, ab_variant, mode, channel, is_after_hours)
                VALUES (?,?,?,?,?,?,?)""",
                (request_id, payload_hash, message.session_id, self.variant, self.settings.llm_mode,
                 message.channel, now.weekday() >= 5 or not 9 <= now.hour < 18),
            )
            connection.execute("UPDATE cycles SET saved_minutes=NULL WHERE session_id=?", (message.session_id,))
            session = connection.execute(
                "SELECT cost_rub, cost_complete FROM cycles WHERE session_id=?", (message.session_id,),
            ).fetchall()
        session_cost = sum((Decimal(row["cost_rub"]) for row in session), Decimal(0))
        remaining = self.settings.max_session_cost_rub - session_cost
        # Account for earlier spend when retrying after an interruption.
        prior_cost = Decimal(previous["cost_rub"]) if previous else Decimal(0)
        usage = CycleUsage(
            cost_rub=prior_cost,
            tokens_in=previous["tokens_in"] if previous else 0,
            tokens_out=previous["tokens_out"] if previous else 0,
            cost_complete=bool(previous["cost_complete"]) if previous else True,
            spending_limit=remaining + prior_cost,
            on_change=lambda current: self._save_usage(request_id, current),
        )
        reason = escalation_trigger(message.message_text)
        chunks: list[RetrievedChunk] = []
        citations: list[RetrievedChunk] = []
        answer = ""
        query = message.message_text
        if reason is None and message.history and len(query.split()) <= 4:
            previous_query = next((item.text for item in reversed(message.history) if item.role == "user"), "")
            query = f"{previous_query} {query}".strip()
            reason = escalation_trigger(query)
        if reason is None:
            if not all(row["cost_complete"] for row in session):
                reason = "unverified_session_cost"
            else:
                try:
                    chunks = await self.retriever.search(query, usage)
                    eligible = [item for item in chunks if item.score >= self.settings.min_retrieval_score]
                    if not eligible:
                        reason = "low_retrieval_score"
                    else:
                        draft = await self._draft(message, query, eligible, usage)
                        evidence = grounded(draft, eligible, structured=self.structured)
                        if evidence is None:
                            reason = "insufficient_context" if draft.need_human else "grounding_failed"
                        else:
                            citations = evidence
                            answer = draft.reply_text
                except BudgetExceeded:
                    reason = "budget_exceeded"
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                    reason = "llm_or_index_unavailable"

        decision = SupportDecisionOut(
            request_id=request_id, mode=self.settings.llm_mode,
            action="escalate_to_operator" if reason else "auto_reply",
            escalation_reason=reason, confidence_score=chunks[0].score if chunks else 0,
            reply_text=answer, citations=citations, retrieved_chunks=chunks,
            summary_for_operator=(
                f"Причина: {reason}. Сообщение клиента: {message.message_text}" if reason else None
            ),
            bitrix_sync_status="pending", need_human=bool(reason),
            cost_rub=usage.rounded_cost, cost_complete=usage.cost_complete,
            latency_ms=round((time.perf_counter() - start) * 1000),
        )
        with self.database.connect() as connection:
            self._update_result(connection, decision, time.perf_counter() - start)
            for ordinal, (method, payload) in enumerate(self._steps(message, decision)):
                connection.execute(
                    """INSERT INTO bitrix_steps(operation_id, request_id, ordinal, method, payload, adapter_mode)
                    VALUES (?,?,?,?,?,?)""",
                    (f"{request_id}:{ordinal}", request_id, ordinal, method,
                     json.dumps(payload, ensure_ascii=False), self.settings.bitrix_mode),
                )
        synced = await self.outbox.publish(request_id)
        elapsed = time.perf_counter() - start
        decision = decision.model_copy(update={
            "bitrix_sync_status": "synced" if synced else "pending",
            "latency_ms": round(elapsed * 1000),
        })
        with self.database.connect() as connection:
            self._update_result(connection, decision, elapsed)
        return decision

    def _save_usage(self, request_id: str, usage: CycleUsage) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE cycles SET cost_rub=?, tokens_in=?, tokens_out=?, cost_complete=?
                WHERE request_id=?""",
                (str(usage.cost_rub), usage.tokens_in, usage.tokens_out, usage.cost_complete, request_id),
            )

    async def _draft(
        self, message: ChatMessageIn, query: str, chunks: list[RetrievedChunk], usage: CycleUsage,
    ) -> Draft:
        if self.settings.llm_mode == "mock":
            return mock_draft(query, chunks, structured=self.structured)
        payload = json.dumps({
            "message": message.message_text,
            "history": [item.model_dump() for item in message.history],
            "context": [chunk.model_dump() for chunk in chunks],
        }, ensure_ascii=False)
        raw = await self.gateway.evaluate(self.prompt, payload, usage)
        return Draft.model_validate(known_fields(raw, Draft))

    @staticmethod
    def _update_result(
        connection: sqlite3.Connection, decision: SupportDecisionOut, elapsed: float,
    ) -> None:
        connection.execute(
            "UPDATE cycles SET response=?, processing_time_sec=?, need_human=? WHERE request_id=?",
            (decision.model_dump_json(), elapsed, decision.need_human, decision.request_id),
        )

    def _steps(
        self, message: ChatMessageIn, decision: SupportDecisionOut,
    ) -> list[tuple[str, dict[str, object]]]:
        dialog = message.dialog_id
        if decision.need_human and self.settings.bitrix_mode == "live":
            actions: list[tuple[str, dict[str, object]]] = [
                ("imopenlines.bot.session.operator", {"dialog_id": dialog}),
                ("imbot.message.add", {
                    "dialog_id": dialog, "session_id": message.session_id,
                    "message": decision.summary_for_operator, "system": "Y",
                }),
            ]
            if message.deal_id is not None:
                actions.append(("crm.timeline.comment.add", {
                    "deal_id": message.deal_id, "comment": decision.summary_for_operator,
                }))
            return actions
        if decision.need_human:
            context = [
                chunk.model_dump() for chunk in decision.retrieved_chunks if chunk.score > 0
            ]
            return [
                ("imopenlines.bot.session.operator", {
                    "dialog_id": dialog, "queue_id": self.settings.bitrix_operator_queue_id,
                }),
                ("operator.summary", {
                    "dialog_id": dialog, "summary": decision.summary_for_operator, "context": context,
                }),
                ("operator.notify", {
                    "dialog_id": dialog, "queue_id": self.settings.bitrix_operator_queue_id,
                    "deal_id": message.deal_id,
                }),
            ]
        sources = "\n".join(f"• {chunk.doc_id} / {chunk.section}" for chunk in decision.citations)
        text = f"{decision.reply_text}\n\nИсточники\n{sources}"
        actions = [
            ("imbot.message.add", {
                "dialog_id": dialog, "session_id": message.session_id, "message": text,
            }),
        ]
        if message.deal_id is not None:
            actions.append(("crm.timeline.comment.add", {"deal_id": message.deal_id, "comment": text}))
        return actions

    async def reconcile(self) -> ReconcileResult:
        async with self.lock:
            with self.database.connect() as connection:
                rows = connection.execute("SELECT request_id, response FROM cycles WHERE response IS NOT NULL")
                decisions = [
                    SupportDecisionOut.model_validate_json(row["response"]) for row in rows
                ]
            synced = pending = 0
            for decision in decisions:
                if decision.bitrix_sync_status == "synced":
                    continue
                if not await self.outbox.publish(decision.request_id):
                    pending += 1
                    continue
                updated = decision.model_copy(update={"bitrix_sync_status": "synced"})
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE cycles SET response=? WHERE request_id=?",
                        (updated.model_dump_json(), updated.request_id),
                    )
                synced += 1
            return ReconcileResult(synced=synced, pending=pending)

    async def confirm_closure(self, request_id: str) -> bool:
        async with self.lock:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT session_id, response FROM cycles WHERE request_id=?", (request_id,),
                ).fetchone()
                if row is None or row["response"] is None:
                    return False
                decision = SupportDecisionOut.model_validate_json(row["response"])
                session = connection.execute(
                    """SELECT request_id, need_human FROM cycles WHERE session_id=?
                    ORDER BY rowid DESC""", (row["session_id"],),
                ).fetchall()
                if (decision.bitrix_sync_status != "synced" or session[0]["request_id"] != request_id
                        or any(item["need_human"] != 0 for item in session)):
                    return False
                baseline = self.settings.baseline_manual_minutes
                if baseline is None:
                    return False
                connection.execute("UPDATE cycles SET saved_minutes=COALESCE(saved_minutes, ?) WHERE request_id=?",
                                   (str(baseline), request_id))
                return True

    def metrics(self) -> list[MetricGroup]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT mode, response, cost_rub, cost_complete, saved_minutes FROM cycles WHERE response IS NOT NULL",
            ).fetchall()
        groups = []
        for mode in ("mock", "live"):
            mode_rows = [row for row in rows if row["mode"] == mode]
            if not mode_rows:
                continue
            decisions = [SupportDecisionOut.model_validate_json(row["response"]) for row in mode_rows]
            total = len(decisions)
            escalations = sum(item.need_human for item in decisions)
            delivered = sum(
                not item.need_human and item.bitrix_sync_status == "synced" for item in decisions
            )
            cost = sum((Decimal(row["cost_rub"]) for row in mode_rows), Decimal(0))
            unknown = sum(not row["cost_complete"] for row in mode_rows)
            savings = [
                Decimal(str(row["saved_minutes"])) for row in mode_rows if row["saved_minutes"] is not None
            ]
            groups.append(MetricGroup(
                saved_minutes=sum(savings, Decimal(0)) if savings else None,
                confirmed_auto_resolutions=len(savings),
                mode=mode, cycles=total, deflection_rate=delivered / total,
                escalations=escalations, avg_latency_ms=sum(item.latency_ms for item in decisions) / total,
                avg_cost_rub=None if unknown else rubles(cost / total),
                cost_rub=rubles(cost), incomplete_cost_cycles=unknown,
            ))
        return groups

