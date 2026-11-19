import asyncio
import json

from src.core.database import Database


class MockBitrixConnector:
    """Durable mock receipt models server-side deduplication on retry."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.lock = asyncio.Lock()

    async def execute(self, operation_id: str, method: str, payload: str) -> str | None:
        async with self.lock:
            with self.database.connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM mock_receipts WHERE operation_id=?", (operation_id,),
                ).fetchone():
                    return operation_id
            await asyncio.sleep(0.5)
            json.loads(payload)
            with self.database.connect() as connection:
                connection.execute("INSERT INTO mock_receipts(operation_id) VALUES (?)", (operation_id,))
            return operation_id

