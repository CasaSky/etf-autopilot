"""Rejections locate the offending record without repeating its values."""

import csv
import io
import json
from copy import deepcopy

import pytest

from etf_autopilot import cli
from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.history import COLUMNS, import_history, validate_dataset

from .conftest import ROOT

FIXTURE = ROOT / "fixtures/history_contract"


def inputs():
    manifest = load_json(FIXTURE / "manifest.json")
    rows = list(csv.DictReader(io.StringIO((FIXTURE / "monthly.csv").read_text())))
    return manifest, rows


def raw_lines():
    # Keep CRLF intact: text mode would translate it before the split.
    return (FIXTURE / "monthly.csv").read_bytes().decode("utf-8").split("\r\n")


def rejection(tmp_path, manifest, rows, *, text=None):
    manifest_path, csv_path = tmp_path / "manifest.json", tmp_path / "history.csv"
    manifest_path.write_text(json.dumps(manifest))
    if text is None:
        with csv_path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text(text, newline="")
    with pytest.raises(SafetyError) as caught:
        import_history(manifest_path, csv_path)
    return caught.value


def test_a_rejected_index_names_its_line_column_and_period_without_the_value(tmp_path):
    manifest, rows = inputs()
    leaked = "0.0000000001"
    rows[2]["execution_index"] = leaked
    exc = rejection(tmp_path, manifest, rows)
    assert exc.code == "invalid_total_return_index"
    assert exc.line == 4
    assert exc.column == "execution_index"
    assert exc.period == "2022-02"
    assert leaked not in str(exc)
    assert "2022-02" in str(exc)


def test_a_non_numeric_cell_is_located_by_column(tmp_path):
    manifest, rows = inputs()
    rows[0]["signal_index"] = "not-a-number"
    exc = rejection(tmp_path, manifest, rows)
    assert (exc.code, exc.line, exc.column) == ("invalid_decimal", 2, "signal_index")
    assert "not-a-number" not in str(exc)


def test_a_naive_timestamp_names_the_timestamp_column_it_rejected(tmp_path):
    manifest, rows = inputs()
    rows[1]["available_at"] = "2022-01-31T16:30:00"
    exc = rejection(tmp_path, manifest, rows)
    assert (exc.code, exc.line, exc.column, exc.period) == (
        "timezone_required",
        3,
        "available_at",
        "2022-01",
    )


def test_a_duplicate_record_names_the_repeated_asset_and_period(tmp_path):
    manifest, rows = inputs()
    rows.append(deepcopy(rows[0]))
    exc = rejection(tmp_path, manifest, rows)
    assert exc.code == "duplicate_history_observation"
    assert (exc.line, exc.column, exc.period) == (len(rows) + 1, "asset_id", "2022-01")


def test_an_undeclared_asset_names_its_line_and_period(tmp_path):
    manifest, rows = inputs()
    rows[0]["asset_id"] = "UNDECLARED_ASSET"
    exc = rejection(tmp_path, manifest, rows)
    assert (exc.code, exc.line, exc.column, exc.period) == (
        "history_asset_not_in_manifest",
        2,
        "asset_id",
        "2022-01",
    )


def test_a_misaligned_asset_observation_names_the_timestamp_column(tmp_path):
    manifest, rows = inputs()
    # The first asset defines the period; the second asset must repeat its timestamps.
    rows[0]["available_at"] = "2022-01-31T16:45:00Z"
    exc = rejection(tmp_path, manifest, rows)
    assert (exc.code, exc.line, exc.column, exc.period) == (
        "unaligned_history_observations",
        3,
        "available_at",
        "2022-01",
    )


def test_a_missing_month_names_the_first_absent_period(tmp_path):
    manifest, rows = inputs()
    rows[:] = [row for row in rows if row["period"] != "2023-05"]
    exc = rejection(tmp_path, manifest, rows)
    assert exc.code == "history_month_gap_no_imputation"
    assert exc.period == "2023-05"
    assert exc.line is None


def test_a_missing_asset_observation_names_the_period_it_belongs_to(tmp_path):
    manifest, rows = inputs()
    rows.pop(0)
    exc = rejection(tmp_path, manifest, rows)
    assert (exc.code, exc.column, exc.period) == (
        "missing_history_asset_observation",
        "asset_id",
        "2022-01",
    )


