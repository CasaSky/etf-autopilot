"""Local EODHD daily exports -> immutable research inputs; no network or secrets.

Provider symbol/currency/ISIN bindings are explicit operator declarations. Vendor
adjusted closes are retrospective proxies, not a verified point-in-time index.
"""

import calendar
import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .domain import SafetyError, digest, instant, load_json, number
from .history import (
    ADJUSTED_ASSUMPTIONS,
    COLUMNS,
    EODHD_POLICY,
    MAX_IMPORT_BYTES,
    _rows_from_csv,
    import_history,
    period_number,
    validate_manifest,
    write_immutable_json,
)
from .instruments import validate_isin

EODHD_SOURCE = "https://eodhd.com/financial-apis/api-for-historical-data-and-volumes"


def _read_daily(path):
    path = Path(path)
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise SafetyError("provider_import_size_limit")
    raw = path.read_bytes()
    try:
        rows = json.loads(raw, parse_float=str, parse_int=str)
    except (ValueError, UnicodeError):
        raise SafetyError("invalid_eodhd_json") from None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50_000:
        raise SafetyError("daily_provider_array_required")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
        }:
            raise SafetyError("unsupported_eodhd_daily_bar")
        try:
            if not isinstance(row["date"], str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row["date"]
            ):
                raise ValueError
            day = date.fromisoformat(row["date"])
        except (ValueError, TypeError):
            raise SafetyError("invalid_provider_bar_date") from None
        period_number(day.strftime("%Y-%m"))
        if day.weekday() >= 5:
            raise SafetyError("unexpected_weekend_etf_bar")
        if day in result:
            raise SafetyError("duplicate_provider_bar")
        values = {
            name: number(row[name])
            for name in ("open", "high", "low", "close", "adjusted_close", "volume")
        }
        if (
            any(
                not number("0.000001") <= values[key] <= number("1000000000")
                for key in ("open", "high", "low", "close", "adjusted_close")
            )
            or not values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
            or not 0 <= values["volume"] <= number("1000000000000000")
            or values["volume"] != values["volume"].to_integral_value()
        ):
            raise SafetyError("invalid_provider_bar_values")
        result[day] = values["adjusted_close"]
    return result, hashlib.sha256(raw).hexdigest()


def _month(number_value):
    year, offset = divmod(number_value - 1, 12)
    return year, offset + 1


def _end_of_day(day):
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def _prepare(spec, paths):
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
        raise SafetyError("unsupported_eodhd_import_spec")
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
    series, sources, exchanges = {}, {}, set()
    for role, asset in roles.items():
        binding = bindings[asset]
        if not isinstance(binding, dict) or set(binding) != {
            "symbol",
            "isin",
            "currency",
            "evidence_url",
        }:
            raise SafetyError("unsupported_provider_binding")
        if (
            binding["isin"] != instruments[asset].get("isin")
            or binding["currency"] != "EUR"
            or instruments[asset].get("currency") != "EUR"
        ):
            raise SafetyError("provider_identity_or_currency_mismatch")
        if spec["origin"] == "declared_historical":
            validate_isin(binding["isin"])
        if not isinstance(binding["symbol"], str) or not re.fullmatch(
            r"[A-Z0-9_-]{1,32}\.[A-Z]{2,12}", binding["symbol"]
        ):
            raise SafetyError("invalid_provider_symbol")
        exchange = binding["symbol"].rsplit(".", 1)[1]
        if exchange in {"CC", "FOREX", "INDX", "MONEY", "GBOND"}:
            raise SafetyError("provider_etf_listing_required")
        exchanges.add(exchange)
        series[asset], raw_hash = _read_daily(paths[role])
        sources[asset] = {
            "symbol": binding["symbol"],
            "raw_sha256": raw_hash,
            "binding_sha256": digest(binding),
            "binding_evidence_url": binding["evidence_url"],
        }
    if len(exchanges) != 1 or len({source["symbol"] for source in sources.values()}) != 2:
        raise SafetyError("distinct_etfs_on_same_venue_required")
    manifest = {
        "version": 2,
        "origin": spec["origin"],
        "provider": "EODHD operator-supplied daily exports",
        "source_url": EODHD_SOURCE,
        "retrieved_at": spec["retrieved_at"],
        "price_basis": "unitized_adjusted_close_proxy",
        "currency": "EUR",
        "instruments": deepcopy(instruments),
        "assumptions": deepcopy(ADJUSTED_ASSUMPTIONS),
        "provenance": {**EODHD_POLICY, "usage_rights": spec["usage_rights"], "sources": sources},
    }
    validate_manifest(manifest)
    raw_csv = monthly_csv(spec, series)
    _rows_from_csv(raw_csv, manifest)
    return manifest, raw_csv


