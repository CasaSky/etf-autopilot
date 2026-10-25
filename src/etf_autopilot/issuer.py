"""Offline issuer growth exports and ECB reference FX -> research proxies only."""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from pathlib import Path

from .domain import SafetyError, digest, instant, load_json, number
from .history import ASSUMPTIONS, ECB_HISTORY_URL, ISSUER_POLICY, period_number, validate_manifest
from .instruments import validate_isin
from .providers import _end_of_day, _month, monthly_csv, write_import

SS = "{urn:schemas-microsoft-com:office:spreadsheet}"
FX = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _xml_file(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise SafetyError("issuer_source_size_limit")
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise SafetyError("issuer_source_size_limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SafetyError("issuer_utf8_xml_required") from None
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise SafetyError("xml_document_entities_forbidden")
    return raw, hashlib.sha256(raw).hexdigest()


def _day(value, *, growth=False):
    pattern = (
        r"([A-Za-z]{3}), ([0-9]{1,2}) ([A-Za-z]{3,4}) ([0-9]{4})"
        if growth
        else (r"([0-9]{1,2})/([A-Za-z]{3,4})/([0-9]{4})")
    )
    match = re.fullmatch(pattern, value)
    if not match:
        raise SafetyError("unsupported_issuer_date")
    parts = match.groups()
    weekday, parts = (parts[0], parts[1:]) if growth else (None, parts)
    day, month, year = parts
    try:
        result = date(int(year), MONTHS[month.replace("Sept", "Sep")], int(day))
    except (ValueError, KeyError):
        raise SafetyError("invalid_issuer_date") from None
    if growth and (weekday != WEEKDAYS[result.weekday()] or result.weekday() >= 5):
        raise SafetyError("invalid_issuer_observation_weekday")
    period_number(result.strftime("%Y-%m"))
    return result


def _cells(row):
    result = []
    for cell in row.findall(SS + "Cell"):
        if SS + "Formula" in cell.attrib:
            raise SafetyError("issuer_financial_formula_forbidden")
        try:
            column = int(cell.get(SS + "Index", str(len(result) + 1)))
        except ValueError:
            raise SafetyError("invalid_issuer_cell_index") from None
        if not len(result) < column <= 16:
            raise SafetyError("invalid_issuer_cell_index")
        result.extend([""] * (column - len(result) - 1))
        result.append(cell.findtext(SS + "Data", default=""))
    return result


def read_issuer(path, *, reviewed_share_class_launch_date=None):
    """Parse the ordered financial prefix; never parse/repair later holdings XML.

    Hash the entire original file. Some published exports have malformed unrelated
    holdings later in the file. This narrow contract ends at the completed Key
    Facts sheet and rejects malformed financial XML, dates, cells or fund values.
    """
    raw, raw_hash = _xml_file(path, 32_000_000)
    sheets = {}
    targets = {"Fund Header", "Growth of Hypothetical 10,000", "Key Facts"}
    root_seen = False
    try:
        for action, element in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
            if not root_seen:
                if element.tag != SS + "Workbook":
                    raise SafetyError("spreadsheetml_workbook_required")
                root_seen = True
            if action != "end" or element.tag != SS + "Worksheet":
                continue
            name = element.get(SS + "Name")
            if name in targets:
                if name in sheets:
                    raise SafetyError("duplicate_issuer_financial_sheet")
                rows = element.findall(SS + "Table/" + SS + "Row")
                if not 1 <= len(rows) <= 50_000:
                    raise SafetyError("issuer_financial_row_limit")
                sheets[name] = [_cells(row) for row in rows]
            if name == "Key Facts":
                break
            element.clear()
    except ET.ParseError:
        raise SafetyError("invalid_issuer_financial_xml") from None
    if set(sheets) != targets:
        raise SafetyError("issuer_financial_prefix_required")
    header = sheets["Fund Header"][0]
    growth = sheets["Growth of Hypothetical 10,000"]
    if (
        not header
        or not 1 <= len(header[0]) <= 160
        or len(growth[0]) != 3
        or growth[0][0] != ""
        or growth[0][1] != header[0]
    ):
        raise SafetyError("issuer_fund_column_binding_required")
    facts = {}
    for row in sheets["Key Facts"]:
        if not any(row):
            continue
        if len(row) < 2 or row[0] in facts:
            raise SafetyError("invalid_or_duplicate_issuer_fact")
        facts[row[0]] = row[1]
    required = {"ISIN", "Asset Class", "UCITS"}
    if not required <= set(facts) or facts["UCITS"] != "Yes":
        raise SafetyError("unsupported_issuer_fund_metadata")
    reviewed_launch = None
    if reviewed_share_class_launch_date is not None:
        try:
            if not isinstance(reviewed_share_class_launch_date, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", reviewed_share_class_launch_date
            ):
                raise ValueError
            reviewed_launch = date.fromisoformat(reviewed_share_class_launch_date)
        except ValueError:
            raise SafetyError("invalid_reviewed_class_launch_date") from None
        period_number(reviewed_launch.strftime("%Y-%m"))
    class_launch = (
        _day(facts["Share Class Launch Date"]) if "Share Class Launch Date" in facts else None
    )
    if reviewed_launch is not None and class_launch is not None and reviewed_launch != class_launch:
        raise SafetyError("reviewed_class_launch_mismatch")
    metadata = {}
    if class_launch is not None and "Share Class Currency" in facts:
        launch, currency = class_launch, facts["Share Class Currency"]
    else:
        if reviewed_launch is None:
            raise SafetyError("share_class_launch_review_required")
        nav_row = sheets["Fund Header"][3] if len(sheets["Fund Header"]) > 3 else []
        nav = re.fullmatch(r"(EUR|USD) [0-9,]+(?:\.[0-9]+)?", nav_row[0]) if nav_row else None
        if (
            nav is None
            or facts.get("Base Currency") != nav[1]
            or ("Share Class Currency" in facts and facts["Share Class Currency"] != nav[1])
        ):
            raise SafetyError("fund_level_nav_currency_binding_required")
        if "Fund Launch Date" not in facts or reviewed_launch < _day(facts["Fund Launch Date"]):
            raise SafetyError("reviewed_class_launch_precedes_fund")
        launch, currency = reviewed_launch, nav[1]
        metadata = {
            "metadata_basis": "reviewed_class_launch_and_matching_nav_base_currency",
            "reviewed_share_class_launch_date": launch.isoformat(),
        }
    if currency not in {"USD", "EUR"}:
        raise SafetyError("unsupported_issuer_fund_metadata")
    values, seen = {}, set()
    undated, seeds = 0, 0
    for row in growth[1:]:
        if not any(row):
            continue
        if len(row) != 3:
            raise SafetyError("unsupported_issuer_growth_row")
        stamp, fund, benchmark = row
        if stamp == "" and fund == "":
            undated += bool(benchmark)
            continue  # Benchmark-only rows are counted, never substituted for the fund.
        if not stamp or not fund:
            raise SafetyError("issuer_fund_observation_missing")
        day = _day(stamp, growth=True)
        if day in seen:
            raise SafetyError("duplicate_issuer_fund_observation")
        seen.add(day)
        if len(fund) > 32 or not re.fullmatch(
            r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", fund
        ):
            raise SafetyError("invalid_issuer_fund_value")
        value = number(fund.replace(",", ""))
        if not number("0.000001") <= value <= number("1000000000"):
            raise SafetyError("invalid_issuer_fund_value")
        if day < launch:
            if value != 10_000:
                raise SafetyError("unexplained_prelaunch_fund_value")
            seeds += 1
            continue
        values[day] = value
    if not values:
        raise SafetyError("issuer_fund_observations_required")
    return {
        "fund_name": header[0],
        "isin": facts["ISIN"],
        "native_currency": currency,
        "asset_class": facts["Asset Class"],
        "raw_sha256": raw_hash,
        "values": values,
        "undated_benchmark_rows": undated,
        "prelaunch_seed_rows": seeds,
        **metadata,
    }


def read_ecb(path):
    raw, raw_hash = _xml_file(path, 15_000_000)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SafetyError("invalid_ecb_xml") from None
    result = {}
    for cube in root.findall(".//" + FX + "Cube[@time]"):
        try:
            day = date.fromisoformat(cube.attrib["time"])
        except ValueError:
            raise SafetyError("invalid_ecb_date") from None
        if day in result or day.weekday() >= 5:
            raise SafetyError("duplicate_or_weekend_ecb_date")
        rates = [item.get("rate") for item in cube if item.get("currency") == "USD"]
        if len(rates) != 1 or rates[0] is None or len(rates[0]) > 32:
            raise SafetyError("one_usd_reference_rate_required")
        value = number(rates[0])
        if not number("0.01") <= value <= 100:
            raise SafetyError("invalid_ecb_usd_rate")
        result[day] = value
    if not 1 <= len(result) <= 50_000:
        raise SafetyError("ecb_reference_history_required")
    return result, raw_hash


def import_issuer(spec_path, equity_path, bond_path, ecb_path, output_directory):
    spec_path = Path(spec_path)
    if spec_path.stat().st_size > 100_000:
        raise SafetyError("provider_spec_size_limit")
    spec = load_json(spec_path)
    if (
        not isinstance(spec, dict)
        or set(spec)
        != {
            "version",
            "origin",
            "retrieved_at",
            "signal_start",
            "signal_end",
            "instruments",
            "bindings",
            "usage_rights",
        }
        or type(spec["version"]) is not int
        or spec["version"] != 1
    ):
        raise SafetyError("unsupported_issuer_import_spec")
    if not isinstance(spec["origin"], str) or spec["origin"] not in {
        "synthetic_fixture",
        "declared_historical",
    }:
        raise SafetyError("history_origin_required")
    instruments, bindings = spec["instruments"], spec["bindings"]
    if (
        not isinstance(instruments, dict)
        or len(instruments) != 2
        or not isinstance(bindings, dict)
        or set(bindings) != set(instruments)
    ):
        raise SafetyError("provider_binding_keys_mismatch")
    roles = {
        item.get("class"): asset for asset, item in instruments.items() if isinstance(item, dict)
    }
    if set(roles) != {"equity", "bond"}:
        raise SafetyError("distinct_equity_and_bond_instruments_required")
    start, end = period_number(spec["signal_start"]), period_number(spec["signal_end"])
    if not 12 <= end - start + 1 <= 1200:
        raise SafetyError("provider_requires_12_to_1200_signal_months")
    first, last = date(*_month(start), 1), date(*_month(end + 1), 7)
    fx, fx_hash = read_ecb(ecb_path)
    sources, series, calendars = {}, {}, []
    paths = {"equity": equity_path, "bond": bond_path}
    for role, asset in roles.items():
        binding = bindings[asset]
        fields = {"isin", "fund_name", "native_currency", "evidence_url"}
        if not isinstance(binding, dict) or set(binding) not in (
            fields,
            fields | {"reviewed_share_class_launch_date"},
        ):
            raise SafetyError("unsupported_issuer_binding")
        data = read_issuer(
            paths[role],
            reviewed_share_class_launch_date=binding.get("reviewed_share_class_launch_date"),
        )
        if any(binding[key] != data[key] for key in ("isin", "fund_name", "native_currency")) or (
            data["isin"] != instruments[asset].get("isin")
            or data["asset_class"] != {"equity": "Equity", "bond": "Fixed Income"}[role]
        ):
            raise SafetyError("issuer_identity_or_currency_mismatch")
        if spec["origin"] == "declared_historical":
            validate_isin(data["isin"])
        if any(_end_of_day(day) > instant(spec["retrieved_at"]) for day in data["values"]):
            raise SafetyError("provider_bar_after_retrieval")
        series[asset] = data["values"]
        calendars.append({day for day in series[asset] if first <= day <= last})
        sources[asset] = {
            key: data[key]
            for key in (
                "fund_name",
                "native_currency",
                "raw_sha256",
                "undated_benchmark_rows",
                "prelaunch_seed_rows",
            )
        }
        sources[asset].update(
            binding_sha256=digest(binding), binding_evidence_url=binding["evidence_url"]
        )
        if "metadata_basis" in data:
            sources[asset].update(
                {key: data[key] for key in ("metadata_basis", "reviewed_share_class_launch_date")}
            )
    joint = set.intersection(*calendars, set(fx))
    if not joint:
        raise SafetyError("no_joint_issuer_and_fx_observations")
    for asset, bars in series.items():
        sources[asset]["excluded_nonjoint_dates"] = len(
            {day for day in bars if first <= day <= last} - joint
        )
        converted = {
            day: bars[day] / fx[day] if sources[asset]["native_currency"] == "USD" else bars[day]
            for day in joint
        }
        base = converted[min(joint)]
        series[asset] = {day: value / base * 100 for day, value in converted.items()}
    manifest = {
        "version": 3,
        "origin": spec["origin"],
        "provider": "iShares issuer fund growth and ECB reference FX",
        "source_url": "https://www.ishares.com/uk/individual/en/products",
        "retrieved_at": spec["retrieved_at"],
        "price_basis": "unitized_issuer_return_proxy",
        "currency": "EUR",
        "instruments": deepcopy(instruments),
        "assumptions": deepcopy(ASSUMPTIONS),
        "provenance": {
            **ISSUER_POLICY,
            "usage_rights": spec["usage_rights"],
            "sources": sources,
            "fx": {
                "source_url": ECB_HISTORY_URL,
                "raw_sha256": fx_hash,
                "conversion": "divide_USD_fund_index_by_same_date_USD_per_EUR",
            },
        },
    }
    validate_manifest(manifest)
    return write_import(manifest, monthly_csv(spec, series), output_directory)
