"""Narrow offline reader for accumulating-fund NAV workbooks; never broker prices."""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

from .domain import SafetyError, number

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
NAV_FORMAT = "ssga_accumulating_nav_xlsx_v1"
GROWTH_FORMAT = "ishares_growth_spreadsheetml_v1"
ISHARES_NAV_FORMAT = "ishares_accumulating_nav_spreadsheetml_v1"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
NAV_COLUMNS = ["Date", "NAV", "Shares Outstanding", "Share Class Assets", "Total Fund Assets (USD)"]


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise SafetyError("invalid_nav_date")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise SafetyError("invalid_nav_date") from None
    if not 1900 <= result.year <= 2200:
        raise SafetyError("invalid_nav_date")
    return result


def validate_splits(splits):
    if not isinstance(splits, list) or len(splits) > 10:
        raise SafetyError("unsupported_nav_split_declarations")
    previous = None
    for item in splits:
        if not isinstance(item, dict) or set(item) != {
            "ex_date",
            "shares_after_per_before",
            "evidence_url",
        }:
            raise SafetyError("unsupported_nav_split_declarations")
        day = iso_day(item["ex_date"])
        factor = item["shares_after_per_before"]
        if (
            not isinstance(factor, str)
            or len(factor) > 24
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", factor)
            or not number("0.001") <= number(factor) <= 1000
            or number(factor) == 1
            or day.weekday() >= 5
            or (previous is not None and day <= previous)
        ):
            raise SafetyError("invalid_nav_split_factor_or_order")
        if not isinstance(item["evidence_url"], str):
            raise SafetyError("public_split_evidence_url_required")
        url = urlparse(item["evidence_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or any((url.username, url.password, url.query, url.fragment))
        ):
            raise SafetyError("public_split_evidence_url_required")
        previous = day


def _xml(archive, name):
    raw = archive.read(name)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SafetyError("nav_utf8_xml_required") from None
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise SafetyError("xml_document_entities_forbidden")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise SafetyError("invalid_nav_workbook_xml") from None


def _rows(archive):
    book = _xml(archive, "xl/workbook.xml")
    sheets = book.findall(S + "sheets/" + S + "sheet")
    if book.tag != S + "workbook" or len(sheets) != 1 or sheets[0].get("name") != "navhist":
        raise SafetyError("single_navhist_sheet_required")
    links = _xml(archive, "xl/_rels/workbook.xml.rels")
    target = [item.get("Target") for item in links if item.get("Id") == sheets[0].get(R + "id")]
    if links.tag != REL + "Relationships" or target != ["worksheets/sheet1.xml"]:
        raise SafetyError("unsupported_nav_sheet_binding")
    for name in archive.namelist():
        if name.endswith(".rels") and any(
            item.get("TargetMode", "").casefold() == "external"
            and item.get("Type") != R[1:-1] + "/hyperlink"
            for item in _xml(archive, name)
        ):
            raise SafetyError("external_nav_relationship_forbidden")
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        strings = _xml(archive, "xl/sharedStrings.xml")
        shared = ["".join(t.text or "" for t in item.iter(S + "t")) for item in strings]
        if strings.tag != S + "sst" or len(shared) > 250_000:
            raise SafetyError("unsupported_nav_shared_strings")
    sheet = _xml(archive, "xl/worksheets/sheet1.xml")
    rows, previous = {}, 0
    if sheet.tag != S + "worksheet":
        raise SafetyError("unsupported_nav_sheet")
    for row in sheet.findall(S + "sheetData/" + S + "row"):
        row_id = row.get("r", "")
        if not row_id.isdigit() or not previous < int(row_id) <= 50_000:
            raise SafetyError("invalid_nav_row_order_or_limit")
        previous = int(row_id)
        values, last_column = {}, ""
        for cell in row.findall(S + "c"):
            ref = re.fullmatch(r"([A-E])([0-9]+)", cell.get("r", ""))
            if ref is None or ref[2] != row_id or ref[1] <= last_column:
                raise SafetyError("invalid_nav_cell_reference")
            if cell.find(S + "f") is not None:
                raise SafetyError("nav_formula_forbidden")
            kind = cell.get("t", "n")
            inline_count = len(cell.findall(S + "is"))
            value_count = len(cell.findall(S + "v"))
            if (inline_count, value_count) != ((1, 0) if kind == "inlineStr" else (0, 1)):
                raise SafetyError("ambiguous_nav_cell_value")
            value = cell.findtext(S + "v", default="")
            if kind == "s":
                if not value.isdigit() or int(value) >= len(shared):
                    raise SafetyError("invalid_nav_shared_string_reference")
                value = shared[int(value)]
            elif kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//" + S + "t"))
            elif kind != "n":
                raise SafetyError("unsupported_nav_cell_type")
            if len(value) > 20_000:
                raise SafetyError("nav_cell_size_limit")
            values[ref[1]] = value
            last_column = ref[1]
        rows[previous] = values
    return rows


def _day(value):
    match = re.fullmatch(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4})", value)
    if not match or match[2] not in MONTHS:
        raise SafetyError("invalid_nav_date")
    return iso_day(f"{match[3]}-{MONTHS.index(match[2]) + 1:02d}-{match[1]}")