def monthly_csv(spec, series):
    """Convert aligned dated research observations; never fill missing bars."""
    start, end = period_number(spec["signal_start"]), period_number(spec["signal_end"])
    if not 12 <= end - start + 1 <= 1200:
        raise SafetyError("provider_requires_12_to_1200_signal_months")
    first = date(*_month(start), 1)
    last = date(*_month(end + 1), 7)
    calendars = [{day for day in bars if first <= day <= last} for bars in series.values()]
    if not calendars[0] or calendars[0] != calendars[1]:
        raise SafetyError("provider_calendars_differ_or_empty")
    dates = sorted(calendars[0])
    if any((later - earlier).days > 10 for earlier, later in zip(dates, dates[1:], strict=False)):
        raise SafetyError("provider_bar_gap_requires_review")
    if any(
        _end_of_day(day) > instant(spec["retrieved_at"]) for bars in series.values() for day in bars
    ):
        raise SafetyError("provider_bar_after_retrieval")
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for month in range(start, end + 1):
        year, month_number = _month(month)
        monthly = [day for day in dates if (day.year, day.month) == (year, month_number)]
        month_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
        if len(monthly) < 10 or (month_end - monthly[-1]).days > 4:
            raise SafetyError("incomplete_provider_signal_month")
        signal_day = monthly[-1]
        signal = _end_of_day(signal_day)
        available = signal + timedelta(days=1)
        execution = next(
            (
                day
                for day in dates
                if (day.year, day.month) == _month(month + 1) and _end_of_day(day) > available
            ),
            None,
        )
        if execution is None:
            raise SafetyError("missing_later_provider_execution_bar")
        for asset, bars in series.items():
            writer.writerow(
                {
                    "period": f"{year:04d}-{month_number:02d}",
                    "asset_id": asset,
                    "signal_at": signal.isoformat(),
                    "available_at": available.isoformat(),
                    "execution_at": _end_of_day(execution).isoformat(),
                    "signal_index": str(bars[signal_day]),
                    "execution_index": str(bars[execution]),
                }
            )
    raw_csv = text.getvalue().encode()
    return raw_csv


def import_eodhd(spec_path, equity_path, bond_path, output_directory):
    spec_path = Path(spec_path)
    if spec_path.stat().st_size > 100_000:
        raise SafetyError("provider_spec_size_limit")
    manifest, raw_csv = _prepare(load_json(spec_path), {"equity": equity_path, "bond": bond_path})
    return write_import(manifest, raw_csv, output_directory)


def write_import(manifest, raw_csv, output_directory):
    """Write already validated provider conversion artifacts immutably."""
    validate_manifest(manifest)
    _rows_from_csv(raw_csv, manifest)
    directory = Path(output_directory)
    # Validate the complete conversion before creating any output artifact.
    write_immutable_json(directory / "manifest.json", manifest)
    csv_path = directory / "monthly.csv"
    try:
        with csv_path.open("xb") as output:
            output.write(raw_csv)
    except FileExistsError:
        if csv_path.read_bytes() != raw_csv:
            raise SafetyError("immutable_artifact_exists_with_different_content") from None
    dataset = import_history(directory / "manifest.json", csv_path)
    write_immutable_json(directory / "dataset.json", dataset)
    return dataset
