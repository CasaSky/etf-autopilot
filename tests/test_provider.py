import json
import subprocess
import sys
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.evaluation import EvaluationRegistry, evaluation_markdown, protocol_template
from etf_autopilot.history import config_for_dataset, validate_dataset
from etf_autopilot.providers import import_eodhd

from .conftest import ROOT


@pytest.fixture
def exports(tmp_path):
    directory = tmp_path / "daily"
    subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "examples/provider_demo.py"),
            "--output-dir",
            str(directory),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return directory


def convert(directory, output=None):
    return import_eodhd(
        directory / "spec.json",
        directory / "equity.json",
        directory / "bond.json",
        output or directory / "converted",
    )


def change(path, transform):
    value = load_json(path)
    transform(value)
    path.write_text(json.dumps(value))


def test_provider_conversion_has_explicit_coverage_adjustments_and_source_hashes(exports):
    dataset = convert(exports)
    validate_dataset(dataset)
    assert len(dataset["rows"]) == 48
    assert dataset["manifest"]["price_basis"] == "unitized_adjusted_close_proxy"
    assert dataset["rows"][0]["signals"]["RESEARCH_EQUITY"] == "100"
    assert dataset["rows"][0]["execution_at"] == "2022-02-02T23:59:59+00:00"
    assert dataset["manifest"]["provenance"]["availability"] == "assumed_24h_after_utc_day_end"
    assert len(dataset["manifest"]["provenance"]["sources"]["RESEARCH_EQUITY"]["raw_sha256"]) == 64
    assert convert(exports) == dataset


def test_full_provider_to_registered_evaluation_keeps_proxy_limits(exports, config):
    dataset = convert(exports)
    windows = {
        "development": {"start": "2022-12", "end": "2023-11"},
        "validation": {"start": "2023-12", "end": "2024-11"},
        "holdout": {"start": "2024-12", "end": "2026-01"},
    }
    protocol = protocol_template(dataset, config, windows)
    registry = EvaluationRegistry(exports / "evaluation.sqlite3")
    registry.register(protocol, dataset, config)
    result = registry.run(protocol, dataset, config, "development")
    assert result["price_basis"] == "unitized_adjusted_close_proxy"
    report = evaluation_markdown(result)
    assert "adjusted-close research proxies" in report
    assert "not verified point-in-time" in report
    assert result["model_calls"] == 0 and result["automatic_promotion"] is False
    assert (
        config_for_dataset(config, dataset)["data_contract"]["price_basis"]
        == "unitized_adjusted_close_proxy"
    )


def test_currency_binding_cannot_relabel_usd_or_gbp_as_eur(exports):
    change(exports / "spec.json", lambda s: s["bindings"]["RESEARCH_EQUITY"].update(currency="USD"))
    with pytest.raises(SafetyError, match="provider_identity_or_currency_mismatch"):
        convert(exports, exports / "rejected")
    assert not (exports / "rejected").exists()


def test_wrong_isin_binding_cannot_use_the_right_symbol(exports):
    change(exports / "spec.json", lambda s: s["bindings"]["RESEARCH_EQUITY"].update(isin="wrong"))
    with pytest.raises(SafetyError, match="provider_identity_or_currency_mismatch"):
        convert(exports)


def test_raw_close_cannot_substitute_for_missing_adjusted_close(exports):
    change(exports / "equity.json", lambda rows: rows[0].pop("adjusted_close"))
    with pytest.raises(SafetyError, match="unsupported_eodhd_daily_bar"):
        convert(exports)


def test_duplicate_daily_bar_is_not_averaged_or_silently_removed(exports):
    change(exports / "equity.json", lambda rows: rows.append(deepcopy(rows[0])))
    with pytest.raises(SafetyError, match="duplicate_provider_bar"):
        convert(exports)


def test_missing_asset_bar_rejects_calendar_intersection_imputation(exports):
    change(exports / "bond.json", lambda rows: rows.pop(20))
    with pytest.raises(SafetyError, match="provider_calendars_differ_or_empty"):
        convert(exports)


def test_full_missing_month_cannot_be_dropped_from_requested_coverage(exports):
    for role in ("equity", "bond"):
        change(
            exports / f"{role}.json",
            lambda rows: rows.__setitem__(
                slice(None), [r for r in rows if not r["date"].startswith("2023-04")]
            ),
        )
    with pytest.raises(SafetyError, match="provider_bar_gap_requires_review"):
        convert(exports)


def test_signal_close_is_not_reused_as_execution(exports):
    for role in ("equity", "bond"):
        change(
            exports / f"{role}.json",
            lambda rows: rows.__setitem__(
                slice(None), [r for r in rows if r["date"] < "2026-01-01"]
            ),
        )
    with pytest.raises(SafetyError, match="missing_later_provider_execution_bar"):
        convert(exports)


def test_retrieval_precedes_all_source_bars_not_just_selected_window(exports):
    change(
        exports / "spec.json",
        lambda s: s.update(retrieved_at="2025-12-31T23:59:59Z", signal_end="2024-12"),
    )
    with pytest.raises(SafetyError, match="provider_bar_after_retrieval"):
        convert(exports)


def test_private_use_declaration_and_credential_free_source_url_are_required(exports):
    change(exports / "spec.json", lambda s: s.update(usage_rights="publish_all_data"))
    with pytest.raises(SafetyError, match="unsupported_provider_provenance"):
        convert(exports)
    change(exports / "spec.json", lambda s: s.update(usage_rights="synthetic_fixture"))
    change(
        exports / "spec.json",
        lambda s: s["bindings"]["RESEARCH_EQUITY"].update(
            evidence_url="https://example.invalid/data?api_token=never-copy"
        ),
    )
    with pytest.raises(SafetyError, match="public_binding_evidence_url_required") as error:
        convert(exports)
    assert "never-copy" not in str(error.value)


def test_changed_source_bytes_cannot_overwrite_existing_artifacts(exports):
    before = (exports / "converted/dataset.json").read_bytes()
    # Same values, a different export encoding: source provenance changes.
    raw = exports / "equity.json"
    raw.write_text(json.dumps(load_json(raw), indent=2))
    with pytest.raises(SafetyError, match="immutable_artifact_exists_with_different_content"):
        convert(exports)
    assert (exports / "converted/dataset.json").read_bytes() == before


def test_provider_cli_is_offline_and_requires_no_sdk_or_cloud(exports):
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "etf_autopilot.cli",
            "history-eodhd",
            str(exports / "spec.json"),
            str(exports / "equity.json"),
            str(exports / "bond.json"),
            "--out-dir",
            str(exports / "cli-converted"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["origin"] == "synthetic_fixture"
    assert receipt["price_basis"] == "unitized_adjusted_close_proxy"


def test_rehashed_dataset_cannot_hide_its_availability_assumption(exports):
    dataset = convert(exports)
    dataset["manifest"]["provenance"]["availability"] = "verified_point_in_time"
    dataset["manifest_hash"] = digest(dataset["manifest"])
    dataset["dataset_id"] = digest({k: v for k, v in dataset.items() if k != "dataset_id"})
    with pytest.raises(SafetyError, match="unsupported_provider_provenance"):
        validate_dataset(dataset)


def test_adjusted_proxy_units_never_enter_the_forward_paper_engine(exports, config, store, events):
    from etf_autopilot.engine import run_event

    proxy_config = config_for_dataset(config, convert(exports))
    with pytest.raises(SafetyError, match="historical_contract_requires_evaluation_runner"):
        run_event(store, proxy_config, events[0])
    with pytest.raises(SafetyError, match="state_not_initialized"):
        store.read()
