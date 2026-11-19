import hashlib
import json
from hmac import compare_digest
from typing import Any
from urllib.parse import parse_qs

from src.domain.schemas import ChatMessageIn
from src.integrations.bitrix_live import chat_id


def event_payload(body: bytes, content_type: str) -> dict[str, Any]:
    if content_type.partition(";")[0] == "application/x-www-form-urlencoded":
        form = parse_qs(body.decode(), max_num_fields=2048)
        params = {
            key[13:-1]: values[0] for key, values in form.items()
            if key.startswith("data[PARAMS][") and key.endswith("]")
        }
        user = {"IS_BOT": form.get("data[USER][IS_BOT]", ["N"])[0]}
        return {
            "event": form.get("event", [""])[0],
            "data": {"PARAMS": params, "USER": user},
            "auth": {"application_token": form.get("auth[application_token]", [""])[0]},
        }
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def authorized(expected: str, submitted: object) -> bool:
    return bool(expected) and isinstance(submitted, str) and compare_digest(
        expected.encode(), submitted.encode(),
    )


def normalize(payload: dict[str, Any], bot_id: int | None) -> tuple[str, ChatMessageIn] | None:
    if payload.get("event") != "ONIMBOTMESSAGEADD":
        return None
    data = payload["data"]
    params = data["PARAMS"]
    if params.get("CHAT_ENTITY_TYPE") != "LINES":
        return None
    author = str(params["FROM_USER_ID"])
    if (not author.isdecimal() or int(author) <= 0 or int(author) == bot_id
            or str(data.get("USER", {}).get("IS_BOT", "N")).upper() == "Y"
            or any(str(params.get(flag, "N")).upper() in ("Y", "YES", "TRUE", "1")
                   for flag in ("SYSTEM", "IS_BOT"))):
        return None
    dialog = params["DIALOG_ID"]
    chat_id(dialog)
    message_id = str(params["MESSAGE_ID"])
    if not message_id.isdecimal() or int(message_id) <= 0:
        raise ValueError("Invalid MESSAGE_ID")
    line_data = str(params.get("CHAT_ENTITY_DATA_1", "")).split("|")
    session = params.get("SESSION_ID") or (line_data[5] if len(line_data) > 5 else None)
    connector = str(params.get("CHAT_ENTITY_ID", "")).split("|", 1)[0].casefold()
    channel = "telegram" if connector == "telegrambot" else connector
    deal = params.get("DEAL_ID")
    if not deal and len(line_data) > 2 and line_data[1] == "DEAL":
        deal = line_data[2]
    if not deal:
        bindings = str(params.get("CHAT_ENTITY_DATA_2", "")).split("|")
        crm = dict(zip(bindings[::2], bindings[1::2]))
        deal = crm.get("DEAL")
    if str(deal) == "0":
        deal = None
    message = ChatMessageIn(
        dialog_id=dialog, session_id=str(session or dialog), user_id=author,
        message_text=params["MESSAGE"],
        channel=channel if channel in ("telegram", "whatsapp") else "web",
        deal_id=int(deal) if deal else None,
    )
    key = hashlib.sha256(f"{dialog}:{int(message_id)}".encode()).hexdigest()
    return f"bitrix:{key}", message

