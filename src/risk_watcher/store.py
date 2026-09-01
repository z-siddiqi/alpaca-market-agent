from datetime import UTC, datetime, timedelta

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from risk_watcher.models import TradePlan


class Store:
    def __init__(self, project: str, owner: str, lease_seconds: int) -> None:
        self.client = firestore.Client(project=project)
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.lease_document = None

    def trade_plan(self, symbol: str) -> TradePlan | None:
        query = self.client.collection("trade_plans").where(
            filter=FieldFilter("optionSymbol", "==", symbol)
        )
        plans = [TradePlan.from_payload(snapshot.to_dict()) for snapshot in query.stream()]
        return max(plans, key=lambda plan: plan.created_at, default=None)

    def acquire_lease(self, trading_date: str) -> bool:
        document = self.client.collection("runtime_leases").document(
            f"position-watcher-{trading_date}"
        )
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire(transaction):
            now = datetime.now(UTC)
            snapshot = document.get(transaction=transaction)
            payload = snapshot.to_dict() if snapshot.exists else {}
            expires_at = payload.get("expiresAt")
            if expires_at is not None and expires_at > now and payload.get("owner") != self.owner:
                return False
            transaction.set(
                document,
                {
                    "owner": self.owner,
                    "heartbeatAt": now,
                    "expiresAt": now + timedelta(seconds=self.lease_seconds),
                },
            )
            return True

        acquired = bool(acquire(transaction))
        if acquired:
            self.lease_document = document
        return acquired

    def heartbeat(self) -> bool:
        if self.lease_document is None:
            return False
        transaction = self.client.transaction()
        document = self.lease_document

        @firestore.transactional
        def renew(transaction):
            now = datetime.now(UTC)
            snapshot = document.get(transaction=transaction)
            payload = snapshot.to_dict() if snapshot.exists else {}
            if payload.get("owner") != self.owner:
                return False
            transaction.update(
                document,
                {
                    "heartbeatAt": now,
                    "expiresAt": now + timedelta(seconds=self.lease_seconds),
                },
            )
            return True

        return bool(renew(transaction))
