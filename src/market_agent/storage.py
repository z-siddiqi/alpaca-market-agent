from datetime import date

from google.api_core.exceptions import Conflict
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from market_agent.models import DecisionRecord, NarrativeRecord, TradePlan

# `decisions` also holds lean markers for scheduled ticks that never produced a
# record. They are not valid DecisionRecords; web/render.py renders them as
# placeholder rows and reads the same sentinel.
MISSING_STATUS = "missing"


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


class DecisionStore:
    def __init__(self, project: str | None = None, client: AsyncClient | None = None) -> None:
        self.project = project or None
        self._client = client

    async def get(self, tick_id: str) -> DecisionRecord | None:
        snapshot = await self._document(tick_id).get()
        if not snapshot.exists:
            return None
        payload = snapshot.to_dict() or {}
        if payload.get("status") == MISSING_STATUS:
            return None
        return DecisionRecord.model_validate(payload)

    async def put(self, record: DecisionRecord) -> DecisionRecord:
        document = self._document(record.tick_id)
        payload = record.model_dump(mode="json", by_alias=True)
        try:
            await document.create(payload)
        except Conflict:
            snapshot = await document.get()
            if not snapshot.exists:
                raise
            stored = snapshot.to_dict() or {}
            if stored.get("status") != MISSING_STATUS:
                return DecisionRecord.model_validate(stored)
            # A real evaluation supersedes the placeholder recorded for this tick.
            await document.set(payload)
        return record

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _document(self, tick_id: str):
        return self._firestore.collection("decisions").document(tick_id)

    @property
    def _firestore(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(project=self.project)
        return self._client


class TradePlanStore:
    def __init__(self, project: str | None = None, client: AsyncClient | None = None) -> None:
        self.project = project or None
        self._client = client

    async def get_for_symbols(self, symbols: list[str]) -> TradePlan | None:
        if not symbols:
            return None
        plans: list[TradePlan] = []
        for symbol in set(symbols):
            query = self._firestore.collection("trade_plans").where(
                filter=FieldFilter("optionSymbol", "==", symbol)
            )
            async for snapshot in query.stream():
                plans.append(TradePlan.model_validate(snapshot.to_dict()))
        return max(plans, key=lambda plan: plan.created_at, default=None)

    async def put(self, plan: TradePlan) -> TradePlan:
        document = self._firestore.collection("trade_plans").document(plan.decision_id)
        try:
            await document.create(plan.model_dump(mode="json", by_alias=True))
        except Conflict:
            snapshot = await document.get()
            if not snapshot.exists:
                raise
            return TradePlan.model_validate(snapshot.to_dict())
        return plan

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @property
    def _firestore(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(project=self.project)
        return self._client
