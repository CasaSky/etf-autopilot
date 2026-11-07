import json
from argparse import Namespace
from copy import deepcopy

import boto3
import pytest

from etf_autopilot.cli import main
from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.forward_control import trial_identity
from etf_autopilot.forward_operator import _bundle, _cloud_store

from .test_forward import captured as captured
from .test_forward import commit
from .test_forward import daily as daily
from .test_forward import exports as exports
from .test_forward import plan as plan


def forbidden(*args, **kwargs):
    raise AssertionError("Preparing operator evidence must not use AWS")


def test_prepare_runtime_then_freeze_via_cli_preserves_financial_history(
    store, config, daily, plan, tmp_path, capsys, monkeypatch
):
    commit(store, config, daily)
    before = deepcopy(store.read())
    monkeypatch.setattr(boto3, "Session", forbidden)
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps(config))
    base = ["--config", str(configuration), "--state", str(store.path)]
    output = tmp_path / "runtime.json"
    assert main([*base, "forward-runtime", str(plan), "--model-enabled", "--out", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cloud_writes"] == result["model_calls"] == 0
    assert result["runtime_hash"] == digest(load_json(output))
    assert load_json(output)["trial_id"] == trial_identity(before)
    assert store.read() == before
    bundle = tmp_path / "handoff.json"
    assert (
        main(
            [
                *base,
                "forward-transfer-freeze",
                "--target",
                "aws",
                "--expected-state-hash",
                digest(before),
                "--out",
                str(bundle),
            ]
        )
        == 0
    )
    frozen = _bundle(bundle)
    assert frozen == store.read() and frozen["accounts"] == before["accounts"]
    assert frozen["operations"] == before["operations"]
    assert frozen["control"]["status"] == "frozen"


@pytest.mark.parametrize(
    "missing", ["allow_cloud_state", "profile", "account_id", "bucket", "region"]
)
def test_transfer_refuses_cloud_access_without_all_explicit_parameters(monkeypatch, missing):
    monkeypatch.setattr(boto3, "Session", forbidden)
    values = {
        "allow_cloud_state": True,
        "profile": "fixture-profile",
        "account_id": "123456789012",
        "bucket": "etf-autopilot-paper-123456789012-eu-central-1",
        "region": "eu-central-1",
    }
    values[missing] = None
    with pytest.raises(SafetyError, match="explicit_owned_cloud_state_parameters_required"):
        _cloud_store(Namespace(**values))


def test_changed_bundle_is_rejected_before_any_store_access(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(
        json.dumps({"version": 1, "snapshot": {"changed": True}, "snapshot_hash": "0" * 64})
    )
    with pytest.raises(SafetyError, match="forward_transfer_bundle_changed"):
        _bundle(path)
