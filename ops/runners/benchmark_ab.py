import argparse
import asyncio
import json
import math
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Literal

import httpx

from src.core.config import ROOT, Settings
from src.core.database import Database
from src.core.llm_gateway import LLMGateway
from src.domain.guardrails import grounded
from src.domain.ports import BitrixOutbox
from src.domain.retriever import HybridRetriever
from src.domain.schemas import DTO, Action, ChatMessageIn, Draft
from src.domain.service import SupportService

DATASET = ROOT / "data/eval/dialogs_60.jsonl"
REPORT = ROOT / "docs/benchmark_ab_report.json"
Variant = Literal["variant_a", "variant_b"]
VARIANTS: tuple[Variant, ...] = ("variant_a", "variant_b")


class EvalCase(DTO):
    case_id: str
    category: Literal["auto_reply", "out_of_scope", "sentiment_legal_human", "adversarial"]
    input: ChatMessageIn
    expected_action: Action
    expected_escalation_reason: str | None
    expected_doc_ids: list[str]
    forbidden_claims: list[str]


class CaseResult(DTO):
    case_id: str
    action: Action
    escalation_reason: str | None
    expected_action: Action
    expected_escalation_reason: str | None
    doc_ids: list[str]
    expected_doc_ids: list[str]
    grounding_pass: bool | None
    forbidden_claims_found: list[str]
    latency_ms: float
    cost_rub: Decimal | None


class Summary(DTO):
    cases: int
    auto_replies: int
    action_accuracy: float
    escalation_recall: float | None
    grounding_pass_rate: float | None
    faithfulness: float | None
    hallucination_rate: float | None
    citation_precision: float | None
    latency_p50_ms: float
    latency_p95_ms: float
    avg_cost_rub: Decimal | None


class BenchmarkReport(DTO):
    mode: Literal["mock", "live"]
    variants: dict[Variant, Summary]
    results: dict[Variant, list[CaseResult]]


class BenchmarkPublisher:
    async def execute(self, operation_id: str, method: str, payload: str) -> str:
        return operation_id


