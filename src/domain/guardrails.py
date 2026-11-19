import re

from src.core.text import tokens
from src.domain.schemas import Draft, RetrievedChunk

ESCALATION = (
    ("complaint_or_refund", re.compile(
        r"\b(?:жалоб\w*|претензи\w*|возврат\w*|верн\w+\s+(?:мои\s+)?"
        r"деньги|суд\w*|юрист\w*|мошен\w*|ужас\w*|отврат\w*|"
        r"обман\w*|кошмар\w*|недовол\w*|недоволь\w*|возмущ\w*|"
        r"безобраз\w*|хамств\w*|ненавиж\w*|не\s+работа\w*|"
        r"иск\w*\s+в\s+суд|роспотребнадзор\w*|идиот\w*|тупиц\w*)\b",
    )),
    ("operator_requested", re.compile(
        r"\b(?:оператор\w*|жив\w*\s+(?:человек\w*|сотрудник\w*)|"
        r"(?:позовите|соедините|переведите|пригласите)\s+(?:меня\s+)?"
        r"(?:с\s+)?(?:человек\w*|менеджер\w*|сотрудник\w*))\b",
    )),
)


OUT_OF_SCOPE = re.compile(
    r"\b(?:amocrm|амо\s*срм|salesforce|sap|hubspot|кастомн\w*|"
    r"сверх\s+прайс\w*|промокод\w*|скидк\w*|доставк\w*|"
    r"округл\w*|придум\w*|выдум\w*|игнориру\w*|подтверди\w*)\b",
)
GREETING = "Здравствуйте! По базе знаний:"


def escalation_trigger(message: str) -> str | None:
    normalized = message.casefold().replace("ё", "е")
    if re.search(r"\bоператор\w*\b", normalized) and not re.search(
        r"(?:^оператор\w*(?:[ ,]+пожалуйста)?[.!?]*$|позов|соедин|перевед|приглас|нужен|хочу|дайте|поговор|"
        r"можно\s+оператор|позвать|связаться|переключ|подключите|вызов|подключить\s+оператор)",
        normalized,
    ):
        normalized = re.sub(r"\bоператор\w*\b", "", normalized)
    reason = next((reason for reason, pattern in ESCALATION if pattern.search(normalized)), None)
    return reason or ("insufficient_context" if OUT_OF_SCOPE.search(normalized) else None)


def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", " ".join(text.split()))


def grounded(
    draft: Draft, chunks: list[RetrievedChunk], *, structured: bool = False,
) -> list[RetrievedChunk] | None:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    if draft.need_human or not draft.reply_text.strip() or not draft.citation_ids:
        return None
    unique_ids = list(dict.fromkeys(draft.citation_ids))
    if any(chunk_id not in by_id for chunk_id in unique_ids):
        return None
    if structured:
        header, *lines = draft.reply_text.strip().splitlines()
        if header != GREETING or not lines or any(not line.startswith("• ") for line in lines):
            return None
        reply = {line[2:] for line in lines}
    else:
        reply = set(sentences(draft.reply_text))
    # Exact evidence membership verifies each entire statement, including numeric qualifiers.
    cited = [by_id[chunk_id] for chunk_id in unique_ids]
    evidence = [set(sentences(chunk.text)) for chunk in cited]
    if not reply.issubset(set().union(*evidence)):
        return None
    if any(not reply.intersection(source) for source in evidence):
        return None
    return cited


def mock_draft(message: str, chunks: list[RetrievedChunk], *, structured: bool = False) -> Draft:
    query = set(tokens(message))
    candidates = [
        (len(query.intersection(tokens(sentence))), chunk.score, chunk.chunk_id, sentence)
        for chunk in chunks
        for sentence in sentences(chunk.text)
    ]
    overlap, _, chunk_id, sentence = max(candidates)
    reply = f"{GREETING}\n• {sentence}" if structured else sentence
    return Draft(need_human=not overlap, reply_text=reply, citation_ids=[chunk_id])