def read_nav(path, binding):
    """Read the observed English format with explicit accumulation and split review."""
    if binding["income_treatment"] != "accumulating":
        raise SafetyError("accumulating_nav_required")
    launch = iso_day(binding["reviewed_share_class_launch_date"])
    splits = binding["corporate_actions"]
    validate_splits(splits)
    path = Path(path)
    if path.stat().st_size > 8_000_000:
        raise SafetyError("nav_source_size_limit")
    with path.open("rb") as source:
        raw = source.read(8_000_001)
    if len(raw) > 8_000_000:
        raise SafetyError("nav_source_size_limit")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()
            names = [item.filename for item in info]
            if (
                len(info) > 100
                or len(names) != len(set(names))
                or sum(item.file_size for item in info) > 32_000_000
                or any(item.flag_bits & 1 for item in info)
                or any(
                    PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts
                    or "\\" in name
                    or "vbaproject" in name.casefold()
                    or "externallinks" in name.casefold()
                    for name in names
                )
            ):
                raise SafetyError("unsafe_or_oversized_nav_workbook")
            rows = _rows(archive)
    except BadZipFile, KeyError, RuntimeError, NotImplementedError, OSError:
        raise SafetyError("invalid_nav_workbook") from None
    if (
        any(
            rows.get(i, {}).get("A") != label
            for i, label in (
                (1, "Fund Name:"),
                (2, "ISIN:"),
                (3, "Report Date:"),
                (4, "NAV Currency:"),
            )
        )
        or any(rows.get(5, {}).values())
        or [rows.get(6, {}).get(col) for col in "ABCDE"] != NAV_COLUMNS
    ):
        raise SafetyError("unsupported_nav_workbook_layout")
    report = _day(rows[3].get("B", ""))
    data_rows = [(index, row) for index, row in rows.items() if index > 6]
    # The observed workbook ends with one blank row and one marketing text cell.
    # Never skip a dated or partially populated financial row as a disclaimer.
    if (
        len(data_rows) >= 2
        and data_rows[-2][1] == {}
        and set(data_rows[-1][1]) == {"A"}
        and data_rows[-1][1]["A"].startswith("Marketing communication ")
    ):
        data_rows = data_rows[:-2]
    values, previous = {}, None
    for _, row in data_rows:
        if not row.get("A") or not row.get("B"):
            raise SafetyError("nav_observation_missing")
        day, text = _day(row["A"]), row["B"]
        if (
            day.weekday() >= 5
            or day < launch
            or day > report
            or (previous is not None and day >= previous)
        ):
            raise SafetyError("invalid_nav_observation_date_or_order")
        if len(text) > 32 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            raise SafetyError("invalid_nav_value")
        value = number(text)
        if not number("0.000001") <= value <= number("1000000000"):
            raise SafetyError("invalid_nav_value")
        values[day], previous = value, day
    if len(values) < 2:
        raise SafetyError("nav_observations_required")
    adjusted = adjust_nav_values(values, splits)
    return {
        "fund_name": rows[1].get("B", ""),
        "isin": rows[2].get("B", ""),
        "native_currency": rows[4].get("B", ""),
        "asset_class": "Equity",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "values": adjusted,
        "undated_benchmark_rows": 0,
        "prelaunch_seed_rows": 0,
        "metadata_basis": "reviewed_accumulating_nav_share_class",
        "reviewed_share_class_launch_date": launch.isoformat(),
        "income_treatment": "accumulating",
        "report_date": report.isoformat(),
        "split_adjustments": deepcopy(splits),
    }


def adjust_nav_values(values, splits):
    """Apply only declared splits, with the same boundary and discontinuity checks."""
    validate_splits(splits)
    days = sorted(values)
    factors = {iso_day(item["ex_date"]): number(item["shares_after_per_before"]) for item in splits}
    for day, factor in factors.items():
        if day not in values or day == days[0]:
            raise SafetyError("nav_split_boundary_missing")
        before = days[days.index(day) - 1]
        if (day - before).days > 7:
            raise SafetyError("nav_split_boundary_missing")
        if not number("0.5") <= values[day] * factor / values[before] <= number("1.5"):
            raise SafetyError("nav_split_factor_does_not_match_raw_boundary")
    adjusted = {day: value / _factor_after(day, factors) for day, value in values.items()}
    for before, after in zip(days, days[1:], strict=False):
        if not number("0.5") <= adjusted[after] / adjusted[before] <= number("1.5"):
            raise SafetyError("nav_discontinuity_requires_review")
    return adjusted


def _factor_after(day, factors):
    result = number("1")
    for ex_date, factor in factors.items():
        if day < ex_date:
            result *= factor
    return result
