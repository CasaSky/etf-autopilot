import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

import pytest

from etf_autopilot.capture import _requests
from etf_autopilot.domain import SafetyError, load_json
from etf_autopilot.history import validate_dataset
from etf_autopilot.issuer import SS, import_issuer, read_issuer, read_issuer_nav
from etf_autopilot.nav import GROWTH_FORMAT, ISHARES_NAV_FORMAT

from .test_issuer import exports as exports
from .test_issuer import xml_change


def add_row(table, values):
    row = ET.SubElement(table, SS + "Row")
    for value in values:
        cell = ET.SubElement(row, SS + "Cell")
        ET.SubElement(cell, SS + "Data", {SS + "Type": "String"}).text = value
    return row


def nav_table(root):
    return next(
        sheet
        for sheet in root.findall(SS + "Worksheet")
        if sheet.get(SS + "Name") == "Historical NAVs"
    ).find(SS + "Table")


@pytest.fixture
def source(exports):
    path = exports / "bond.xls"
    original = read_issuer(path)
    points = original["values"]
    report = max(points)

    def extend(root):
        header = root.find(SS + "Worksheet/" + SS + "Table")
        add_row(header, [""])
        add_row(header, ["NAV as of " + report.strftime("%d/%b/%Y")])
        add_row(header, ["EUR " + str(points[report] / 100)])
        sheet = ET.Element(SS + "Worksheet", {SS + "Name": "Historical NAVs"})
        table = ET.SubElement(sheet, SS + "Table")
        add_row(table, ["", "", "", "", "Historical NAVs"])
        for day, value in sorted(points.items(), reverse=True):
            add_row(table, [day.strftime("%d/%b/%Y"), str(value / 100), "", ""])
        add_row(table, ["The figures shown relate to past performance. Fictional fixture notice."])
        root.insert(1, sheet)
        facts = next(sheet for sheet in root if sheet.get(SS + "Name") == "Key Facts")
        add_row(facts.find(SS + "Table"), ["Use of Income", "Accumulating"])

    xml_change(path, extend)
    spec = load_json(exports / "spec.json")
    spec["version"] = 2
    for binding in spec["bindings"].values():
        binding["source_format"] = GROWTH_FORMAT
    binding = spec["bindings"]["RESEARCH_BOND"]
    binding.update(
        source_format=ISHARES_NAV_FORMAT,
        income_treatment="accumulating",
        reviewed_share_class_launch_date="2022-01-03",
        corporate_actions=[],
    )
    return path, binding, spec, original


def test_nav_identity_income_and_normalized_values_match_accumulating_growth(
    source, exports, tmp_path
):
    from etf_autopilot.history import write_immutable_json

    path, binding, spec, growth = source
    data = read_issuer_nav(path, binding)
    assert data["isin"] == growth["isin"] and data["native_currency"] == "EUR"
    assert data["values"] == {day: value / 100 for day, value in growth["values"].items()}
    assert data["report_date"] == "2026-01-07" and data["weekend_quarter_end_rows"] == 0
    plan = {
        "version": 1,
        "spec": spec,
        "downloads": {
            "RESEARCH_BOND": {"provider": "ishares", "portfolio_id": "222"},
            "RESEARCH_EQUITY": {"provider": "ishares", "portfolio_id": "111"},
        },
    }
    assert len(_requests(plan)) == 3
    write_immutable_json(tmp_path / "spec.json", spec)
    dataset = import_issuer(
        tmp_path / "spec.json",
        exports / "equity.xls",
        path,
        exports / "fx.xml",
        tmp_path / "normalized",
    )
    validate_dataset(dataset)
    provenance = dataset["manifest"]["provenance"]["sources"]["RESEARCH_BOND"]
    assert provenance["source_format"] == ISHARES_NAV_FORMAT
    assert provenance["income_treatment"] == "accumulating"


@pytest.mark.parametrize(
    "case",
    [
        "income",
        "currency",
        "report",
        "missing",
        "ambiguous",
        "formula",
        "duplicate",
        "footer",
        "weekend",
    ],
)
def test_nav_contract_rejects_ambiguous_or_incompatible_data(source, case):
    path, binding, _, _ = source

    def change(root):
        table = nav_table(root)
        if case == "income":
            facts = next(sheet for sheet in root if sheet.get(SS + "Name") == "Key Facts")
            facts.find(SS + "Table")[-1][1][0].text = "Distributing"
        elif case == "currency":
            root.find(SS + "Worksheet/" + SS + "Table")[3][0][0].text = "USD 109.6"
        elif case == "report":
            root.find(SS + "Worksheet/" + SS + "Table")[3][0][0].text = "EUR 999"
        elif case == "missing":
            table[1][1][0].text = ""
        elif case == "ambiguous":
            table[1][2][0].text = "123"
        elif case == "formula":
            table[1][1].set(SS + "Formula", "=1+1")
        elif case == "duplicate":
            table.insert(2, deepcopy(table[1]))
        elif case == "footer":
            table[-1][0][0].text = "01/Jan/2026"
        elif case == "weekend":
            table[1][0][0].text = "04/Jan/2026"

    xml_change(path, change)
    with pytest.raises(SafetyError):
        read_issuer_nav(path, binding)


def test_quarter_end_accounting_nav_is_counted_and_excluded_by_joint_calendar(
    source, exports, tmp_path
):
    from etf_autopilot.history import write_immutable_json

    path, binding, spec, _ = source

    def quarter_end(root):
        table = nav_table(root)
        row = ET.Element(SS + "Row")
        for value in ["31/Dec/2022", "102.2", "", ""]:
            ET.SubElement(ET.SubElement(row, SS + "Cell"), SS + "Data").text = value
        for index, existing in enumerate(table):
            if existing[0][0].text == "30/Dec/2022":
                table.insert(index, row)
                break

    xml_change(path, quarter_end)
    assert read_issuer_nav(path, binding)["weekend_quarter_end_rows"] == 1
    write_immutable_json(tmp_path / "spec.json", spec)
    data = import_issuer(
        tmp_path / "spec.json",
        exports / "equity.xls",
        path,
        exports / "fx.xml",
        tmp_path / "normalized",
    )
    source = data["manifest"]["provenance"]["sources"]["RESEARCH_BOND"]
    assert source["weekend_quarter_end_rows"] == 1 and source["excluded_nonjoint_dates"] == 1


def test_declared_bond_split_uses_shared_adjustment_checks(source):
    path, binding, _, _ = source

    def split(root):
        table = nav_table(root)
        for row in table[1:-1]:
            text = row[0][0].text
            stamp = datetime.strptime(text, "%d/%b/%Y").date()
            if stamp >= date(2023, 3, 6):
                row[1][0].text = str(Decimal(row[1][0].text) / 5)
        root.find(SS + "Worksheet/" + SS + "Table")[3][0][0].text = "EUR 21.92"

    xml_change(path, split)
    with pytest.raises(SafetyError, match="nav_discontinuity_requires_review"):
        read_issuer_nav(path, binding)
    binding["corporate_actions"] = [
        {
            "ex_date": "2023-03-06",
            "shares_after_per_before": "5",
            "evidence_url": "https://example.invalid/fictional-split",
        }
    ]
    data = read_issuer_nav(path, binding)
    assert data["values"][date(2023, 3, 3)] == data["values"][date(2023, 3, 6)]
