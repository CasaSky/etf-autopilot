import csv
import io
import json
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest, load_json, validate_config
from etf_autopilot.engine import run_event
from etf_autopilot.history import (
    COLUMNS,
    config_for_dataset,
    dataset_receipt,
    import_history,
    validate_dataset,
    write_immutable_json,
)

from .conftest import ROOT

FIXTURE = ROOT / "fixtures/history_contract"


@pytest.fixture
def dataset():
    return import_history(FIXTURE / "manifest.json", FIXTURE / "monthly.csv")


@pytest.fixture
def import_inputs(tmp_path):
    manifest = load_json(FIXTURE / "manifest.json")
    rows = list(csv.DictReader(io.StringIO((FIXTURE / "monthly.csv").read_text())))

    def write(manifest_value=manifest, row_values=rows):
        manifest_path, csv_path = tmp_path / "manifest.json", tmp_path / "history.csv"
        manifest_path.write_text(json.dumps(manifest_value))
        with csv_path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(row_values)
        return import_history(manifest_path, csv_path)

    return manifest, rows, write


def rehash(dataset):
    dataset["manifest_hash"] = digest(dataset["manifest"])
    dataset["dataset_id"] = digest(
        {key: value for key, value in dataset.items() if key != "dataset_id"}
    )
    return dataset


def test_import_is_reproducible_and_provenance_is_explicit(dataset):
    assert dataset == import_history(FIXTURE / "manifest.json", FIXTURE / "monthly.csv")
    validate_dataset(dataset)
    receipt = dataset_receipt(dataset)
    assert receipt["origin"] == "synthetic_fixture"
    assert receipt["periods"] == 48
    assert receipt["broker_mapping"] == "unverified"
    assert receipt["verification"] == "provider_declarations_not_independently_verified"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("duplicate", "duplicate_history_observation"),
        ("month_gap", "history_month_gap_no_imputation"),
        ("asset_gap", "missing_history_asset_observation"),
        ("late", "history_not_available_before_execution"),
        ("same_month", "execution_must_be_in_following_month"),
        ("signal_month", "signal_period_mismatch"),
        ("gap", "history_execution_gap_exceeds_seven_days"),
        ("retrieval", "history_execution_after_retrieval"),
        ("nan", "non_finite_decimal"),
        ("zero", "invalid_total_return_index"),
        ("alignment", "unaligned_history_observations"),
        ("unknown_asset", "history_asset_not_in_manifest"),
        ("timezone", "timezone_required"),
    ],
)
def test_bad_data_is_rejected_before_any_evaluation(import_inputs, case, expected):
    manifest, rows, write = import_inputs
    if case == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif case == "month_gap":
        rows[:] = [row for row in rows if row["period"] != "2023-05"]
    elif case == "asset_gap":
        rows.pop(0)
    elif case == "late":
        rows[0]["available_at"] = rows[0]["execution_at"]
    elif case == "same_month":
        rows[0]["execution_at"] = rows[0]["signal_at"]
    elif case == "signal_month":
        rows[0]["period"] = "2021-12"
    elif case == "gap":
        rows[0]["execution_at"] = "2022-02-15T09:00:00Z"
    elif case == "retrieval":
        manifest["retrieved_at"] = "2021-01-01T00:00:00Z"
    elif case == "nan":
        rows[0]["signal_index"] = "NaN"
    elif case == "zero":
        rows[0]["execution_index"] = "0"
    elif case == "alignment":
        rows[0]["available_at"] = "2022-01-31T16:45:00Z"
    elif case == "unknown_asset":
        rows[0]["asset_id"] = "UNDECLARED"
    elif case == "timezone":
        rows[0]["signal_at"] = "2022-01-31T16:00:00"
    with pytest.raises(SafetyError, match=expected):
        write(manifest, rows)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("basis", "eur_total_return_indices_required"),
        ("currency", "eur_total_return_indices_required"),
        ("ucits", "declared_ucits_equity_security_required"),
        ("legacy", "declared_ucits_equity_security_required"),
        ("same_class", "distinct_equity_and_bond_instruments_required"),
        ("origin", "history_origin_required"),
        ("embedded_secret", "history_source_url_required"),
        ("tax", "unsupported_history_assumptions"),
        ("pretend_real", "real_history_requires_isin_format"),
    ],
)
def test_invalid_provenance_and_asset_metadata_are_rejected(import_inputs, case, expected):
    manifest, rows, write = import_inputs
    if case == "basis":
        manifest["price_basis"] = "unadjusted_close"
    elif case == "currency":
        manifest["currency"] = "USD"
    elif case == "ucits":
        manifest["instruments"]["RESEARCH_EQUITY"]["ucits"] = False
    elif case == "legacy":
        manifest["instruments"]["RESEARCH_EQUITY"]["type"] = "security"
    elif case == "same_class":
        manifest["instruments"]["RESEARCH_BOND"]["class"] = "equity"
    elif case == "origin":
        manifest.pop("origin")
        expected = "unsupported_history_manifest"
    elif case == "embedded_secret":
        manifest["source_url"] = "https://example.invalid/download?api_key=do-not-persist"
    elif case == "tax":
        manifest["assumptions"]["investor_tax"] = "silently_ignored"
    elif case == "pretend_real":
        manifest["origin"] = "declared_historical"
    with pytest.raises(SafetyError, match=expected):
        write(manifest, rows)


