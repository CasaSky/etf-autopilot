import io
import json
import zipfile

import boto3
import pytest
from botocore.stub import Stubber

from etf_autopilot.aws_handler import handler
from etf_autopilot.domain import SafetyError, canonical
from etf_autopilot.engine import initial_state, run_event
from etf_autopilot.store import MAX_STATE_BYTES, S3Store
from scripts.package_lambda import main as package_lambda

from .conftest import ROOT


@pytest.fixture
def s3():
    # Explicit fixture credentials prevent any metadata-service/credential-provider access.
    return boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="fixture-access",
        aws_secret_access_key="fixture-secret",
    )


def get_response(state, etag='"etag-1"'):
    body = canonical(state).encode()
    return {"Body": io.BytesIO(body), "ETag": etag, "ContentLength": len(body)}


def test_s3_initialization_requires_if_none_match(s3):
    store = S3Store(s3, "fixture-bucket")
    with Stubber(s3) as stub:
        stub.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params={"Bucket": store.bucket, "Key": store.key},
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": store.bucket,
                "Key": store.key,
                "Body": b'{"version":1}',
                "ContentType": "application/json",
                "IfNoneMatch": "*",
            },
        )
        assert store.update(lambda _: ("created", {"version": 1})) == "created"
        stub.assert_no_pending_responses()


def test_s3_updates_require_matching_etag(s3):
    store = S3Store(s3, "fixture-bucket")
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object", get_response({"version": 1}), {"Bucket": store.bucket, "Key": store.key}
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": store.bucket,
                "Key": store.key,
                "Body": b'{"version":2}',
                "ContentType": "application/json",
                "IfMatch": '"etag-1"',
            },
        )
        assert store.update(lambda _: ("updated", {"version": 2})) == "updated"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("PreconditionFailed", 412, "concurrent_state_change_replay_same_event"),
        ("ConditionalRequestConflict", 409, "concurrent_state_change_replay_same_event"),
        ("InternalError", 500, "state_write_unknown_replay_same_event"),
    ],
)
def test_s3_conflicts_and_unknown_write_outcomes_do_not_auto_retry(s3, code, status, expected):
    store = S3Store(s3, "fixture-bucket")
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object", get_response({"version": 1}), {"Bucket": store.bucket, "Key": store.key}
        )
        stub.add_client_error(
            "put_object",
            service_error_code=code,
            http_status_code=status,
            expected_params={
                "Bucket": store.bucket,
                "Key": store.key,
                "Body": b'{"version":2}',
                "ContentType": "application/json",
                "IfMatch": '"etag-1"',
            },
        )
        with pytest.raises(SafetyError, match=expected):
            store.update(lambda _: (None, {"version": 2}))
        stub.assert_no_pending_responses()


def test_local_transaction_rollback_does_not_leave_partial_state(store, config, events):
    run_event(store, config, events[0])
    before = store.read()

    def fail(state):
        state["accounts"]["strategy"]["portfolio"]["cash"] = "0.00"
        raise SafetyError("injected_failure")

    with pytest.raises(SafetyError, match="injected_failure"):
        store.update(fail)
    assert store.read() == before


def test_state_growth_is_bounded(store):
    with pytest.raises(SafetyError, match="state_size_limit"):
        store.update(lambda _: (None, {"padding": "x" * MAX_STATE_BYTES}))


def test_lambda_paper_environment_and_fixed_input_key(monkeypatch, s3, config, events, tmp_path):
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: s3)
    # Package places this config next to the handler; emulate the installed path without
    # writing a config into source code.
    import etf_autopilot.aws_handler as module

    monkeypatch.setattr(module, "load_json", lambda _: config)
    event_body = json.dumps(events[0]).encode()
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(event_body), "ContentLength": len(event_body)},
            {"Bucket": "fixture-bucket", "Key": "paper/inputs/current.json"},
        )
        stub.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params={"Bucket": "fixture-bucket", "Key": "paper/state.json"},
        )
        from botocore.stub import ANY

        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "fixture-bucket",
                "Key": "paper/state.json",
                "Body": ANY,
                "ContentType": "application/json",
                "IfNoneMatch": "*",
            },
        )
        result = handler({"action": "run", "input_key": "ignored-untrusted-input"}, None)
        assert result["status"] == "completed"
        stub.assert_no_pending_responses()
    monkeypatch.setenv("EXECUTION_MODE", "live")
    with pytest.raises(SafetyError, match="paper_only_worker"):
        handler({"action": "run"}, None)


def test_lambda_health_does_not_call_broker_or_model(monkeypatch, s3, config):
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: s3)
    import etf_autopilot.aws_handler as module

    monkeypatch.setattr(module, "load_json", lambda _: config)
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object",
            get_response(initial_state(config)),
            {"Bucket": "fixture-bucket", "Key": "paper/state.json"},
        )
        assert handler({"action": "health"}, None)["ai"] == "disabled"
        stub.assert_no_pending_responses()


def test_lambda_health_exposes_unknown_allocation_without_retry(monkeypatch, s3, config):
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: s3)
    import etf_autopilot.aws_handler as module

    monkeypatch.setattr(module, "load_json", lambda _: config)
    state = initial_state(config)
    state["operations"]["allocations"] = [
        {"id": "allocation:fixture", "status": "reserved", "source": "scripted_fixture"}
    ]
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object",
            get_response(state),
            {"Bucket": "fixture-bucket", "Key": "paper/state.json"},
        )
        result = handler({"action": "health"}, None)
        assert result["status"] == "awaiting_allocation_result" and result["ai"] == "disabled"
        stub.assert_no_pending_responses()


def test_lambda_cycle_uses_runtime_clock_and_never_writes_stale_input(
    monkeypatch, s3, config, events
):
    from datetime import UTC, datetime

    import etf_autopilot.aws_handler as module
    import etf_autopilot.cycle as cycle

    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 25, tzinfo=UTC)

    monkeypatch.setattr(cycle, "datetime", Clock)
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: s3)
    monkeypatch.setattr(module, "load_json", lambda _: config)
    body = json.dumps(events[0]).encode()
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(body), "ContentLength": len(body)},
            {"Bucket": "fixture-bucket", "Key": "paper/inputs/current.json"},
        )
        result = handler({"action": "cycle", "at": events[0]["at"]}, None)
        assert result["status"] == "waiting_for_input" and result["orders"] == 0
        stub.assert_no_pending_responses()


def test_lambda_package_contains_only_allowlisted_source_and_fixture_config():
    package_lambda()
    with zipfile.ZipFile(ROOT / "var/lambda.zip") as archive:
        files = archive.namelist()
        assert "etf_autopilot/aws_handler.py" in files
        assert "etf_autopilot/paper.json" in files
        assert all(name.startswith("etf_autopilot/") for name in files)
        assert not any(
            any(part in name for part in (".env", ".aws", ".git", "sqlite")) for name in files
        )
