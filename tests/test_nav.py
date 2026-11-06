import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from etf_autopilot.domain import SafetyError, load_json
from etf_autopilot.engine import run_event
from etf_autopilot.evaluation import evaluate_window, evaluation_markdown, protocol_template
from etf_autopilot.history import config_for_dataset, validate_dataset, validate_manifest
from etf_autopilot.issuer import import_issuer, read_issuer
from etf_autopilot.nav import GROWTH_FORMAT, NAV_COLUMNS, NAV_FORMAT, REL, R, S, read_nav

from .test_issuer import exports as exports


def binding():
    return {
        "income_treatment": "accumulating",
        "reviewed_share_class_launch_date": "2022-01-03",
        "corporate_actions": [
            {
                "ex_date": "2023-03-06",
                "shares_after_per_before": "5",
                "evidence_url": "https://example.invalid/fictional-split-notice",
            }
        ],
    }


def workbook(
    path,
    points=None,
    *,
    name="Fictional equity NAV",
    isin="SIMULATED-EQUITY",
    report="2023-03-07",
    shared=False,
    footer=True,
):
    points = points or {date(2023, 3, 3): Decimal("100"), date(2023, 3, 6): Decimal("20.4")}
    strings = []
    book = ET.Element(S + "workbook")
    sheets = ET.SubElement(book, S + "sheets")
    ET.SubElement(sheets, S + "sheet", {"name": "navhist", R + "id": "rId3"})
    rels = ET.Element(REL + "Relationships")
    ET.SubElement(
        rels,
        REL + "Relationship",
        {"Id": "rId3", "Type": R[1:-1] + "/worksheet", "Target": "worksheets/sheet1.xml"},
    )
    root = ET.Element(S + "worksheet")
    data = ET.SubElement(root, S + "sheetData")

    def stamp(day):
        months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
        return f"{day.day:02d}-{months[day.month - 1]}-{day.year}"

    rows = [
        ["Fund Name:", name],
        ["ISIN:", isin],
        ["Report Date:", stamp(date.fromisoformat(report))],
        ["NAV Currency:", "USD"],
        [],
        NAV_COLUMNS,
        *[
            [stamp(day), str(value), "0", "0", "0"]
            for day, value in sorted(points.items(), reverse=True)
        ],
    ]
    if footer:
        rows += [[], ["Marketing communication \nFictional fixture notice, not vendor content."]]
    for index, values in enumerate(rows, 1):
        row = ET.SubElement(data, S + "row", {"r": str(index)})
        for col, value in zip("ABCDE", values, strict=False):
            cell = ET.SubElement(row, S + "c", {"r": f"{col}{index}"})
            if shared:
                cell.set("t", "s")
                strings.append(value)
                ET.SubElement(cell, S + "v").text = str(len(strings) - 1)
            else:
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, S + "is"), S + "t").text = value
    parts = {
        "xl/workbook.xml": ET.tostring(book),
        "xl/_rels/workbook.xml.rels": ET.tostring(rels),
        "xl/worksheets/sheet1.xml": ET.tostring(root),
    }
    if shared:
        table = ET.Element(S + "sst")
        for text in strings:
            ET.SubElement(ET.SubElement(table, S + "si"), S + "t").text = text
        parts["xl/sharedStrings.xml"] = ET.tostring(table)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for key, value in parts.items():
            archive.writestr(key, value)


def rewrite(path, transform):
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    transform(parts)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, raw in parts.items():
            archive.writestr(name, raw)


@pytest.mark.parametrize("shared", [False, True])
def test_explicit_split_preserves_growth_and_hashes_original_workbook(tmp_path, shared):
    path = tmp_path / "nav.xlsx"
    workbook(path, shared=shared)
    original = path.read_bytes()
    review = binding()
    result = read_nav(path, review)
    values = result["values"]
    assert values[date(2023, 3, 3)] == Decimal("20")
    assert values[date(2023, 3, 6)] / values[date(2023, 3, 3)] == Decimal("1.02")
    assert result["split_adjustments"] == review["corporate_actions"]
    assert result["metadata_basis"] == "reviewed_accumulating_nav_share_class"
    assert path.read_bytes() == original


