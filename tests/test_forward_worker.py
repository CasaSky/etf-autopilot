import io
from copy import deepcopy
from types import SimpleNamespace

import boto3
import pytest

from etf_autopilot.aws_handler import handler
from etf_autopilot.domain import SafetyError, canonical, digest
from etf_autopilot.forward_cloud import INPUT_PREFIX, STATE_KEY, dispatch, read_inputs

from .forward_s3_fixture import MemoryS3
from .test_forward_cloud import cloud as cloud
from .test_issuer import exports as exports
from .test_observations import captured as captured
from .test_observations import plan as plan


def inputs(setup, config):
    client, bucket, plan, runtime, _ = setup
    for name, value in (("config", config), ("plan", plan), ("runtime", runtime)):
        client.objects[bucket, f"{INPUT_PREFIX}/{name}.json"] = canonical(value).encode()
    return client, bucket, digest(runtime)


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected SDK construction, inference or notification")


def test_disabled_forward_action_does_not_construct_an_sdk_client(monkeypatch):
    monkeypatch.delenv("FORWARD_ENABLED", raising=False)
    monkeypatch.setattr(boto3, "client", forbidden)
    assert handler({"action": "forward_tick"}, None)["diagnosis"] == "forward_runtime_disabled"


def test_sealed_input_and_health_are_read_only_even_when_model_permission_is_off(cloud, config):
    setup = cloud()
    client, bucket, runtime_hash = inputs(setup, config)
    before = deepcopy(client.objects)
    client.calls.clear()
    result = dispatch(
        client,
        bucket,
        {"action": "forward_health"},
        runtime_hash=runtime_hash,
        allow_model=False,
        notify=forbidden,
    )
    assert not result["state_changes"] and not result["model_permission_enabled"]
    assert client.objects == before and all(method == "get" for method, _ in client.calls)
    assert set(key for _, key in client.calls) == {
        STATE_KEY,
        *(f"{INPUT_PREFIX}/{name}.json" for name in ("runtime", "config", "plan")),
    }
    with pytest.raises(SafetyError, match="forward_runtime_not_reviewed"):
        read_inputs(client, bucket, "0" * 64)


@pytest.mark.parametrize(
    "event",
    [
        {"action": "forward_tick", "scheduled_at": "2026-01-08T09:00:00Z", "model": "other"},
        {"action": "forward_tick"},
        {"action": "forward_health", "state_key": "other-project/state.json"},
    ],
)
def test_forward_requests_cannot_select_inputs_or_models(event):
    client = MemoryS3()
    with pytest.raises(SafetyError, match="unsupported_forward_worker_request"):
        dispatch(client, "fixture-bucket", event, runtime_hash=None, allow_model=False)
    assert client.calls == []


def test_input_size_failure_closes_body_without_parsing():
    body = io.BytesIO(b"{}")
    client = SimpleNamespace(get_object=lambda **_: {"Body": body, "ContentLength": 100_001})
    with pytest.raises(SafetyError, match="forward_runtime_input_size_limit"):
        read_inputs(client, "fixture-bucket", "0" * 64)
    assert body.closed


def test_alert_delivery_test_can_precede_transfer_and_never_resends(cloud, config):
    setup = cloud()
    client, bucket, runtime_hash = inputs(setup, config)
    del client.objects[bucket, STATE_KEY]
    messages = []
    for _ in range(2):
        result = dispatch(
            client,
            bucket,
            {"action": "forward_alert_test"},
            runtime_hash=runtime_hash,
            allow_model=False,
            notify=messages.append,
        )
        assert result["notification"] == "sent"
        assert result["model_calls"] == result["orders"] == 0
    assert len(messages) == 1
    assert not any(key == STATE_KEY for _, key in client.calls[-8:])
    assert all("email" not in canonical(value) for value in messages)


def test_handler_failure_remains_a_lambda_error_for_alarm_and_dead_letter_delivery(monkeypatch):
    import etf_autopilot.forward_cloud as module

    monkeypatch.setenv("FORWARD_ENABLED", "true")
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setattr(boto3, "client", lambda *_, **__: MemoryS3())
    monkeypatch.setattr(
        module,
        "dispatch",
        lambda *_, **__: {"status": "capture_failed", "orders": 0, "mode": "paper"},
    )
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 180_000)
    with pytest.raises(SafetyError, match="forward_operation_requires_review"):
        handler({"action": "forward_tick", "scheduled_at": "2026-01-08T09:00:00Z"}, context)


def test_handler_sanitizes_unexpected_upstream_errors(monkeypatch, capsys):
    import etf_autopilot.forward_cloud as module

    monkeypatch.setenv("FORWARD_ENABLED", "true")
    monkeypatch.setenv("STATE_BUCKET", "fixture-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setattr(boto3, "client", lambda *_, **__: MemoryS3())

    def upstream(*_, **__):
        raise RuntimeError("fixture-confidential-upstream-body")

    monkeypatch.setattr(module, "dispatch", upstream)
    with pytest.raises(SafetyError, match="^forward_runtime_failure$"):
        handler({"action": "forward_health"}, None)
    assert "fixture-confidential-upstream-body" not in capsys.readouterr().out
