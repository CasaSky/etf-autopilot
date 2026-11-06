import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from etf_autopilot.capture import capture_issuer
from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.observations import (
    observations_for_capture,
    save_observations,
    validate_observations,
)

from .test_capture import fetcher
from .test_capture import plan as plan
from .test_issuer import exports as exports


@pytest.fixture
def captured(plan, exports, tmp_path):
    value = load_json(plan)
    value["spec"]["signal_end"] = "2025-11"
    plan.write_text(json.dumps(value))
    out = tmp_path / "captured"
    receipt = capture_issuer(
        plan, out, fetch=fetcher(exports, []), clock=lambda: datetime(2026, 1, 8, tzinfo=UTC)
    )
    return plan, out, receipt


def test_daily_derivation_preserves_receipt_time_and_observed_tail(captured, tmp_path):
    plan, out, receipt = captured
    before = (out / "capture.json").read_bytes()
    data = save_observations(plan, out, tmp_path / "daily.json")
    assert data["capture_id"] == receipt["evidence_id"]
    assert data["captured_at"] == receipt["received_at"]
    assert data["rows"][-1]["date"] == "2026-01-07"
    monthly = load_json(out / "normalized/dataset.json")
    assert monthly["rows"][-1]["period"] == "2025-11"
    assert monthly["rows"][-1]["execution_at"].startswith("2025-12")
    assert data["policy"]["units"] == "research_index_units_not_broker_shares"
    assert save_observations(plan, out, tmp_path / "daily.json") == data
    assert (out / "capture.json").read_bytes() == before


def test_daily_derivation_checks_original_source_before_reading_it(captured):
    plan, out, _ = captured
    (out / "bond.xls").write_bytes(b"altered source")
    with pytest.raises(SafetyError, match="issuer_capture_file_changed"):
        observations_for_capture(plan, out)


def test_daily_derivation_requires_completed_capture_without_creating_one(plan, tmp_path):
    with pytest.raises(SafetyError, match="completed_capture_required"):
        observations_for_capture(plan, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "case,code",
    [
        ("hash", "daily_observations_hash_mismatch"),
        ("date", "daily_observation_date_invalid_or_not_completed"),
        ("basis", "daily_index_basis_must_start_at_100"),
        ("units", "unsupported_daily_index_policy"),
        ("clock", "daily_capture_time_mismatch"),
    ],
)
def test_daily_input_cannot_hide_revisions_or_change_its_basis(captured, case, code):
    plan, out, _ = captured
    data = deepcopy(observations_for_capture(plan, out))
    if case == "date":
        data["rows"][-1]["date"] = "2026-01-08"
    if case in {"hash", "basis"}:
        data["rows"][0]["values"]["RESEARCH_EQUITY"] = "99"
    if case == "units":
        data["policy"]["units"] = "broker_shares"
    if case == "clock":
        data["captured_at"] = "2026-01-09T00:00:00Z"
    if case != "hash":
        data["observation_id"] = digest({k: v for k, v in data.items() if k != "observation_id"})
    with pytest.raises(SafetyError, match=code):
        validate_observations(data)