def test_undeclared_split_fails_instead_of_becoming_a_fake_loss(tmp_path):
    path = tmp_path / "nav.xlsx"
    workbook(path)
    review = binding()
    review["corporate_actions"] = []
    with pytest.raises(SafetyError, match="nav_discontinuity_requires_review"):
        read_nav(path, review)


@pytest.mark.parametrize(
    "factor,points",
    [("25", None), ("5", {date(2023, 3, 3): Decimal("20"), date(2023, 3, 6): Decimal("20.4")})],
)
def test_wrong_factor_or_already_adjusted_source_cannot_be_adjusted_again(tmp_path, factor, points):
    path = tmp_path / "nav.xlsx"
    workbook(path, points)
    review = binding()
    review["corporate_actions"][0]["shares_after_per_before"] = factor
    with pytest.raises(SafetyError, match="nav_split_factor_does_not_match_raw_boundary"):
        read_nav(path, review)


def test_multiple_splits_compound_only_before_each_ex_date(tmp_path):
    path = tmp_path / "nav.xlsx"
    workbook(
        path,
        {
            date(2023, 3, 3): Decimal("100"),
            date(2023, 3, 6): Decimal("20"),
            date(2023, 3, 7): Decimal("10"),
        },
    )
    review = binding()
    review["corporate_actions"].append(
        {
            "ex_date": "2023-03-07",
            "shares_after_per_before": "2",
            "evidence_url": "https://example.invalid/fictional-second-split",
        }
    )
    assert set(read_nav(path, review)["values"].values()) == {Decimal("10")}


@pytest.mark.parametrize(
    "case,code",
    [
        ("distribution", "accumulating_nav_required"),
        ("factor", "invalid_nav_split_factor_or_order"),
        ("duplicate", "invalid_nav_split_factor_or_order"),
        ("private_url", "public_split_evidence_url_required"),
        ("no_boundary", "nav_split_boundary_missing"),
        ("wrong_launch", "invalid_nav_observation_date_or_order"),
    ],
)
def test_invalid_operator_review_cannot_produce_nav_series(tmp_path, case, code):
    path = tmp_path / "nav.xlsx"
    workbook(path)
    review = binding()
    if case == "distribution":
        review["income_treatment"] = "distributing"
    if case == "factor":
        review["corporate_actions"][0]["shares_after_per_before"] = "0"
    if case == "duplicate":
        review["corporate_actions"] *= 2
    if case == "private_url":
        review["corporate_actions"][0]["evidence_url"] += "?secret=not-a-real-key"
    if case == "no_boundary":
        review["corporate_actions"][0]["ex_date"] = "2023-03-07"
    if case == "wrong_launch":
        review["reviewed_share_class_launch_date"] = "2024-01-01"
    with pytest.raises(SafetyError, match=code):
        read_nav(path, review)


@pytest.mark.parametrize(
    "case,code",
    [
        ("formula", "nav_formula_forbidden"),
        ("ambiguous", "ambiguous_nav_cell_value"),
        ("missing", "nav_observation_missing"),
        ("duplicate_date", "invalid_nav_observation_date_or_order"),
        ("nan", "invalid_nav_value"),
        ("entities", "xml_document_entities_forbidden"),
        ("external", "external_nav_relationship_forbidden"),
        ("path", "unsafe_or_oversized_nav_workbook"),
        ("expanded", "unsafe_or_oversized_nav_workbook"),
        ("sheet", "single_navhist_sheet_required"),
    ],
)
def test_unsafe_or_ambiguous_workbooks_fail_before_import(tmp_path, case, code):
    path = tmp_path / "nav.xlsx"
    workbook(path)

    def change(parts):
        if case == "entities":
            parts["xl/workbook.xml"] = b'<!DOCTYPE workbook [<!ENTITY x "no">]><workbook/>'
            return
        if case == "path":
            parts["../outside.xml"] = b"no"
            return
        if case == "expanded":
            parts["large.txt"] = b"0" * 32_000_001
            return
        if case == "external":
            root = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
            ET.SubElement(
                root,
                REL + "Relationship",
                {
                    "Id": "bad",
                    "TargetMode": "External",
                    "Target": "https://example.invalid/data",
                    "Type": R[1:-1] + "/externalLink",
                },
            )
            parts["xl/_rels/workbook.xml.rels"] = ET.tostring(root)
            return
        if case == "sheet":
            root = ET.fromstring(parts["xl/workbook.xml"])
            root.find(S + "sheets")[0].set("name", "other")
            parts["xl/workbook.xml"] = ET.tostring(root)
            return
        root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        row = root.find(S + "sheetData")[6]
        if case == "formula":
            ET.SubElement(row[1], S + "f").text = "1+1"
        if case == "ambiguous":
            ET.SubElement(row[1], S + "v").text = "999"
        if case == "missing":
            row.remove(row[1])
        if case == "duplicate_date":
            root.find(S + "sheetData")[7][0].find(".//" + S + "t").text = "06-Mar-2023"
        if case == "nan":
            row[1].find(".//" + S + "t").text = "NaN"
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)

    rewrite(path, change)
    with pytest.raises(SafetyError, match=code):
        read_nav(path, binding())


