"""Exact integration admission fence, retained separately from rollout cohorts.

Counters describe only this positively identified process. They never certify an
unsupervised predecessor; production cutover must separately witness that drain.
"""
import asyncio
import json
import os
from pathlib import Path
from services.writer_attestor import scopes, join_owned, AttestationUnavailable


class LegacyDrain:
    def __init__(self, configured=None):
        self.configured = scopes("WRITER_LEGACY_DRAIN_SCOPES") if configured is None else configured
        self.active = {}
        self.acquired = {}
        self.joined = {}
        self.publish()

    @staticmethod
    def key(scope):
        return str(scope.get("integration_id", "")), scope.get("channel", "TELEGRAM")

    def fenced(self, scope):
        return any(self.key(row) == self.key(scope) for row in self.configured)

    async def run(self, scope, factory):
        if self.fenced(scope):
            raise AttestationUnavailable()
        key = self.key(scope)
        task = asyncio.create_task(factory())
        self.active.setdefault(key, set()).add(task)
        self.acquired[key] = self.acquired.get(key, 0) + 1
        self.publish()
        try:
            result = await join_owned(task)
            if result.cancelled:
                raise asyncio.CancelledError()
            if result.error is not None:
                raise result.error
            return result.value
        finally:
            self.active[key].discard(task)
            self.joined[key] = self.joined.get(key, 0) + 1
            self.publish()

    async def activate(self, scope):
        # Fence before awaiting existing original work. No time or task disappearance
        # can replace an actual successful join of each acquired task.
        if not self.fenced(scope):
            self.configured.append(dict(scope))
        pending = tuple(self.active.get(self.key(scope), ()))
        self.publish()
        if pending:
            await join_owned(asyncio.gather(*pending, return_exceptions=True))
        self.publish()
        return self.receipts()

    def receipts(self):
        rows = []
        for row in self.configured:
            key = self.key(row)
            active = len(self.active.get(key, ()))
            rows.append({**row, "supplier_instance_id": os.environ.get("WRITER_CHILD_SUPPLIER"),
                "process_incarnation_id": os.environ.get("WRITER_CHILD_PROCESS"),
                "state": "DRAINING" if active else "DRAINED", "active_count": active,
                "acquired_count": self.acquired.get(key, 0), "joined_count": self.joined.get(key, 0),
                "current_process_only": True})
        return rows

    def publish(self):
        path = os.environ.get("WRITER_CHILD_DRAIN_RECEIPT")
        if path:
            target = Path(path)
            temporary = target.with_suffix(".new")
            temporary.write_text(json.dumps(self.receipts(), sort_keys=True))
            os.chmod(temporary, 0o600)
            temporary.replace(target)


legacy_drain = LegacyDrain()
