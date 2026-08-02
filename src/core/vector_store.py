"""Qdrant embedded в data/qdrant. Коллекция messages пересоздаётся при первом upsert
с актуальным размером embedding'а — это позволяет менять провайдера без миграций.

qdrant_client импортируется и клиент открывается ЛЕНИВО: сам импорт стоит ~60 МБ RSS,
а открытие сразу на импорте модуля ещё и захватывает файловый лок хранилища —
из-за этого любой вспомогательный скрипт падал с 'already accessed by another instance'.
"""
import asyncio
import logging
from dataclasses import dataclass

from src.config import settings


logger = logging.getLogger(__name__)


COLLECTION = "messages"


@dataclass
class VectorHit:
    user_id: int
    peer_id: int
    peer_name: str | None
    message_id: int
    text: str
    date_iso: str | None
    score: float


class VectorStore:
    def __init__(self) -> None:
        self._path = settings.data_dir / "qdrant"
        self._client = None
        self._lock = asyncio.Lock()
        self._dim: int | None = None

    def _c(self):
        """Клиент создаётся при первом реальном обращении, а не при импорте."""
        if self._client is None:
            from qdrant_client import QdrantClient

            self._path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(self._path))
        return self._client

    async def _ensure_collection(self, dim: int) -> None:
        if self._dim == dim:
            return
        async with self._lock:
            if self._dim == dim:
                return

            def _check_or_create() -> None:
                from qdrant_client.http import models as qmodels

                client = self._c()
                existing = {c.name for c in client.get_collections().collections}
                if COLLECTION in existing:
                    info = client.get_collection(COLLECTION)
                    actual = info.config.params.vectors.size
                    if actual != dim:
                        logger.warning(
                            "Recreating Qdrant collection %s: dim %d → %d",
                            COLLECTION, actual, dim,
                        )
                        client.delete_collection(COLLECTION)
                        client.create_collection(
                            COLLECTION,
                            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
                        )
                else:
                    client.create_collection(
                        COLLECTION,
                        vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
                    )

            await asyncio.to_thread(_check_or_create)
            self._dim = dim

    @staticmethod
    def _point_id(user_id: int, peer_id: int, message_id: int) -> int:
        # 64-битный int = user(16) | peer(24) | msg(24)
        return ((user_id & 0xFFFF) << 48) | ((peer_id & 0xFFFFFF) << 24) | (message_id & 0xFFFFFF)

    async def upsert(
        self,
        *,
        user_id: int,
        peer_id: int,
        peer_name: str | None,
        message_id: int,
        text: str,
        date_iso: str | None,
        embedding: list[float],
    ) -> None:
        await self._ensure_collection(len(embedding))

        def _do() -> None:
            from qdrant_client.http import models as qmodels

            self._c().upsert(
                collection_name=COLLECTION,
                points=[
                    qmodels.PointStruct(
                        id=self._point_id(user_id, peer_id, message_id),
                        vector=embedding,
                        payload={
                            "user_id": user_id,
                            "peer_id": peer_id,
                            "peer_name": peer_name,
                            "message_id": message_id,
                            "text": text,
                            "date_iso": date_iso,
                        },
                    )
                ],
            )

        await asyncio.to_thread(_do)

    async def search(
        self,
        *,
        user_id: int,
        embedding: list[float],
        limit: int = 10,
        peer_id: int | None = None,
    ) -> list[VectorHit]:
        if self._dim is None:
            return []

        def _do():
            from qdrant_client.http import models as qmodels

            flt = qmodels.Filter(
                must=[qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id))]
            )
            if peer_id is not None:
                flt.must.append(
                    qmodels.FieldCondition(key="peer_id", match=qmodels.MatchValue(value=peer_id))
                )
            return self._c().search(
                collection_name=COLLECTION,
                query_vector=embedding,
                limit=limit,
                query_filter=flt,
            )

        raw = await asyncio.to_thread(_do)
        return [
            VectorHit(
                user_id=p.payload.get("user_id"),
                peer_id=p.payload.get("peer_id"),
                peer_name=p.payload.get("peer_name"),
                message_id=p.payload.get("message_id"),
                text=p.payload.get("text", ""),
                date_iso=p.payload.get("date_iso"),
                score=float(p.score),
            )
            for p in raw
        ]


vector_store = VectorStore()