def mixed_spec(exports):
    import json

    spec = load_json(exports / "spec.json")
    spec["version"] = 2
    for item in spec["bindings"].values():
        item["source_format"] = GROWTH_FORMAT
    item = spec["bindings"]["RESEARCH_EQUITY"]
    item.update(binding(), source_format=NAV_FORMAT)
    values = read_issuer(exports / "equity.xls")["values"]
    points = {
        day: value / 100 / (5 if day >= date(2023, 3, 6) else 1) for day, value in values.items()
    }
    workbook(
        exports / "equity.xlsx",
        points,
        name=item["fund_name"],
        isin=item["isin"],
        report="2026-01-08",
    )
    (exports / "mixed-spec.json").write_text(json.dumps(spec))
    return spec


def test_mixed_nav_growth_import_retains_returns_provenance_and_research_boundary(
    exports, config, store, events
):
    original = import_issuer(
        exports / "spec.json",
        exports / "equity.xls",
        exports / "bond.xls",
        exports / "fx.xml",
        exports / "old",
    )
    mixed_spec(exports)
    dataset = import_issuer(
        exports / "mixed-spec.json",
        exports / "equity.xlsx",
        exports / "bond.xls",
        exports / "fx.xml",
        exports / "mixed",
    )
    validate_dataset(dataset)
    assert dataset["rows"] == original["rows"]
    assert original["manifest"]["version"] == 3 and dataset["manifest"]["version"] == 4
    source = dataset["manifest"]["provenance"]["sources"]["RESEARCH_EQUITY"]
    assert source["source_format"] == NAV_FORMAT
    assert source["split_adjustments"] == binding()["corporate_actions"]
    with pytest.raises(SafetyError, match="historical_contract_requires_evaluation_runner"):
        run_event(store, config_for_dataset(config, dataset), events[0])
    windows = {
        "development": {"start": "2022-12", "end": "2023-11"},
        "validation": {"start": "2023-12", "end": "2024-11"},
        "holdout": {"start": "2024-12", "end": "2026-01"},
    }
    protocol = protocol_template(dataset, config, windows)
    result = evaluate_window(dataset, config, protocol, "development")
    assert result["model_calls"] == 0 and result["automatic_promotion"] is False
    assert all(a["summary"]["reconciliation"] == "matched" for a in result["accounts"].values())
    assert "reference FX" in evaluation_markdown(result)
    tampered = deepcopy(dataset["manifest"])
    tampered["provenance"]["sources"]["RESEARCH_EQUITY"]["income_treatment"] = "distributing"
    with pytest.raises(SafetyError, match="accumulating_equity_nav_required"):
        validate_manifest(tampered)


def test_mixed_import_checks_actual_identity_before_writing(exports):
    mixed_spec(exports)
    workbook(exports / "wrong.xlsx", name="Wrong synthetic share class")
    with pytest.raises(SafetyError, match="issuer_identity_or_currency_mismatch"):
        import_issuer(
            exports / "mixed-spec.json",
            exports / "wrong.xlsx",
            exports / "bond.xls",
            exports / "fx.xml",
            exports / "rejected",
        )
    assert not (exports / "rejected").exists()
