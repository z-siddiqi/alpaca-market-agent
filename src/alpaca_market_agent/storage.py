from datetime import date

from google.api_core.exceptions import Conflict
from google.cloud.firestore_v1 import AsyncClient

from alpaca_market_agent.models import NarrativeRecord


class NarrativeStore:
    def __init__(self, project: str | None = None, client: AsyncClient | None = None) -> None:
        self.project = project or None
        self._client = client

    async def get(self, plan_date: date) -> NarrativeRecord | None:
        snapshot = await self._document(plan_date).get()
        if not snapshot.exists:
            return None
        return NarrativeRecord.model_validate(snapshot.to_dict())

    async def put(self, narrative: NarrativeRecord) -> NarrativeRecord:
        document = self._document(narrative.plan_session)
        try:
            await document.create(narrative.model_dump(mode="json", by_alias=True))
        except Conflict:
            existing = await self.get(narrative.plan_session)
            if existing is None:
                raise
            return existing
        return narrative

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _document(self, plan_date: date):
        return self._firestore.collection("narratives").document(plan_date.isoformat())

    @property
    def _firestore(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(project=self.project)
        return self._client
