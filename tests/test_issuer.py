import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from etf_autopilot.domain import SafetyError, digest, load_json, number
from etf_autopilot.engine import run_event
from etf_autopilot.evaluation import EvaluationRegistry, evaluation_markdown, protocol_template
from etf_autopilot.history import config_for_dataset, validate_dataset
from etf_autopilot.issuer import FX, SS, import_issuer, read_ecb, read_issuer

from .conftest import ROOT


@pytest.fixture
def exports(tmp_path):
    directory = tmp_path / "issuer"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "examples/issuer_demo.py"),
            "--output-dir",
            str(directory),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["network_calls"] == 0 and receipt["investment_evidence"] is False
    return directory


def convert(folder, output=None):
    return import_issuer(
        folder / "spec.json",
        folder / "equity.xls",
        folder / "bond.xls",
        folder / "fx.xml",
        output or folder / "converted",
    )


def xml_change(path, transform):
    root = ET.fromstring(path.read_bytes())
    transform(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def growth(root):
    return next(
        sheet
        for sheet in root.findall(SS + "Worksheet")
        if sheet.get(SS + "Name") == "Growth of Hypothetical 10,000"
    ).find(SS + "Table")


def test_offline_import_and_replay_preserve_source_currency_and_proxy_limits(
    exports, config, store, events
):
    dataset = convert(exports)
    validate_dataset(dataset)
    manifest = dataset["manifest"]
    assert manifest["version"] == 3 and manifest["price_basis"] == "unitized_issuer_return_proxy"
    assert len(dataset["rows"]) == 48
    assert number(dataset["rows"][0]["signals"]["RESEARCH_EQUITY"]) == 100
    assert dataset["rows"][0]["execution_at"] == "2022-02-02T23:59:59+00:00"
    provenance = manifest["provenance"]
    assert provenance["sources"]["RESEARCH_EQUITY"]["native_currency"] == "USD"
    assert provenance["sources"]["RESEARCH_EQUITY"]["undated_benchmark_rows"] == 1
    assert provenance["availability"] == "assumed_24h_after_utc_day_end"
    assert provenance["calendar"] == "joint_observation_dates_without_imputation"
    assert convert(exports) == dataset
    with pytest.raises(SafetyError, match="historical_contract_requires_evaluation_runner"):
        run_event(store, config_for_dataset(config, dataset), events[0])


def test_usd_fx_conversion_changes_returns_without_relabeling_native_currency(exports):
    def change(root):
        for point in root.findall(".//" + FX + "Cube[@time]"):
            if point.get("time").startswith("2022-02"):
                point[0].set("rate", "1")

    xml_change(exports / "fx.xml", change)
    dataset = convert(exports, exports / "different-fx")
    assert number(dataset["rows"][1]["signals"]["RESEARCH_EQUITY"]) == 202
    assert number(dataset["rows"][1]["signals"]["RESEARCH_BOND"]) == number("100.2")


def test_benchmark_values_can_never_replace_missing_fund_observations(exports):
    def missing(root):
        row = growth(root)[1]
        row[1].find(SS + "Data").text = ""

    xml_change(exports / "equity.xls", missing)
    with pytest.raises(SafetyError, match="issuer_fund_observation_missing"):
        convert(exports, exports / "rejected")
    assert not (exports / "rejected").exists()


def test_benchmark_change_leaves_fund_values_unchanged_but_changes_vintage_hash(exports):
    before = convert(exports)

    def change(root):
        for row in list(growth(root))[1:]:
            row[2].find(SS + "Data").text = "1.00"

    xml_change(exports / "equity.xls", change)
    after = convert(exports, exports / "benchmark-changed")
    assert before["rows"] == after["rows"] and before["dataset_id"] != after["dataset_id"]


def test_missing_joint_day_is_counted_without_imputation(exports):
    def missing(root):
        wrapper = root.find(FX + "Cube")
        wrapper.remove(next(row for row in wrapper if row.get("time") == "2022-01-10"))

    xml_change(exports / "fx.xml", missing)
    dataset = convert(exports, exports / "joint")
    assert all(
        source["excluded_nonjoint_dates"] == 1
        for source in dataset["manifest"]["provenance"]["sources"].values()
    )


def test_missing_month_end_cannot_silently_shorten_requested_coverage(exports):
    def missing(root):
        table = growth(root)
        for row in list(table)[1:]:
            text = row[0].findtext(SS + "Data")
            if text and any(f", {day} Jan 2022" in text for day in range(25, 32)):
                table.remove(row)

    xml_change(exports / "bond.xls", missing)
    with pytest.raises(SafetyError, match="incomplete_provider_signal_month"):
        convert(exports, exports / "shortened")
    assert not (exports / "shortened").exists()


@pytest.mark.parametrize(
    "key,value", [("isin", "WRONG"), ("native_currency", "EUR"), ("fund_name", "Benchmark")]
)
def test_metadata_binding_cannot_relabel_another_fund_or_currency(exports, key, value):
    spec = load_json(exports / "spec.json")
    spec["bindings"]["RESEARCH_EQUITY"][key] = value
    (exports / "spec.json").write_text(json.dumps(spec))
    with pytest.raises(SafetyError, match="issuer_identity_or_currency_mismatch"):
        convert(exports)


def test_duplicate_observation_and_changed_fund_header_fail_closed(exports):
    def duplicate(root):
        table = growth(root)
        table.append(ET.fromstring(ET.tostring(table[1])))

    xml_change(exports / "equity.xls", duplicate)
    with pytest.raises(SafetyError, match="duplicate_issuer_fund_observation"):
        convert(exports)


def test_only_completed_financial_prefix_is_parsed_without_repairing_its_xml(exports):
    path = exports / "equity.xls"
    before = read_issuer(path)
    raw = path.read_bytes()
    closing = raw.rfind(b"</")
    path.write_bytes(raw[:closing] + b"<unrelated>broken & holdings</unrelated>" + raw[closing:])
    after = read_issuer(path)
    assert after["values"] == before["values"] and after["raw_sha256"] != before["raw_sha256"]
    path.write_bytes(path.read_bytes().replace(b"Fund Header", b"broken & header", 1))
    with pytest.raises(SafetyError, match="invalid_issuer_financial_xml"):
        read_issuer(path)


def test_prelaunch_seed_is_counted_but_economic_history_is_not_invented(exports):
    def seed(root):
        row = ET.fromstring(ET.tostring(growth(root)[1]))
        row[0].find(SS + "Data").text = "Fri, 31 Dec 2021"
        row[1].find(SS + "Data").text = "10,000.00"
        growth(root).append(row)

    xml_change(exports / "bond.xls", seed)
    parsed = read_issuer(exports / "bond.xls")
    assert parsed["prelaunch_seed_rows"] == 1 and date(2021, 12, 31) not in parsed["values"]


@pytest.mark.parametrize("raw", [b'<!DOCTYPE x [<!ENTITY y "boom">]><x/>', "<x/>".encode("utf-16")])
def test_entity_and_non_utf8_documents_do_not_expand_or_trigger_fetches(tmp_path, raw):
    path = tmp_path / "unsafe.xml"
    path.write_bytes(raw)
    with pytest.raises(SafetyError):
        read_ecb(path)


@pytest.mark.parametrize("rate", ["0", "NaN", "Infinity", "-1"])
def test_invalid_fx_rate_is_not_filled_or_used(exports, rate):
    def invalid(root):
        root.find(".//" + FX + "Cube[@currency='USD']").set("rate", rate)

    xml_change(exports / "fx.xml", invalid)
    with pytest.raises(SafetyError):
        convert(exports)


def test_registered_evaluation_discloses_reference_fx_and_assumed_availability(exports, config):
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
    rendered = evaluation_markdown(result)
    assert "issuer fund-return proxies" in rendered and "ECB reference FX" in rendered
    assert "assumed 24-hour lag" in rendered and "no imputation" in rendered
    assert result["model_calls"] == 0 and result["automatic_promotion"] is False
    corrupted = load_json(exports / "converted/dataset.json")
    corrupted["manifest"]["provenance"]["availability"] = "verified"
    corrupted["manifest_hash"] = digest(corrupted["manifest"])
    corrupted["dataset_id"] = digest(
        {key: value for key, value in corrupted.items() if key != "dataset_id"}
    )
    with pytest.raises(SafetyError, match="unsupported_issuer_provenance"):
        validate_dataset(corrupted)
