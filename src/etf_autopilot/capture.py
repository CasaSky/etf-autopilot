"""Finite public-issuer capture outside the model; timestamps are receipt times."""

import hashlib
import re
import time
from copy import deepcopy
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .domain import SafetyError, digest, instant, load_json
from .history import ECB_HISTORY_URL, validate_dataset, write_immutable_json
from .issuer import import_issuer
from .nav import GROWTH_FORMAT, ISHARES_NAV_FORMAT, NAV_FORMAT

MAX_CAPTURE_SECONDS = 45


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url, limit, deadline):
    """One unauthenticated HTTPS attempt; no redirects, cookies, proxies or retries."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SafetyError("issuer_capture_deadline_exceeded")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={"User-Agent": "ETF-Autopilot/0.1 public-issuer-capture"})
    try:
        with opener.open(request, timeout=min(15, remaining)) as response:
            if response.status != 200:
                raise SafetyError("issuer_capture_http_failure")
            parts, total = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise SafetyError("issuer_capture_deadline_exceeded")
                chunk = response.read(min(65_536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise SafetyError("issuer_capture_source_size_limit")
                parts.append(chunk)
            return b"".join(parts)
    except HTTPError, URLError, TimeoutError, OSError, HTTPException:
        raise SafetyError("issuer_capture_network_failure") from None


def _requests(plan):
    if (
        not isinstance(plan, dict)
        or set(plan) != {"version", "spec", "downloads"}
        or (type(plan["version"]) is not int or plan["version"] != 1)
    ):
        raise SafetyError("unsupported_issuer_capture_plan")
    spec, downloads = plan["spec"], plan["downloads"]
    if (
        not isinstance(spec, dict)
        or not isinstance(downloads, dict)
        or not isinstance(spec.get("instruments"), dict)
        or len(spec["instruments"]) != 2
        or set(downloads) != set(spec["instruments"])
        or not isinstance(spec.get("bindings"), dict)
        or set(spec["bindings"]) != set(downloads)
    ):
        raise SafetyError("issuer_capture_universe_mismatch")
    roles = {}
    for asset, item in spec["instruments"].items():
        if not isinstance(item, dict) or not isinstance(item.get("class"), str):
            raise SafetyError("issuer_capture_universe_mismatch")
        roles[item["class"]] = asset
    if set(roles) != {"equity", "bond"}:
        raise SafetyError("distinct_equity_and_bond_instruments_required")
    requests = []
    for role in ("equity", "bond"):
        asset = roles[role]
        download, binding = downloads[asset], spec["bindings"][asset]
        if not isinstance(download, dict) or not isinstance(binding, dict):
            raise SafetyError("unsupported_issuer_capture_source")
        source_format = binding.get("source_format", GROWTH_FORMAT)
        if download.get("provider") == "ishares" and set(download) == {"provider", "portfolio_id"}:
            product = download["portfolio_id"]
            if (
                not isinstance(product, str)
                or not re.fullmatch(r"[1-9][0-9]{0,7}", product)
                or source_format not in {GROWTH_FORMAT, ISHARES_NAV_FORMAT}
            ):
                raise SafetyError("invalid_issuer_capture_product")
            url = (
                "https://www.blackrock.com/varnish-api/uk-retail01-product-data/product-data/api/v1/get-fund-document?"
                + urlencode(
                    {
                        "appSubType": "ISHARES",
                        "appType": "PRODUCT_PAGE",
                        "component": "fundDownloadV2",
                        "locale": "en_GB",
                        "portfolioId": product,
                        "targetSite": "ishares-uk",
                        "userType": "individual",
                    }
                )
            )
            filename, limit = role + ".xls", 32_000_000
        elif download.get("provider") == "ssga" and set(download) == {"provider", "download_code"}:
            code = download["download_code"]
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+){0,3}", code)
                or len(code) > 32
                or source_format != NAV_FORMAT
                or role != "equity"
            ):
                raise SafetyError("invalid_issuer_capture_product")
            url = f"https://www.ssga.com/library-content/products/fund-data/etfs/emea/navhist-emea-en-{code}.xlsx"
            filename, limit = role + ".xlsx", 8_000_000
        else:
            raise SafetyError("unsupported_issuer_capture_source")
        requests.append((role, filename, url, limit))
    requests.append(("fx", "ecb.xml", ECB_HISTORY_URL, 15_000_000))
    return requests


def _hash_file(path):
    result, total = hashlib.sha256(), 0
    with path.open("rb") as source:
        while raw := source.read(65_536):
            total += len(raw)
            if total > 32_000_000:
                raise SafetyError("issuer_capture_source_size_limit")
            result.update(raw)
    return result.hexdigest()


def _replay(directory, plan_hash, requests):
    receipt = load_json(directory / "capture.json")
    body = {key: value for key, value in receipt.items() if key != "evidence_id"}
    if receipt.get("evidence_id") != digest(body) or receipt.get("plan_hash") != plan_hash:
        raise SafetyError("issuer_capture_identity_conflict")
    allowed = {request[1] for request in requests} | {
        "spec.json",
        "normalized/manifest.json",
        "normalized/monthly.csv",
        "normalized/dataset.json",
    }
    hashes = receipt.get("file_hashes")
    if not isinstance(hashes, dict) or set(hashes) != allowed:
        raise SafetyError("invalid_issuer_capture_files")
    for name, value in hashes.items():
        if _hash_file(directory / name) != value:
            raise SafetyError("issuer_capture_file_changed")
    dataset = load_json(directory / "normalized/dataset.json")
    validate_dataset(dataset)
    if dataset["dataset_id"] != receipt["dataset_id"]:
        raise SafetyError("issuer_capture_dataset_changed")
    return {**receipt, "duplicate": True}


def capture_issuer(plan_path, output_directory, *, fetch=None, clock=None):
    """Capture one reviewed pair and ECB, then import; never evaluate or place orders.

    A completed directory replays without network calls. A failed/incomplete capture
    requires a new directory, preserving the earlier bytes and failure evidence.
    """
    plan_path, directory = Path(plan_path), Path(output_directory)
    if plan_path.stat().st_size > 100_000:
        raise SafetyError("issuer_capture_plan_size_limit")
    plan = load_json(plan_path)
    requests = _requests(plan)
    plan_hash = digest(plan)
    if (directory / "capture.json").is_file():
        return _replay(directory, plan_hash, requests)
    if directory.exists():
        raise SafetyError("issuer_capture_incomplete_use_new_directory")
    clock_source = "injected_clock" if clock is not None else "runtime_utc"
    transport = "injected_provider" if fetch is not None else "public_https_single_attempt"
    clock = clock or (lambda: datetime.now(UTC))
    started = instant(clock().isoformat())
    directory.mkdir(parents=True, mode=0o700)
    write_immutable_json(
        directory / "capture.pending.json",
        {"plan_hash": plan_hash, "started_at": started.isoformat()},
    )
    fetch = fetch or _download
    deadline = time.monotonic() + MAX_CAPTURE_SECONDS
    sources, paths = [], {}
    try:
        for role, filename, url, limit in requests:
            if time.monotonic() >= deadline:
                raise SafetyError("issuer_capture_deadline_exceeded")
            raw = fetch(url, limit, deadline)
            if not isinstance(raw, bytes) or not raw or len(raw) > limit:
                raise SafetyError("issuer_capture_source_size_limit")
            received = instant(clock().isoformat())
            if received < started or (sources and received < instant(sources[-1]["received_at"])):
                raise SafetyError("issuer_capture_clock_reversed")
            path = directory / filename
            with path.open("xb") as target:
                target.write(raw)
            path.chmod(0o600)
            paths[role] = path
            sources.append(
                {
                    "role": role,
                    "file": filename,
                    "source_url": url,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "received_at": received.isoformat(),
                }
            )
        spec = deepcopy(plan["spec"])
        spec["retrieved_at"] = sources[-1]["received_at"]
        write_immutable_json(directory / "spec.json", spec)
        dataset = import_issuer(
            directory / "spec.json",
            paths["equity"],
            paths["bond"],
            paths["fx"],
            directory / "normalized",
        )
        names = [source["file"] for source in sources] + [
            "spec.json",
            "normalized/manifest.json",
            "normalized/monthly.csv",
            "normalized/dataset.json",
        ]
        receipt = {
            "version": 1,
            "origin": dataset["manifest"]["origin"],
            "transport": transport,
            "clock_source": clock_source,
            "plan_hash": plan_hash,
            "started_at": started.isoformat(),
            "received_at": spec["retrieved_at"],
            "sources": sources,
            "dataset_id": dataset["dataset_id"],
            "file_hashes": {name: _hash_file(directory / name) for name in names},
            "timing_evidence": "receipt_times_only_original_publication_times_unknown",
            "execution_eligible": False,
            "model_calls": 0,
            "automatic_promotion": False,
        }
        receipt["evidence_id"] = digest(receipt)
        write_immutable_json(directory / "capture.json", receipt)
        return {**receipt, "duplicate": False}
    except Exception:
        write_immutable_json(
            directory / "capture.failed.json",
            {"plan_hash": plan_hash, "status": "incomplete_do_not_use_for_decisions"},
        )
        raise
