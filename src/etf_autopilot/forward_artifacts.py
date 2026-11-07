"""Bounded immutable S3 capture artifacts; no provider or financial side effects."""

import hashlib
import json
from pathlib import Path

from .domain import SafetyError, canonical, digest, load_json
from .nav import iso_day

PREFIX = "paper/forward/ticks"
CONTROL_FILES = frozenset(
    {
        "ready.json",
        "outcome.json",
        "failure.json",
        "alert.json",
        "alert-result.json",
        "alert-test.json",
        "alert-test-result.json",
    }
)
LIMITS = {
    "plan.json": 100_000,
    "context.json": 100_000,
    "capture/capture.pending.json": 100_000,
    "capture/capture.failed.json": 100_000,
    "capture/capture.json": 100_000,
    "capture/spec.json": 100_000,
    "capture/equity.xlsx": 8_000_000,
    "capture/equity.xls": 32_000_000,
    "capture/bond.xls": 32_000_000,
    "capture/ecb.xml": 15_000_000,
    "capture/normalized/manifest.json": 1_000_000,
    "capture/normalized/monthly.csv": 5_000_000,
    "capture/normalized/dataset.json": 5_000_000,
    "observations.json": 5_000_000,
    "ready.json": 100_000,
    "outcome.json": 5_000_000,
    "failure.json": 100_000,
    "alert.json": 100_000,
    "alert-result.json": 100_000,
    "alert-test.json": 100_000,
    "alert-test-result.json": 100_000,
}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


class ForwardArtifacts:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def _key(self, day, name):
        if iso_day(day).isoformat() != day or name not in LIMITS:
            raise SafetyError("unsupported_forward_artifact")
        return f"{PREFIX}/{day}/{name}"

    def get(self, day, name):
        from botocore.exceptions import BotoCoreError, ClientError

        key = self._key(day, name)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                if not 0 < response["ContentLength"] <= LIMITS[name]:
                    raise SafetyError("forward_artifact_size_limit")
                raw = body.read(LIMITS[name] + 1)
            finally:
                body.close()
            if len(raw) != response["ContentLength"]:
                raise SafetyError("forward_artifact_length_mismatch")
            return raw
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise SafetyError("forward_artifact_read_failed") from None
        except BotoCoreError, OSError:
            raise SafetyError("forward_artifact_read_failed") from None

    def get_json(self, day, name):
        raw = self.get(day, name)
        if raw is None:
            return None
        try:
            return json.loads(raw, parse_float=str)
        except ValueError, UnicodeError:
            raise SafetyError("invalid_forward_artifact_json") from None

    def put(self, day, name, raw):
        from botocore.exceptions import BotoCoreError, ClientError

        key = self._key(day, name)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMITS[name]:
            raise SafetyError("forward_artifact_size_limit")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=raw,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                ContentType=(
                    "application/json" if name.endswith(".json") else "application/octet-stream"
                ),
            )
            return {"duplicate": False}
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                prior = self.get(day, name)
                if prior == raw:
                    return {"duplicate": True}
                raise SafetyError("immutable_forward_artifact_conflict") from None
            raise SafetyError("forward_artifact_write_unknown") from None
        except BotoCoreError, OSError:
            raise SafetyError("forward_artifact_write_unknown") from None

    def put_json(self, day, name, value):
        return self.put(day, name, canonical(value).encode())

    def checkpoint(self, day, directory, context, *, complete=True):
        directory = Path(directory)
        if load_json(directory / "context.json") != context:
            raise SafetyError("forward_capture_context_changed")
        files, total = {}, 0
        for name in LIMITS:
            if name in CONTROL_FILES:
                continue
            path = directory / name
            if not path.is_file():
                continue
            if not 0 < path.stat().st_size <= LIMITS[name]:
                raise SafetyError("forward_artifact_size_limit")
            raw = path.read_bytes()
            total += len(raw)
            if total > 80_000_000:
                raise SafetyError("forward_capture_artifact_total_limit")
            self.put(day, name, raw)
            files[name] = {"sha256": _hash(raw), "bytes": len(raw)}
        if complete and not {
            "plan.json",
            "context.json",
            "capture/capture.json",
            "observations.json",
        } <= set(files):
            raise SafetyError("verified_forward_capture_required")
        receipt = {"version": 1, "context_hash": digest(context), "files": files}
        receipt["receipt_id"] = digest(receipt)
        if complete:
            self.put_json(day, "ready.json", receipt)
        return receipt

    def restore(self, day, directory, context):
        receipt = self.get_json(day, "ready.json")
        if receipt is None:
            return None
        if (
            receipt.get("version") != 1
            or receipt.get("context_hash") != digest(context)
            or receipt.get("receipt_id")
            != digest({k: v for k, v in receipt.items() if k != "receipt_id"})
        ):
            raise SafetyError("forward_capture_receipt_changed")
        files = receipt.get("files")
        allowed = set(LIMITS) - CONTROL_FILES
        if (
            not isinstance(files, dict)
            or not {"plan.json", "context.json", "capture/capture.json", "observations.json"}
            <= set(files)
            or not set(files) <= allowed
        ):
            raise SafetyError("unsupported_forward_capture_files")
        total = 0
        for name, evidence in files.items():
            raw = self.get(day, name)
            if (
                raw is None
                or not isinstance(evidence, dict)
                or set(evidence) != {"sha256", "bytes"}
                or evidence["sha256"] != _hash(raw)
                or evidence["bytes"] != len(raw)
            ):
                raise SafetyError("forward_capture_artifact_changed")
            total += len(raw)
            if total > 80_000_000:
                raise SafetyError("forward_capture_artifact_total_limit")
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists():
                if _hash(path.read_bytes()) != _hash(raw):
                    raise SafetyError("forward_capture_local_artifact_changed")
            else:
                with path.open("xb") as target:
                    target.write(raw)
                path.chmod(0o600)
        if load_json(Path(directory) / "context.json") != context:
            raise SafetyError("forward_capture_context_changed")
        return receipt
