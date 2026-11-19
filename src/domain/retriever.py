import hashlib
import json
import math
import re
from pathlib import Path

from src.core.database import Database
from src.core.llm_gateway import CycleUsage, LLMGateway
from src.core.text import tokens
from src.domain.schemas import RetrievedChunk


def read_chunks(path: Path) -> list[RetrievedChunk]:
    chunks = []
    for document in sorted(path.glob("*.md")):
        sections = re.split(r"^##\s+(.+)$", document.read_text(encoding="utf-8-sig"), flags=re.M)
        for ordinal, (section, body) in enumerate(
            zip(sections[1::2], sections[2::2], strict=True), 1,
        ):
            blocks = re.split(r"\n\s*\n", body.strip())
            for index, block in enumerate(blocks, 1):
                if block.strip():
                    chunk_id = hashlib.sha256(
                        f"{document.stem}\n{ordinal}\n{section}\n{index}".encode(),
                    ).hexdigest()[:16]
                    chunks.append(RetrievedChunk(
                        doc_id=document.stem, section=section.strip(),
                        chunk_id=chunk_id, text=block.strip(),
                    ))
    if not chunks:
        raise ValueError("No Markdown sections in the knowledge base")
    return chunks


def cosine(left: list[float], right: list[float]) -> float:
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


class HybridRetriever:
    def __init__(self, path: Path, gateway: LLMGateway, database: Database) -> None:
        self.path = path
        self.gateway = gateway
        self.database = database
        self.index: list[tuple[RetrievedChunk, set[str], list[float]]] = []

    async def load_cached(self) -> None:
        await self._build_index(None)

    async def reindex(self, usage: CycleUsage) -> int:
        return await self._build_index(usage)

    async def _build_index(self, usage: CycleUsage | None) -> int:
        snapshot: list[tuple[RetrievedChunk, set[str], list[float]]] = []
        settings = self.gateway.settings
        embedding_id = (
            "mock-hash-v1" if settings.llm_mode == "mock"
            else f"{settings.base_url.rstrip('/')}:{settings.embedding_model}"
        )
        for chunk in read_chunks(self.path):
            content = f"{chunk.section}\n{chunk.text}"
            key = hashlib.sha256(f"{embedding_id}\n{content}".encode()).hexdigest()
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT vector FROM embedding_cache WHERE cache_key=?", (key,),
                ).fetchone()
            if row is None and usage is None:
                raise ValueError("Knowledge base cache requires reindexing")
            vector: list[float] = (
                json.loads(row["vector"]) if row else await self._embed(content, usage)
            )
            if snapshot and len(vector) != len(snapshot[0][2]):
                raise ValueError("Embedding dimensions changed during indexing")
            if row is None:
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO embedding_cache VALUES (?,?)", (key, json.dumps(vector)),
                    )
            snapshot.append((chunk, set(tokens(content)), vector))
        self.index = snapshot
        return len(snapshot)

    async def _embed(self, content: str, usage: CycleUsage | None) -> list[float]:
        assert usage is not None
        return await self.gateway.embed(content, usage)

    async def search(self, query: str, usage: CycleUsage) -> list[RetrievedChunk]:
        terms = set(tokens(query))
        if not terms:
            return []
        if not self.index:
            raise ValueError("Knowledge base index is unavailable")
        vector = await self.gateway.embed(query, usage)
        hits = []
        for chunk, words, embedding in self.index:
            lexical = len(terms & words) / len(terms)
            semantic = cosine(vector, embedding)
            # A lexical anchor is required for a dense match to authorize an auto-reply.
            score = 0.75 * math.sqrt(lexical) + 0.25 * semantic if lexical else 0.0
            hits.append(chunk.model_copy(update={"score": score}))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:5]

