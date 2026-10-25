"""Immutable monthly total-return research imports, separate from broker quotes.

The importer records the provider's declarations. It does not verify a vendor,
UCITS status, account eligibility, or that an adjusted index is an executable price.
"""

import csv
import hashlib
import io
import re
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from .domain import SafetyError, canonical, digest, instant, load_json, number

COLUMNS = (
    "period",
    "asset_id",
    "signal_at",
    "available_at",
    "execution_at",
    "signal_index",
    "execution_index",
)
ASSUMPTIONS = {
    "distributions": "reinvested",
    "fund_expenses": "embedded_in_index",
    "investor_tax": "excluded",
    "fx": "already_in_eur",
    "cash_return": "zero",
}
ADJUSTED_ASSUMPTIONS = {**ASSUMPTIONS, "distributions": "vendor_dividend_adjustment_proxy"}
EODHD_POLICY = {
    "adapter": "eodhd_daily_v1",
    "availability": "assumed_24h_after_utc_day_end",
    "execution": "later_daily_close_in_following_month",
    "adjustments": "retrospective_split_and_dividend_proxy",
}
MAX_IMPORT_BYTES = 5_000_000
MAX_PERIODS = 1200


def period_number(period):
    if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        raise SafetyError("invalid_history_period")
    year, month = map(int, period.split("-"))
    if not 1900 <= year <= 2200:
        raise SafetyError("invalid_history_period")
    return year * 12 + month


