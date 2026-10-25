"""Atomic whole-state storage for the low-frequency paper engine.

All broker effects are simulated inside the transaction. This does not claim
exactly-once execution against an external financial service.
"""

import json
import sqlite3
from pathlib import Path

from .domain import SafetyError, canonical

MAX_STATE_BYTES = 5_000_000


class LocalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def update(self, operation):
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT body FROM state WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else None
            result, updated = operation(state)
            body = canonical(updated)
            if len(body.encode()) > MAX_STATE_BYTES:
                raise SafetyError("state_size_limit_archive_required")
            conn.execute("INSERT OR REPLACE INTO state (id,body) VALUES (1,?)", (body,))
            conn.commit()
            return result

    def read(self):
        if not self.path.exists():
            raise SafetyError("state_not_initialized")
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT body FROM state WHERE id=1").fetchone()
        if not row:
            raise SafetyError("state_not_initialized")
        return json.loads(row[0])


class S3Store:
    """Versioned S3 snapshot with compare-and-swap. No nonconditional writes."""

    def __init__(self, client, bucket: str, key: str = "paper/state.json"):
        self.client, self.bucket, self.key = client, bucket, key

    def _load(self):
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key)
            if obj["ContentLength"] > MAX_STATE_BYTES:
                raise SafetyError("state_size_limit_archive_required")
            return json.loads(obj["Body"].read()), obj["ETag"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None, None
            raise SafetyError("state_read_failed") from None

    def read(self):
        state, _ = self._load()
        if state is None:
            raise SafetyError("state_not_initialized")
        return state

    def update(self, operation):
        from botocore.exceptions import ClientError

        state, etag = self._load()
        result, updated = operation(state)
        body = canonical(updated).encode()
        if len(body) > MAX_STATE_BYTES:
            raise SafetyError("state_size_limit_archive_required")
        precondition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=body,
                ContentType="application/json",
                **precondition,
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                raise SafetyError("concurrent_state_change_replay_same_event") from None
            raise SafetyError("state_write_unknown_replay_same_event") from None
        return result
