from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Action = Literal["auto_reply", "escalate_to_operator"]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryMessage(DTO):
    role: Literal["user", "assistant", "operator"]
    text: Message


class ChatMessageIn(DTO):
    session_id: Identifier
    dialog_id: Identifier
    user_id: Identifier
    deal_id: int | None = Field(None, gt=0)
    channel: Literal["web", "telegram", "whatsapp"]
    message_text: Message
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)


class RetrievedChunk(DTO):
    doc_id: str
    section: str
    chunk_id: str
    text: str
    score: float = Field(0, ge=0, le=1)


class Draft(DTO):
    need_human: bool = Field(strict=True)
    reply_text: str = Field(max_length=4000)
    citation_ids: list[str] = Field(max_length=5)


class SupportDecisionOut(DTO):
    action: Action
    escalation_reason: str | None
    confidence_score: float = Field(ge=0, le=1)
    reply_text: str
    citations: list[RetrievedChunk]
    summary_for_operator: str | None
    bitrix_sync_status: Literal["pending", "synced"]
    cost_rub: Decimal
    latency_ms: int = Field(ge=0)
    need_human: bool
    request_id: str
    mode: Literal["mock", "live"]
    cost_complete: bool
    retrieved_chunks: list[RetrievedChunk]


class MetricGroup(DTO):
    mode: Literal["mock", "live"]
    cycles: int
    deflection_rate: float
    escalations: int
    avg_latency_ms: float
    avg_cost_rub: Decimal | None
    cost_rub: Decimal
    incomplete_cost_cycles: int
    saved_minutes: Decimal | None = None
    confirmed_auto_resolutions: int = 0


class ConfirmClosureIn(DTO):
    request_id: Identifier


class IndexResult(DTO):
    chunks: int
    cost_rub: Decimal


class ReconcileResult(DTO):
    synced: int
    pending: int

