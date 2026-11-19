from uuid import uuid4

import httpx
import streamlit as st

from ops.runners.benchmark_ab import REPORT, BenchmarkReport
from src.core.config import Settings
from src.domain.schemas import (
    ChatMessageIn,
    HistoryMessage,
    MetricGroup,
    SupportDecisionOut,
)
from src.ui.presets import PRESETS


def submit(text: str, channel: str, api: str) -> None:
    pending = st.session_state.get("pending")
    if pending is None:
        history = [
            HistoryMessage(role=role, text=message)
            for role, message in st.session_state.messages[-20:]
        ]
        request = ChatMessageIn.model_validate({
            "session_id": st.session_state.session_id, "dialog_id": st.session_state.session_id,
            "user_id": "demo", "channel": channel, "message_text": text, "history": history,
        })
        pending = (uuid4().hex, request)
        st.session_state.pending = pending
    key, request = pending
    try:
        response = httpx.post(
            f"{api}/support/message", json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": key}, timeout=75,
        )
        response.raise_for_status()
        decision = SupportDecisionOut.model_validate(response.json())
    except (httpx.HTTPError, ValueError):
        st.error("API не вернул решение. Повторите отправку с тем же ключом.")
        return
    assistant = decision.reply_text or "Передаю обращение оператору."
    st.session_state.messages.extend([
        ("user", request.message_text), ("assistant", assistant),
    ])
    st.session_state.decision = decision
    st.session_state.pending = None


def demo(api: str) -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid4().hex
        st.session_state.messages = []
    channel = st.selectbox("Канал", ["web", "telegram", "whatsapp"])
    pending = st.session_state.get("pending")
    if pending and st.button("Повторить отправку"):
        submit("", channel, api)
        pending = st.session_state.get("pending")
    chosen = None
    for column, (label, query) in zip(st.columns(4), PRESETS.items(), strict=True):
        if column.button(label, disabled=bool(pending)):
            chosen = query
    text = st.chat_input("Сообщение клиента", max_chars=4000, disabled=bool(pending))
    message = (chosen or text or "").strip()
    if message:
        submit(message, channel, api)
    for role, content in st.session_state.messages:
        with st.chat_message(role):
            st.write(content)
    result: SupportDecisionOut | None = st.session_state.get("decision")
    if result is None:
        return
    if result.need_human:
        st.info(f"Эскалация · {result.escalation_reason} · Bitrix24 {result.bitrix_sync_status}")
    if result.citations:
        st.caption("Источники · " + " / ".join(
            f"{item.doc_id} · {item.section}" for item in result.citations
        ))
    with st.expander("Найденные фрагменты", expanded=True):
        if not result.retrieved_chunks:
            st.caption("Поиск не вернул фрагменты.")
        for chunk in result.retrieved_chunks:
            st.markdown(f"**{chunk.section}** · {chunk.score:.3f}")
            st.caption(chunk.doc_id)
            st.write(chunk.text)


def dashboard(api: str) -> None:
    try:
        response = httpx.get(f"{api}/metrics", timeout=3)
        response.raise_for_status()
        metrics = [MetricGroup.model_validate(group) for group in response.json()]
    except (httpx.HTTPError, ValueError):
        st.warning("Метрики недоступны. Проверьте запуск API.")
        return
    if not metrics:
        st.info("Показатели появятся после первого обращения.")
    for group in metrics:
        st.subheader({"mock": "Демо-обращения", "live": "Обращения с LLM"}[group.mode])
        st.caption(f"Всего обращений · {group.cycles}")
        columns = st.columns(4)
        columns[0].metric("Deflection Rate", f"{group.deflection_rate:.1%}")
        columns[1].metric("Эскалации", group.escalations)
        columns[2].metric("Средняя задержка", f"{group.avg_latency_ms:.0f} мс")
        cost = group.avg_cost_rub
        columns[3].metric("Средняя стоимость", "—" if cost is None else f"{cost:.2f} ₽")
        st.caption(f"Учтённые расходы · {group.cost_rub:.2f} ₽")
        st.caption(
            f"Подтверждённые закрытия без эскалации · {group.confirmed_auto_resolutions}"
        )
        saved = group.saved_minutes
        st.metric("Сэкономлено минут", "—" if saved is None else f"{saved:.2f}")
        if group.incomplete_cost_cycles:
            st.caption(f"Обращений с неполным учётом · {group.incomplete_cost_cycles}")


def benchmark_results() -> None:
    if not REPORT.is_file():
        return
    try:
        report = BenchmarkReport.model_validate_json(REPORT.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        st.warning("Отчёт A/B недоступен.")
        return

    st.subheader("A/B-бенчмарк")
    st.caption(f"Синтетические диалоги · LLM {report.mode} · задержка без сети Bitrix24")
    rows = []
    for variant, item in report.variants.items():
        rows.append({
            "Вариант": variant, "Кейсы": item.cases,
            "Action Accuracy": item.action_accuracy, "Escalation Recall": item.escalation_recall,
            "Grounding Pass Rate": item.grounding_pass_rate, "Faithfulness": item.faithfulness,
            "Hallucination Rate": item.hallucination_rate, "Citation Precision": item.citation_precision,
            "p50, мс": item.latency_p50_ms, "p95, мс": item.latency_p95_ms,
            "Стоимость, ₽": str(item.avg_cost_rub) if item.avg_cost_rub is not None else "—",
        })
    st.dataframe(rows, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Первая линия поддержки", layout="wide")
    st.title("Первая линия поддержки")
    api = f"http://127.0.0.1:{Settings().api_port}/api/v1"
    chat, metrics = st.tabs(["Live Demo", "Executive ROI-дашборд"])
    with chat:
        demo(api)
    with metrics:
        dashboard(api)
        benchmark_results()


if __name__ == "__main__":
    main()
