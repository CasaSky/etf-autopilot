"""Exercise the free-issuer import contract with fictional XML and FX, offline."""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/issuer-demo")
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
    write_immutable_json(directory / "spec.json", spec)
    dataset = import_issuer(
        directory / "spec.json",
        directory / "equity.xls",
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
