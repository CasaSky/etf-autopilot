import io

import boto3
import pytest
from botocore.stub import Stubber

from etf_autopilot.domain import SafetyError, canonical
from etf_autopilot.forward_artifacts import PREFIX, ForwardArtifacts
from etf_autopilot.history import write_immutable_json

from .forward_s3_fixture import MemoryS3

DAY = "2026-01-08"


@pytest.fixture
def artifacts():
    return ForwardArtifacts(MemoryS3(), "fixture-bucket")


def test_artifact_sdk_calls_are_conditional_encrypted_and_exactly_bound():
    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="fixture-access",
        aws_secret_access_key="fixture-secret",
    )
    store = ForwardArtifacts(client, "fixture-bucket")
    key = f"{PREFIX}/{DAY}/context.json"
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "fixture-bucket",
                "Key": key,
                "Body": b"{}",
                "IfNoneMatch": "*",
                "ServerSideEncryption": "AES256",
                "ContentType": "application/json",
            },
        )
        assert not store.put(DAY, "context.json", b"{}")["duplicate"]
        stub.add_client_error(
            "put_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params={
                "Bucket": "fixture-bucket",
                "Key": key,
                "Body": b"{}",
                "IfNoneMatch": "*",
                "ServerSideEncryption": "AES256",
                "ContentType": "application/json",
            },
        )
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(b"{}"), "ContentLength": 2},
            {"Bucket": "fixture-bucket", "Key": key},
        )
        assert store.put(DAY, "context.json", b"{}")["duplicate"]
        stub.assert_no_pending_responses()


def test_unknown_write_is_not_retried_and_conflicting_bytes_cannot_overwrite(artifacts):
    key = f"{PREFIX}/{DAY}/context.json"
    artifacts.client.unknown_write_key = key
    with pytest.raises(SafetyError, match="forward_artifact_write_unknown"):
        artifacts.put(DAY, "context.json", b"first")
    assert artifacts.client.calls == [("put", key)]
    with pytest.raises(SafetyError, match="immutable_forward_artifact_conflict"):
        artifacts.put(DAY, "context.json", b"second")
    assert artifacts.get(DAY, "context.json") == b"first"


@pytest.mark.parametrize(
    "day,name",
    [("../2026-01-08", "context.json"), (DAY, "../state.json"), (DAY, "capture/secret.txt")],
)
def test_artifact_paths_fail_before_sdk_access(artifacts, day, name):
    with pytest.raises(SafetyError):
        artifacts.put(day, name, b"{}")
    assert not artifacts.client.calls


def populate(directory, context):
    directory.mkdir()
    for name, value in {
        "context.json": context,
        "plan.json": {"fixture": True},
        "capture/capture.json": {"fixture": True},
        "observations.json": {"fixture": True},
    }.items():
        write_immutable_json(directory / name, value)


def test_checkpoint_marker_is_last_and_restore_binds_context_and_bytes(artifacts, tmp_path):
    context = {"version": 1, "fixture": True}
    source = tmp_path / "source"
    populate(source, context)
    receipt = artifacts.checkpoint(DAY, source, context)
    assert artifacts.client.calls[-1] == ("put", f"{PREFIX}/{DAY}/ready.json")
    destination = tmp_path / "restored"
    assert artifacts.restore(DAY, destination, context) == receipt
    assert (destination / "context.json").read_bytes() == canonical(context).encode() + b"\n"
    assert (destination / "context.json").stat().st_mode & 0o777 == 0o600
    artifacts.client.objects["fixture-bucket", f"{PREFIX}/{DAY}/observations.json"] = b"changed"
    with pytest.raises(SafetyError, match="forward_capture_artifact_changed"):
        artifacts.restore(DAY, tmp_path / "tampered", context)


def test_failed_capture_checkpoint_never_becomes_ready(artifacts, tmp_path):
    context = {"fixture": True}
    source = tmp_path / "partial"
    source.mkdir()
    write_immutable_json(source / "context.json", context)
    write_immutable_json(source / "capture/capture.failed.json", {"failed": True})
    artifacts.checkpoint(DAY, source, context, complete=False)
    assert artifacts.get(DAY, "ready.json") is None
    assert artifacts.restore(DAY, tmp_path / "restore", context) is None


def test_wrong_context_and_oversized_body_fail_without_a_ready_marker(artifacts, tmp_path):
    source = tmp_path / "source"
    populate(source, {"fixture": True})
    with pytest.raises(SafetyError, match="forward_capture_context_changed"):
        artifacts.checkpoint(DAY, source, {"fixture": False})
    assert not artifacts.client.calls
    with pytest.raises(SafetyError, match="forward_artifact_size_limit"):
        artifacts.put(DAY, "context.json", b"x" * 100_001)
    assert not artifacts.client.calls
