import csv
import io
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest

from etf_autopilot.domain import SafetyError, load_json, number
from etf_autopilot.history import _rows_from_csv, validate_manifest
from etf_autopilot.issuer import import_issuer
from etf_autopilot.nav import GROWTH_FORMAT
from etf_autopilot.providers import monthly_csv

from .test_issuer import exports as exports


@pytest.fixture
def inputs():
    spec = {
        "signal_start": "2022-01",
        "signal_end": "2022-12",
        "retrieved_at": "2023-01-09T00:00:00Z",
    }
    bars = {}
    day = date(2022, 1, 3)
    while day <= date(2023, 1, 7):
        if day.weekday() < 5:
            bars[day] = number(str(100 + (day - date(2022, 1, 3)).days))
        day += timedelta(days=1)
    series = {"EQUITY": bars, "BOND": deepcopy(bars)}
    original = list(csv.DictReader(io.StringIO(monthly_csv(spec, series).decode())))
    dates = {row["period"]: row["execution_at"][:10] for row in original}
    dates["2022-12"] = "2023-01-04"
    return spec, series, dates


def test_exact_comparison_date_uses_its_observation_and_leaves_signals_unchanged(inputs):
    spec, series, dates = inputs
    original = list(csv.DictReader(io.StringIO(monthly_csv(spec, series).decode())))
    aligned = list(
        csv.DictReader(io.StringIO(monthly_csv(spec, series, execution_dates=dates).decode()))
    )
    for before, after in zip(original, aligned, strict=True):
        assert before["signal_at"] == after["signal_at"]
        assert before["signal_index"] == after["signal_index"]
        assert after["execution_at"][:10] == dates[after["period"]]
    assert aligned[-1]["execution_index"] == str(series["BOND"][date(2023, 1, 4)])


@pytest.mark.parametrize(
    "case,code",
    [
        ("missing", "declared_execution_observation_missing_or_unavailable"),
        ("unavailable", "declared_execution_observation_missing_or_unavailable"),
        ("wrong_month", "invalid_execution_calendar_date"),
        ("weekend", "invalid_execution_calendar_date"),
        ("late", "invalid_execution_calendar_date"),
        ("coverage", "invalid_execution_calendar"),
    ],
)
def test_bad_calendar_never_substitutes_a_nearby_price(inputs, case, code):
    spec, series, dates = inputs
    if case == "missing":
        for bars in series.values():
            del bars[date(2023, 1, 4)]
    if case == "unavailable":
        dates["2022-02"] = "2022-03-01"
    if case == "wrong_month":
        dates["2022-12"] = "2022-12-05"
    if case == "weekend":
        dates["2022-12"] = "2023-01-07"
    if case == "late":
        dates["2022-12"] = "2023-01-09"
    if case == "coverage":
        del dates["2022-12"]
    with pytest.raises(SafetyError, match=code):
        monthly_csv(spec, series, execution_dates=dates)


def test_calendar_provenance_is_bound_to_csv_and_normalized_validation(exports):
    spec = load_json(exports / "spec.json")
    original = import_issuer(
        exports / "spec.json",
        exports / "equity.xls",
        exports / "bond.xls",
        exports / "fx.xml",
        exports / "original",
    )
    spec["version"] = 2
    for item in spec["bindings"].values():
        item["source_format"] = GROWTH_FORMAT
    spec["execution_dates"] = {row["period"]: row["execution_at"][:10] for row in original["rows"]}
    spec["execution_dates"]["2022-12"] = "2023-01-04"
    path = exports / "calendar-spec.json"
    path.write_text(json.dumps(spec))
    output = exports / "aligned"
    dataset = import_issuer(
        path, exports / "equity.xls", exports / "bond.xls", exports / "fx.xml", output
    )
    manifest = dataset["manifest"]
    assert manifest["provenance"]["execution_dates"] == spec["execution_dates"]
    row = next(row for row in dataset["rows"] if row["period"] == "2022-12")
    assert row["execution_at"] == "2023-01-04T23:59:59+00:00"
    wrong = deepcopy(manifest)
    wrong["provenance"]["execution_dates"]["2022-12"] = "2023-01-05"
    validate_manifest(wrong)
    with pytest.raises(SafetyError, match="history_execution_calendar_mismatch"):
        _rows_from_csv((output / "monthly.csv").read_bytes(), wrong)