def validate_manifest(manifest):
    required = {
        "version",
        "origin",
        "provider",
        "source_url",
        "retrieved_at",
        "price_basis",
        "currency",
        "instruments",
        "assumptions",
    }
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise SafetyError("unsupported_history_manifest")
    version = manifest["version"]
    if version not in {1, 2} or set(manifest) != (
        required | {"provenance"} if version == 2 else required
    ):
        raise SafetyError("unsupported_history_manifest")
    if manifest["origin"] not in {"synthetic_fixture", "declared_historical"}:
        raise SafetyError("history_origin_required")
    basis = "unitized_total_return" if version == 1 else "unitized_adjusted_close_proxy"
    if manifest["price_basis"] != basis or manifest["currency"] != "EUR":
        raise SafetyError("eur_total_return_indices_required")
    if manifest["assumptions"] != (ASSUMPTIONS if version == 1 else ADJUSTED_ASSUMPTIONS):
        raise SafetyError("unsupported_history_assumptions")
    if not isinstance(manifest["provider"], str) or not 1 <= len(manifest["provider"]) <= 160:
        raise SafetyError("history_provider_required")
    url = urlparse(manifest["source_url"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise SafetyError("history_source_url_required")
    instant(manifest["retrieved_at"])
    instruments = manifest["instruments"]
    if not isinstance(instruments, dict) or len(instruments) != 2:
        raise SafetyError("two_research_instruments_required")
    classes, isins = set(), set()
    for asset_id, item in instruments.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", asset_id):
            raise SafetyError("invalid_history_asset_id")
        if not isinstance(item, dict) or set(item) != {
            "label",
            "isin",
            "class",
            "type",
            "ucits",
            "currency",
        }:
            raise SafetyError("unsupported_history_instrument")
        if item["type"] != "equity_security" or item["ucits"] is not True:
            raise SafetyError("declared_ucits_equity_security_required")
        if item["currency"] != "EUR" or item["class"] not in {"equity", "bond"}:
            raise SafetyError("unsupported_history_asset_class_or_currency")
        if not isinstance(item["label"], str) or not 1 <= len(item["label"]) <= 160:
            raise SafetyError("invalid_history_asset_label")
        if not isinstance(item["isin"], str):
            raise SafetyError("invalid_history_isin")
        if manifest["origin"] == "declared_historical":
            if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", item["isin"]):
                raise SafetyError("real_history_requires_isin_format")
        elif not item["isin"].startswith("SIMULATED-"):
            raise SafetyError("fixture_isin_label_required")
        classes.add(item["class"])
        isins.add(item["isin"])
    if classes != {"equity", "bond"} or len(isins) != 2:
        raise SafetyError("distinct_equity_and_bond_instruments_required")
    if version == 2:
        provenance = manifest["provenance"]
        if (
            not isinstance(provenance, dict)
            or set(provenance) != set(EODHD_POLICY) | {"usage_rights", "sources"}
            or any(provenance[key] != value for key, value in EODHD_POLICY.items())
            or provenance["usage_rights"]
            != (
                "synthetic_fixture"
                if manifest["origin"] == "synthetic_fixture"
                else "operator_confirmed_private_analysis"
            )
            or not isinstance(provenance["sources"], dict)
            or set(provenance["sources"]) != set(instruments)
        ):
            raise SafetyError("unsupported_provider_provenance")
        for source in provenance["sources"].values():
            if not isinstance(source, dict) or set(source) != {
                "symbol",
                "raw_sha256",
                "binding_sha256",
                "binding_evidence_url",
            }:
                raise SafetyError("unsupported_provider_source")
            if not isinstance(source["symbol"], str) or not re.fullmatch(
                r"[A-Z0-9_-]{1,32}\.[A-Z]{2,12}", source["symbol"]
            ):
                raise SafetyError("invalid_provider_symbol")
            if any(
                not isinstance(source[key], str) or not re.fullmatch(r"[0-9a-f]{64}", source[key])
                for key in ("raw_sha256", "binding_sha256")
            ):
                raise SafetyError("invalid_provider_source_hash")
            if not isinstance(source["binding_evidence_url"], str):
                raise SafetyError("public_binding_evidence_url_required")
            source_url = urlparse(source["binding_evidence_url"])
            if (
                source_url.scheme != "https"
                or not source_url.hostname
                or source_url.username
                or source_url.password
                or source_url.query
                or source_url.fragment
            ):
                raise SafetyError("public_binding_evidence_url_required")


def _rows_from_csv(raw, manifest):
    if len(raw) > MAX_IMPORT_BYTES:
        raise SafetyError("history_import_size_limit")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
        if reader.fieldnames != list(COLUMNS):
            raise SafetyError("history_csv_columns_mismatch")
        grouped = {}
        seen = set()
        for index, row in enumerate(reader):
            if index >= MAX_PERIODS * 2:
                raise SafetyError("history_period_limit")
            if set(row) != set(COLUMNS) or any(value is None for value in row.values()):
                raise SafetyError("malformed_history_row")
            period, asset = row["period"], row["asset_id"]
            month = period_number(period)
            if asset not in manifest["instruments"]:
                raise SafetyError("history_asset_not_in_manifest")
            if (period, asset) in seen:
                raise SafetyError("duplicate_history_observation")
            seen.add((period, asset))
            signal = instant(row["signal_at"])
            available = instant(row["available_at"])
            execution = instant(row["execution_at"])
            if signal.year * 12 + signal.month != month:
                raise SafetyError("signal_period_mismatch")
            if execution.year * 12 + execution.month != month + 1:
                raise SafetyError("execution_must_be_in_following_month")
            if not signal <= available < execution:
                raise SafetyError("history_not_available_before_execution")
            if execution - signal > timedelta(days=7):
                raise SafetyError("history_execution_gap_exceeds_seven_days")
            if execution > instant(manifest["retrieved_at"]):
                raise SafetyError("history_execution_after_retrieval")
            values = [number(row[name]) for name in ("signal_index", "execution_index")]
            if any(not number("0.000001") <= value <= number("1000000000") for value in values):
                raise SafetyError("invalid_total_return_index")
            times = {
                "signal_at": signal.isoformat(),
                "available_at": available.isoformat(),
                "execution_at": execution.isoformat(),
            }
            period_row = grouped.setdefault(
                period, {"period": period, **times, "signals": {}, "executions": {}}
            )
            if any(period_row[name] != value for name, value in times.items()):
                raise SafetyError("unaligned_history_observations")
            period_row["signals"][asset] = str(values[0])
            period_row["executions"][asset] = str(values[1])
    except (UnicodeError, csv.Error, TypeError, KeyError):
        raise SafetyError("malformed_history_csv") from None
    rows = [grouped[period] for period in sorted(grouped)]
    if len(rows) < 12:
        raise SafetyError("history_requires_warmup_and_two_valuations")
    months = [period_number(row["period"]) for row in rows]
    if months != list(range(months[0], months[-1] + 1)):
        raise SafetyError("history_month_gap_no_imputation")
    assets = set(manifest["instruments"])
    for row in rows:
        if set(row["signals"]) != assets or set(row["executions"]) != assets:
            raise SafetyError("missing_history_asset_observation")
    return rows


def import_history(manifest_path, csv_path):
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    csv_path = Path(csv_path)
    if csv_path.stat().st_size > MAX_IMPORT_BYTES:
        raise SafetyError("history_import_size_limit")
    raw = csv_path.read_bytes()
    rows = _rows_from_csv(raw, manifest)
    body = {
        "version": 1,
        "manifest": manifest,
        "rows": rows,
        "csv_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_hash": digest(manifest),
    }
    return {**body, "dataset_id": digest(body)}


def validate_dataset(dataset):
    if set(dataset) != {"version", "manifest", "rows", "csv_sha256", "manifest_hash", "dataset_id"}:
        raise SafetyError("unsupported_normalized_dataset")
    body = {key: value for key, value in dataset.items() if key != "dataset_id"}
    if dataset["version"] != 1 or digest(body) != dataset["dataset_id"]:
        raise SafetyError("dataset_content_hash_mismatch")
    if digest(dataset["manifest"]) != dataset["manifest_hash"]:
        raise SafetyError("dataset_manifest_hash_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", dataset["csv_sha256"]):
        raise SafetyError("invalid_dataset_source_hash")
    validate_manifest(dataset["manifest"])
    # Recheck all timing and series constraints on normalized files, not just imports.
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS)
    writer.writeheader()
    for row in dataset["rows"]:
        if set(row) != {
            "period",
            "signal_at",
            "available_at",
            "execution_at",
            "signals",
            "executions",
        }:
            raise SafetyError("malformed_normalized_history")
        assets = set(dataset["manifest"]["instruments"])
        if set(row["signals"]) != assets or set(row["executions"]) != assets:
            raise SafetyError("missing_history_asset_observation")
        for asset in row["signals"]:
            writer.writerow(
                {
                    "period": row["period"],
                    "asset_id": asset,
                    "signal_at": row["signal_at"],
                    "available_at": row["available_at"],
                    "execution_at": row["execution_at"],
                    "signal_index": row["signals"][asset],
                    "execution_index": row["executions"][asset],
                }
            )
    if _rows_from_csv(text.getvalue().encode(), dataset["manifest"]) != dataset["rows"]:
        raise SafetyError("normalized_history_not_canonical")


def dataset_receipt(dataset):
    validate_dataset(dataset)
    rows, manifest = dataset["rows"], dataset["manifest"]
    return {
        "dataset_id": dataset["dataset_id"],
        "origin": manifest["origin"],
        "provider": manifest["provider"],
        "price_basis": manifest["price_basis"],
        "periods": len(rows),
        "first_signal_period": rows[0]["period"],
        "last_signal_period": rows[-1]["period"],
        "verification": "provider_declarations_not_independently_verified",
        "broker_mapping": "unverified",
        "data_availability": dataset["manifest"]
        .get("provenance", {})
        .get("availability", "operator_declared_timestamps"),
    }


def write_immutable_json(path, value):
    path = Path(path)
    encoded = canonical(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as output:
            output.write(encoded)
    except FileExistsError:
        if path.read_text() != encoded:
            raise SafetyError("immutable_artifact_exists_with_different_content") from None
    return path


def config_for_dataset(base_config, dataset):
    """Bind identity/provenance to a research run without changing strategy or risk defaults."""
    validate_dataset(dataset)
    result = deepcopy(base_config)
    by_class = {item["class"]: item for item in base_config["assets"].values()}
    result["assets"] = {
        asset: {
            **deepcopy(item),
            "source": "historical_total_return",
            "weight": by_class[item["class"]]["weight"],
        }
        for asset, item in dataset["manifest"]["instruments"].items()
    }
    result["data_contract"] = {
        "dataset_id": dataset["dataset_id"],
        "origin": dataset["manifest"]["origin"],
        "price_basis": dataset["manifest"]["price_basis"],
        "broker_mapping": "unverified",
    }
    return result
