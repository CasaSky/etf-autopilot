"""Daily research indices derived from a verified immutable source capture."""

import json
import re
from pathlib import Path

from .capture import _replay, _requests
from .domain import SafetyError, canonical, digest, instant, load_json, number
from .history import validate_manifest, write_immutable_json
from .issuer import prepare_issuer
from .nav import iso_day

DAILY_POLICY = {
    "price_basis": "unitized_observed_issuer_proxy",
    "units": "research_index_units_not_broker_shares",
    "calendar": "joint_fund_and_ecb_dates_without_imputation",
    "normalization": "first_joint_observation_100",
    "timing": "runtime_capture_receipt_original_publication_times_unknown",
}
MAX_DAILY_BYTES = 5_000_000


def validate_observations(data):
    if len(canonical(data).encode()) > MAX_DAILY_BYTES:
        raise SafetyError("daily_observation_size_limit")
    fields = {
        "version",
        "capture_id",
        "captured_at",
        "manifest",
        "policy",
        "rows",
        "observation_id",
    }
    if (
        not isinstance(data, dict)
        or set(data) != fields
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        raise SafetyError("unsupported_daily_observations")
    if data["observation_id"] != digest({k: v for k, v in data.items() if k != "observation_id"}):
        raise SafetyError("daily_observations_hash_mismatch")
    if not isinstance(data["capture_id"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", data["capture_id"]
    ):
        raise SafetyError("invalid_daily_capture_identity")
    validate_manifest(data["manifest"])
    if data["manifest"]["version"] not in {3, 4} or data["policy"] != DAILY_POLICY:
        raise SafetyError("unsupported_daily_index_policy")
    captured = instant(data["captured_at"])
    if captured != instant(data["manifest"]["retrieved_at"]):
        raise SafetyError("daily_capture_time_mismatch")
    if not isinstance(data["rows"], list) or not 12 <= len(data["rows"]) <= 50_000:
        raise SafetyError("daily_observation_row_limit")
    assets = set(data["manifest"]["instruments"])
    previous = None
    for row in data["rows"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"date", "values"}
            or not isinstance(row["values"], dict)
            or set(row["values"]) != assets
        ):
            raise SafetyError("daily_observation_universe_mismatch")
        day = iso_day(row["date"])
        if (
            day.weekday() >= 5
            or (previous is not None and day <= previous)
            or day >= captured.date()
        ):
            raise SafetyError("daily_observation_date_invalid_or_not_completed")
        for value in row["values"].values():
            if (
                not isinstance(value, str)
                or len(value) > 40
                or not number("0.000001") <= number(value) <= number("1000000000")
            ):
                raise SafetyError("invalid_daily_index_value")
        previous = day
    if any(number(value) != 100 for value in data["rows"][0]["values"].values()):
        raise SafetyError("daily_index_basis_must_start_at_100")


def observations_for_capture(plan_path, directory):
    """Verify the retained capture before deriving; no network or current-time rewrite."""
    plan_path, directory = Path(plan_path), Path(directory)
    if plan_path.stat().st_size > 100_000:
        raise SafetyError("issuer_capture_plan_size_limit")
    plan = load_json(plan_path)
    requests = _requests(plan)
    if not (directory / "capture.json").is_file():
        raise SafetyError("completed_capture_required")
    receipt = _replay(directory, digest(plan), requests)
    paths = {role: directory / filename for role, filename, _, _ in requests}
    _, manifest, series = prepare_issuer(
        directory / "spec.json", paths["equity"], paths["bond"], paths["fx"], observed_tail=True
    )
    normalized = load_json(directory / "normalized/dataset.json")
    if digest(manifest) != normalized["manifest_hash"]:
        raise SafetyError("daily_and_monthly_source_context_mismatch")
    dates = sorted(set.intersection(*(set(bars) for bars in series.values())))
    body = {
        "version": 1,
        "capture_id": receipt["evidence_id"],
        "captured_at": receipt["received_at"],
        "manifest": manifest,
        "policy": dict(DAILY_POLICY),
        "rows": [
            {
                "date": day.isoformat(),
                "values": {asset: str(bars[day]) for asset, bars in series.items()},
            }
            for day in dates
        ],
    }
    result = {**body, "observation_id": digest(body)}
    validate_observations(result)
    return result


def save_observations(plan_path, directory, output):
    data = observations_for_capture(plan_path, directory)
    write_immutable_json(output, data)
    Path(output).chmod(0o600)
    return data


def load_observations(path):
    path = Path(path)
    if path.stat().st_size > MAX_DAILY_BYTES:
        raise SafetyError("daily_observation_size_limit")
    with path.open("rb") as source:
        raw = source.read(MAX_DAILY_BYTES + 1)
    if len(raw) > MAX_DAILY_BYTES:
        raise SafetyError("daily_observation_size_limit")
    data = json.loads(raw, parse_float=str)
    validate_observations(data)
    return data