def test_declared_historical_data_stays_unverified(import_inputs):
    manifest, rows, write = import_inputs
    manifest["origin"] = "declared_historical"
    # Format-only identifiers for a contract test; no actual fund is selected or queried.
    manifest["instruments"]["RESEARCH_EQUITY"]["isin"] = "ZZ0000000011"
    manifest["instruments"]["RESEARCH_BOND"]["isin"] = "ZZ0000000022"
    receipt = dataset_receipt(write(manifest, rows))
    assert receipt["origin"] == "declared_historical"
    assert receipt["broker_mapping"] == "unverified"


def test_changed_normalized_content_cannot_keep_its_old_identity(dataset):
    dataset["rows"][0]["signals"]["RESEARCH_EQUITY"] = "999"
    with pytest.raises(SafetyError, match="dataset_content_hash_mismatch"):
        validate_dataset(dataset)


def test_timing_is_rechecked_even_with_new_content_hash(dataset):
    dataset["rows"][0]["available_at"] = dataset["rows"][0]["execution_at"]
    rehash(dataset)
    with pytest.raises(SafetyError, match="history_not_available_before_execution"):
        validate_dataset(dataset)


def test_config_binding_preserves_the_existing_strategy_weights_and_risk(config, dataset):
    before = deepcopy(config)
    research = config_for_dataset(config, dataset)
    validate_config(research)
    assert config == before
    assert research["strategy"] == before["strategy"]
    assert research["lookback_months"] == before["lookback_months"]
    assert research["risk"] == before["risk"]
    assert research["execution"] == before["execution"]
    assert research["assets"]["RESEARCH_EQUITY"]["weight"] == "0.70"
    assert research["assets"]["RESEARCH_BOND"]["weight"] == "0.20"
    assert research["data_contract"]["dataset_id"] == dataset["dataset_id"]


def test_research_index_contract_cannot_enter_broker_paper_workflow(config, dataset, store, events):
    research = config_for_dataset(config, dataset)
    with pytest.raises(SafetyError, match="historical_contract_requires_evaluation_runner"):
        run_event(store, research, events[0])


def test_dataset_artifacts_are_immutable_and_identical_imports_are_idempotent(dataset, tmp_path):
    path = tmp_path / "dataset.json"
    write_immutable_json(path, dataset)
    before = path.read_bytes()
    write_immutable_json(path, dataset)
    assert path.read_bytes() == before
    changed = deepcopy(dataset)
    changed["rows"].pop()
    with pytest.raises(SafetyError, match="immutable_artifact_exists"):
        write_immutable_json(path, changed)
    assert path.read_bytes() == before
