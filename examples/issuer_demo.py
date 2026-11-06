"""Exercise the free-issuer import contract with fictional XML and FX, offline."""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SS = "{urn:schemas-microsoft-com:office:spreadsheet}"
FX = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def sheet(book, name, rows):
    page = ET.SubElement(book, SS + "Worksheet", {SS + "Name": name})
    table = ET.SubElement(page, SS + "Table")
    for values in rows:
        row = ET.SubElement(table, SS + "Row")
        for value in values:
            cell = ET.SubElement(row, SS + "Cell")
            ET.SubElement(cell, SS + "Data", {SS + "Type": "String"}).text = str(value)


def write_xml(path, root):
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise RuntimeError("Different fixture already exists; use a new output directory")
    else:
        path.write_bytes(raw)


def write_nav(path, binding, days):
    """Fictional accumulating NAV with one declared five-for-one split."""
    from etf_autopilot.nav import NAV_COLUMNS, REL, R, S

    book = ET.Element(S + "workbook")
    ET.SubElement(
        ET.SubElement(book, S + "sheets"), S + "sheet", {"name": "navhist", R + "id": "rId3"}
    )
    rels = ET.Element(REL + "Relationships")
    ET.SubElement(
        rels,
        REL + "Relationship",
        {"Id": "rId3", "Type": R[1:-1] + "/worksheet", "Target": "worksheets/sheet1.xml"},
    )
    worksheet = ET.Element(S + "worksheet")
    table = ET.SubElement(worksheet, S + "sheetData")
    rows = [
        ["Fund Name:", binding["fund_name"]],
        ["ISIN:", binding["isin"]],
        ["Report Date:", "08-Jan-2026"],
        ["NAV Currency:", "USD"],
        [],
        NAV_COLUMNS,
    ]
    for day in reversed(days):
        month = (day.year - 2022) * 12 + day.month - 1
        value = 100 + month
        value = str(value) if day < date(2023, 3, 6) else str(Decimal(value) / 5)
        rows.append([f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}", value, "0", "0", "0"])
    for index, values in enumerate(rows, 1):
        row = ET.SubElement(table, S + "row", {"r": str(index)})
        for column, value in zip("ABCDE", values, strict=False):
            cell = ET.SubElement(row, S + "c", {"r": f"{column}{index}", "t": "inlineStr"})
            ET.SubElement(ET.SubElement(cell, S + "is"), S + "t").text = value
    # Fixed ZIP metadata makes repeat runs byte-identical.
    from io import BytesIO
    from zipfile import ZipInfo

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, root in {
            "xl/workbook.xml": book,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/worksheets/sheet1.xml": worksheet,
        }.items():
            info = ZipInfo(name, date_time=(2026, 1, 8, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, ET.tostring(root, encoding="utf-8", xml_declaration=True))
    raw = output.getvalue()
    if path.exists() and path.read_bytes() != raw:
        raise RuntimeError("Different fixture already exists; use a new output directory")
    path.write_bytes(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/issuer-demo")
    parser.add_argument(
        "--equity-nav", action="store_true", help="Use fictional split-adjusted NAV"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from etf_autopilot.domain import load_json
    from etf_autopilot.history import dataset_receipt, write_immutable_json
    from etf_autopilot.issuer import import_issuer

    directory = args.output_dir
    directory.mkdir(parents=True, exist_ok=True)
    instruments = load_json(ROOT / "fixtures/history_contract/manifest.json")["instruments"]
    days = []
    day = date(2022, 1, 3)
    while day <= date(2026, 1, 7):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    bindings = {}
    for asset, metadata in instruments.items():
        role = metadata["class"]
        currency = "USD" if role == "equity" else "EUR"
        name = metadata["label"]
        bindings[asset] = {
            "isin": metadata["isin"],
            "fund_name": name,
            "native_currency": currency,
            "evidence_url": "https://example.invalid/fictional-issuer/" + role,
        }
        book = ET.Element(SS + "Workbook")
        sheet(book, "Fund Header", [[name]])
        growth = [["", name, "Fictional benchmark, never fund data"]]
        for day in reversed(days):
            month = (day.year - 2022) * 12 + day.month - 1
            value = 10_000 + month * (100 if role == "equity" else 20)
            stamp = f"{WEEKDAYS[day.weekday()]}, {day.day:02d} {MONTHS[day.month - 1]} {day.year}"
            growth.append([stamp, f"{value:,}.00", "999,999.00"])
        growth.append(["", "", "777,777.00"])
        sheet(book, "Growth of Hypothetical 10,000", growth)
        sheet(
            book,
            "Key Facts",
            [
                ["ISIN", metadata["isin"]],
                ["Share Class Currency", currency],
                ["Asset Class", "Equity" if role == "equity" else "Fixed Income"],
                ["UCITS", "Yes"],
                ["Share Class Launch Date", "03/Jan/2022"],
            ],
        )
        write_xml(directory / f"{role}.xls", book)
    fx = ET.Element("Envelope")
    wrapper = ET.SubElement(fx, FX + "Cube")
    for day in reversed(days):
        point = ET.SubElement(wrapper, FX + "Cube", {"time": day.isoformat()})
        ET.SubElement(point, FX + "Cube", {"currency": "USD", "rate": "2"})
    write_xml(directory / "fx.xml", fx)
    spec = {
        "version": 1,
        "origin": "synthetic_fixture",
        "retrieved_at": "2026-01-08T00:00:00Z",
        "signal_start": "2022-01",
        "signal_end": "2025-12",
        "instruments": instruments,
        "bindings": bindings,
        "usage_rights": "synthetic_fixture",
    }
    equity_path = directory / "equity.xls"
    if args.equity_nav:
        from etf_autopilot.nav import GROWTH_FORMAT, NAV_FORMAT

        spec["version"] = 2
        for item in bindings.values():
            item["source_format"] = GROWTH_FORMAT
        equity = next(asset for asset, item in instruments.items() if item["class"] == "equity")
        bindings[equity].update(
            source_format=NAV_FORMAT,
            reviewed_share_class_launch_date="2022-01-03",
            income_treatment="accumulating",
            corporate_actions=[
                {
                    "ex_date": "2023-03-06",
                    "shares_after_per_before": "5",
                    "evidence_url": "https://example.invalid/fictional-split-notice",
                }
            ],
        )
        equity_path = directory / "equity.xlsx"
        write_nav(equity_path, bindings[equity], days)
    write_immutable_json(directory / "spec.json", spec)
    dataset = import_issuer(
        directory / "spec.json",
        equity_path,
        directory / "bond.xls",
        directory / "fx.xml",
        directory / "converted",
    )
    print(
        json.dumps(
            {**dataset_receipt(dataset), "investment_evidence": False, "network_calls": 0}, indent=2
        )
    )


if __name__ == "__main__":
    main()
