from typing import Protocol

from src.core.database import Database


class RetryableBitrixError(Exception):
    pass


class BitrixPort(Protocol):
    async def execute(self, operation_id: str, method: str, payload: str) -> str | None: ...


class LiveBitrixPort(BitrixPort, Protocol):
    async def verify(self, operation_id: str, method: str, payload: str) -> str | None: ...


class BitrixOutbox:
    def __init__(
        self, database: Database, adapter: BitrixPort, live: LiveBitrixPort | None = None,
    ) -> None:
        self.live = live
        self.database = database
        self.adapter = adapter

    async def publish(self, request_id: str) -> bool:
        with self.database.connect() as connection:
            steps = connection.execute(
                "SELECT * FROM bitrix_steps WHERE request_id=? AND state!='done' ORDER BY ordinal",
                (request_id,),
            ).fetchall()
        for step in steps:
            operation_id = step["operation_id"]
            live = self.live if step["adapter_mode"] == "live" else None
            if step["adapter_mode"] == "live" and live is None:
                return False
            args = (operation_id, step["method"], step["payload"])
            checking = live is not None and step["state"] == "sending"
            try:
                if live is not None and checking:
                    receipt = await live.verify(*args)
                    if receipt is None:
                        return False
                else:
                    with self.database.connect() as connection:
                        connection.execute(
                            "UPDATE bitrix_steps SET state='sending', attempts=attempts+1 WHERE operation_id=?",
                            (operation_id,),
                        )
                    adapter = live if live is not None else self.adapter
                    receipt = await adapter.execute(*args)
            except Exception as error:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE bitrix_steps SET last_error=?, state=? WHERE operation_id=?",
                        (type(error).__name__,
                         "pending" if not checking and isinstance(error, RetryableBitrixError) else "sending",
                         operation_id),
                    )
                return False
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE bitrix_steps SET state='done', last_error=NULL, receipt=? WHERE operation_id=?",
                    (receipt, operation_id),
                )
        return True