def test_a_wrong_header_names_the_first_column_that_differs(tmp_path):
    manifest, _ = inputs()
    text = "period,asset,signal_at,available_at,execution_at,signal_index,execution_index\n"
    exc = rejection(tmp_path, manifest, [], text=text)
    assert (exc.code, exc.line, exc.column) == ("history_csv_columns_mismatch", 1, "asset_id")


def test_an_extra_header_column_is_not_reported_as_a_missing_one(tmp_path):
    manifest, rows = inputs()
    text = ",".join([*COLUMNS, "memo"]) + "\r\n"
    exc = rejection(tmp_path, manifest, [], text=text)
    assert (exc.code, exc.line, exc.column) == ("history_csv_columns_mismatch", 1, "extra_column")


def test_a_row_with_too_many_fields_is_located(tmp_path):
    manifest, _ = inputs()
    lines = raw_lines()
    lines[1] = lines[1] + ",unexpected"
    exc = rejection(tmp_path, manifest, [], text="\r\n".join(lines))
    assert (exc.code, exc.line) == ("malformed_history_row", 2)


def test_a_malformed_quote_is_located_at_the_line_that_breaks_the_reader(tmp_path):
    manifest, _ = inputs()
    lines = raw_lines()
    lines[2] = lines[2].replace("RESEARCH_BOND", '"RESEARCH_BOND"X')
    exc = rejection(tmp_path, manifest, [], text="\r\n".join(lines))
    assert (exc.code, exc.line) == ("malformed_history_csv", 3)


def test_reported_lines_are_physical_lines_not_record_numbers(tmp_path):
    manifest, _ = inputs()
    lines = raw_lines()
    lines[3] = lines[3].replace(",103.581765", ",0")
    lines.insert(3, "")  # one blank line the CSV reader skips between records
    exc = rejection(tmp_path, manifest, [], text="\r\n".join(lines))
    assert exc.code == "invalid_total_return_index"
    assert exc.line == 5


def test_revalidation_of_a_tampered_dataset_keeps_the_location(tmp_path):
    manifest, _ = inputs()
    dataset = import_history(FIXTURE / "manifest.json", FIXTURE / "monthly.csv")
    tampered = deepcopy(dataset)
    tampered["rows"] = [row for row in tampered["rows"] if row["period"] != "2023-05"]
    tampered["manifest_hash"] = digest(tampered["manifest"])
    tampered["dataset_id"] = digest(
        {key: value for key, value in tampered.items() if key != "dataset_id"}
    )
    with pytest.raises(SafetyError) as caught:
        validate_dataset(tampered)
    assert caught.value.code == "history_month_gap_no_imputation"
    assert caught.value.period == "2023-05"


def test_a_code_only_rejection_keeps_its_message_and_reports_no_location():
    exc = SafetyError("state_not_initialized")
    assert str(exc) == "state_not_initialized"
    assert exc.location() == {}
    assert exc.code == "state_not_initialized"


def test_a_located_rejection_starts_with_its_stable_code():
    exc = SafetyError("invalid_total_return_index", line=4, column="signal_index", period="2022-02")
    assert str(exc) == (
        "invalid_total_return_index at line 4 (column signal_index) (period 2022-02)"
    )
    assert str(exc).split()[0] == exc.code
    assert exc.location() == {"line": 4, "column": "signal_index", "period": "2022-02"}


def test_the_cli_blocked_receipt_carries_the_location_and_no_values(tmp_path, capsys):
    manifest, rows = inputs()
    rows[2]["execution_index"] = "0.0000000001"
    manifest_path, csv_path = tmp_path / "manifest.json", tmp_path / "history.csv"
    manifest_path.write_text(json.dumps(manifest))
    with csv_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    code = cli.main(
        ["history-import", str(manifest_path), str(csv_path), "--out", str(tmp_path / "out.json")]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert code == 2
    assert receipt["code"] == "invalid_total_return_index"
    assert receipt["location"] == {"line": 4, "column": "execution_index", "period": "2022-02"}
    assert "0.0000000001" not in receipt["detail"]
    assert not (tmp_path / "out.json").exists()


def test_the_untouched_fixture_still_imports_without_a_location(tmp_path):
    dataset = import_history(FIXTURE / "manifest.json", FIXTURE / "monthly.csv")
    validate_dataset(dataset)
    assert dataset["rows"][0]["period"] == "2022-01"
    assert dataset["rows"][0]["signals"]["RESEARCH_EQUITY"] == "102.000000"