def load_cases(path: Path = DATASET) -> list[EvalCase]:
    return [EvalCase.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ratio(count: int, total: int) -> float | None:
    return count / total if total else None


def summarize(results: list[CaseResult]) -> Summary:
    replies = [item for item in results if item.action == "auto_reply"]
    escalations = [item for item in results if item.expected_action == "escalate_to_operator"]
    costs = [item.cost_rub for item in results if item.cost_rub is not None]
    latency = sorted(item.latency_ms for item in results)
    faithful = sum(bool(item.grounding_pass) and not item.forbidden_claims_found for item in replies)
    return Summary(
        cases=len(results), auto_replies=len(replies),
        action_accuracy=sum(item.action == item.expected_action for item in results) / len(results),
        escalation_recall=ratio(sum(item.action == "escalate_to_operator" for item in escalations), len(escalations)),
        grounding_pass_rate=ratio(sum(bool(item.grounding_pass) for item in replies), len(replies)),
        faithfulness=ratio(faithful, len(replies)),
        hallucination_rate=ratio(len(replies) - faithful, len(replies)),
        citation_precision=ratio(
            sum(len(set(item.doc_ids) & set(item.expected_doc_ids)) for item in replies),
            sum(len(item.doc_ids) for item in replies),
        ),
        latency_p50_ms=latency[math.ceil(0.50 * len(latency)) - 1],
        latency_p95_ms=latency[math.ceil(0.95 * len(latency)) - 1],
        avg_cost_rub=sum(costs, Decimal(0)) / len(results) if len(costs) == len(results) else None,
    )


async def benchmark(
    settings: Settings, dataset: Path = DATASET, *, transport: httpx.AsyncBaseTransport | None = None,
) -> BenchmarkReport:
    cases = load_cases(dataset)
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Evaluation dataset must contain unique case IDs")
    results: dict[Variant, list[CaseResult]] = {}
    with TemporaryDirectory() as directory:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            # The embedding cache is shared, while request keys and sessions isolate variants.
            database = Database(Path(directory) / "benchmark.db")
            for variant in VARIANTS:
                config = settings.model_copy(update={"support_variant": variant, "bitrix_mode": "mock"})
                gateway = LLMGateway(config, client)
                retriever = HybridRetriever(config.knowledge_base_path, gateway, database)
                outbox = BitrixOutbox(database, BenchmarkPublisher())
                service = SupportService(config, database, gateway, retriever, outbox)
                await service.reindex()
                result = []
                for case in cases:
                    request_id = f"{variant}:{case.case_id}"
                    message = case.input.model_copy(update={"session_id": f"{variant}:{case.input.session_id}"})
                    start = perf_counter()
                    decision = await service.message(request_id, message)
                    elapsed = (perf_counter() - start) * 1000
                    draft = Draft(
                        need_human=decision.need_human, reply_text=decision.reply_text,
                        citation_ids=[chunk.chunk_id for chunk in decision.citations],
                    )
                    with database.connect() as connection:
                        usage = connection.execute(
                            "SELECT cost_rub, cost_complete FROM cycles WHERE request_id=?", (request_id,),
                        ).fetchone()
                    normalized = " ".join(decision.reply_text.casefold().split())
                    result.append(CaseResult(
                        case_id=case.case_id, action=decision.action,
                        escalation_reason=decision.escalation_reason, expected_action=case.expected_action,
                        expected_escalation_reason=case.expected_escalation_reason,
                        doc_ids=sorted({chunk.doc_id for chunk in decision.citations}),
                        expected_doc_ids=case.expected_doc_ids,
                        grounding_pass=None if decision.need_human else bool(grounded(
                            draft, decision.retrieved_chunks, structured=variant == "variant_b",
                        )),
                        forbidden_claims_found=[
                            claim for claim in case.forbidden_claims
                            if " ".join(claim.casefold().split()) in normalized
                        ],
                        latency_ms=round(elapsed, 3),
                        cost_rub=Decimal(usage["cost_rub"]) if usage["cost_complete"] else None,
                    ))
                results[variant] = result
    return BenchmarkReport(
        mode=settings.llm_mode, results=results,
        variants={variant: summarize(result) for variant, result in results.items()},
    )


def write_report(report: BenchmarkReport, path: Path) -> None:
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    lines = [
        "# A/B-бенчмарк поддержки", "",
        f"Режим LLM: `{report.mode}`. Использован синтетический набор по корпоративной базе знаний.",
        "Задержка включает локальную запись журнала и исключает сеть Bitrix24 и индексацию.",
        "Grounding, Faithfulness и Hallucination Rate рассчитаны по выданным автоответам.",
        "Faithfulness проверяет дословную опору и запрещённые утверждения, а совпадение источников",
        "измеряется отдельно. Стоимость показана за цикл, включая эскалации.", "",
        "| Вариант | Кейсы | Auto replies | Action Accuracy | Escalation Recall | Grounding Pass Rate | "
        "Faithfulness | Hallucination Rate | Citation Precision | p50, мс | p95, мс | avg_cost_rub |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, item in report.variants.items():
        rates = (
            item.action_accuracy, item.escalation_recall, item.grounding_pass_rate,
            item.faithfulness, item.hallucination_rate, item.citation_precision,
        )
        values = " | ".join("—" if rate is None else f"{rate:.1%}" for rate in rates)
        cost = "—" if item.avg_cost_rub is None else str(item.avg_cost_rub)
        lines.append(f"| {variant} | {item.cases} | {item.auto_replies} | {values} | "
                     f"{item.latency_p50_ms:.3f} | {item.latency_p95_ms:.3f} | {cost} |")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock-llm", action="store_true")
    mode.add_argument("--live-llm", action="store_true")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=REPORT)
    args = parser.parse_args()
    settings = Settings(llm_mode="live" if args.live_llm else "mock")
    report = asyncio.run(benchmark(settings, args.dataset))
    write_report(report, args.output)
    print(json.dumps({variant: item.model_dump(mode="json")
                      for variant, item in report.variants.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

