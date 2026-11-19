import asyncio
import hashlib
import json
import re
from time import monotonic
from typing import Any

import httpx

from src.core.config import Settings
from src.domain.ports import RetryableBitrixError


class UnconfirmedBitrixWrite(Exception):
    """The remote write may have succeeded."""


def chat_id(dialog: str) -> int:
    match = re.fullmatch(r"chat([1-9][0-9]*)", dialog)
    if match is None:
        raise ValueError("Expected an OpenLines chat identifier")
    return int(match[1])


def receipt_id(value: object) -> str:
    if type(value) in (int, str) and re.fullmatch(r"[1-9][0-9]*", str(value)):
        return str(value)
    raise UnconfirmedBitrixWrite


class BitrixLiveAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        if (settings.bitrix_webhook_url is None or settings.bitrix_bot_id is None
                or settings.bitrix_client_id is None or not settings.bitrix_client_id.get_secret_value()):
            raise ValueError("Live Bitrix requires BITRIX_WEBHOOK_URL, BITRIX_BOT_ID and BITRIX_CLIENT_ID")
        url = httpx.URL(settings.bitrix_webhook_url.get_secret_value())
        if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("Invalid Bitrix webhook URL")
        self.url = str(url).rstrip("/")
        self.bot_id = settings.bitrix_bot_id
        self.client_id = settings.bitrix_client_id.get_secret_value()
        self.client = client
        self.lock = asyncio.Lock()
        # Also leave a full interval after a publisher restart.
        self.next_request_at = monotonic() + 0.5

    async def _call(
        self, method: str, params: dict[str, Any], *, read: bool = False,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                async with self.lock:
                    while (delay := self.next_request_at - monotonic()) > 0:
                        await asyncio.sleep(delay)
                    try:
                        response = await self.client.post(f"{self.url}/{method}.json", json=params)
                    finally:
                        self.next_request_at = monotonic() + 0.5
                if response.status_code == 429:
                    raise RetryableBitrixError
                if response.status_code == 503:
                    try:
                        code = response.json().get("error")
                    except (ValueError, AttributeError):
                        code = None
                    if read or code in ("QUERY_LIMIT_EXCEEDED", "OVERLOAD_LIMIT"):
                        raise RetryableBitrixError
                    raise UnconfirmedBitrixWrite
                body: dict[str, Any] = response.json()
                if body.get("error") in ("QUERY_LIMIT_EXCEEDED", "OVERLOAD_LIMIT"):
                    raise RetryableBitrixError
                if not response.is_success or "result" not in body or body.get("error"):
                    raise UnconfirmedBitrixWrite
                return body
            except (RetryableBitrixError, httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 2:
                    raise RetryableBitrixError from None
                await asyncio.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def marker(operation_id: str) -> str:
        return "Support ref " + hashlib.sha256(operation_id.encode()).hexdigest()[:24]

    async def execute(self, operation_id: str, method: str, payload: str) -> str:
        data = json.loads(payload)
        marker = self.marker(operation_id)
        if method == "imopenlines.bot.session.operator":
            result = await self._call(method, {"CHAT_ID": chat_id(data["dialog_id"])})
            if result["result"] is True:
                return "transferred"
            raise UnconfirmedBitrixWrite
        if method == "crm.timeline.comment.add":
            result = await self._call(method, {"fields": {
                "ENTITY_TYPE": "deal", "ENTITY_ID": data["deal_id"],
                "COMMENT": f'{data["comment"]}\n\n{marker}',
            }})
        elif method == "imbot.message.add":
            result = await self._call(method, {
                "BOT_ID": self.bot_id, "DIALOG_ID": data["dialog_id"], "CLIENT_ID": self.client_id,
                "MESSAGE": f'{data["message"]}\n\n{marker}',
                "SYSTEM": data.get("system", "N"),
                "SKIP_CONNECTOR": data.get("system", "N"),
            })
        else:
            raise ValueError("Unsupported Bitrix operation")
        return receipt_id(result["result"])

    async def verify(self, operation_id: str, method: str, payload: str) -> str | None:
        data = json.loads(payload)
        marker = self.marker(operation_id)
        if method == "crm.timeline.comment.add":
            start = 0
            while True:
                page = await self._call("crm.timeline.comment.list", {
                    "filter": {"ENTITY_TYPE": "deal", "ENTITY_ID": data["deal_id"]},
                    "select": ["ID", "COMMENT"], "start": start,
                }, read=True)
                for item in page["result"]:
                    if str(item["COMMENT"]).rstrip().endswith(marker):
                        return receipt_id(item["ID"])
                next_start = page.get("next")
                if type(next_start) is not int or next_start <= start:
                    return None
                start = next_start
        if method not in ("imbot.message.add", "imopenlines.bot.session.operator"):
            return None
        # Session history can match a message marker. It does not identify a specific
        # transfer request, so an ambiguous transfer stays pending for inspection.
        if method == "imopenlines.bot.session.operator":
            return None
        params = {"CHAT_ID": chat_id(data["dialog_id"])}
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id.isdecimal():
            params = {"SESSION_ID": int(session_id)}
        page = await self._call("imopenlines.session.history.get", params, read=True)
        for item in page["result"]["message"].values():
            if str(item["text"]).rstrip().endswith(marker):
                return receipt_id(item["id"])
        return None

