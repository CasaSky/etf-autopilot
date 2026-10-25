import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from etf_autopilot import bitpanda
from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.instruments import asset_uuid, resolve_etf, validate_isin

from .conftest import ROOT

ISIN = "ZZ0000000016"
IDENTIFIER = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def catalog_payload():
    return load_json(ROOT / "fixtures/asset_catalog.json")


def test_isin_checks_format_and_check_digit_without_claiming_registration():
    assert validate_isin(ISIN) == ISIN
    with pytest.raises(SafetyError, match="invalid_isin_check_digit"):
        validate_isin("ZZ0000000017")
    for invalid in [None, 12, "zz0000000016", "SIMULATED-EQUITY", " ZZ0000000016"]:
        with pytest.raises(SafetyError, match="invalid_isin"):
            validate_isin(invalid)


def test_uuid_binding_is_canonical_and_not_a_symbol():
    assert asset_uuid(IDENTIFIER) == IDENTIFIER
    for invalid in [None, "FIXTURE", IDENTIFIER.replace("-", "")]:
        with pytest.raises(SafetyError, match="invalid_broker_asset_id"):
            asset_uuid(invalid)


def test_real_type_can_be_distinguished_from_legacy_same_isin(catalog_payload):
    legacy = {**catalog_payload["data"][0], "id": OTHER_ID, "type": "security"}
    catalog_payload["data"].insert(0, legacy)
    identity = resolve_etf(catalog_payload, ISIN)
    assert identity["broker_asset_id"] == IDENTIFIER
    assert identity["type"] == "equity_security"
    assert "ucits" not in identity and "account_eligible" not in identity


@pytest.mark.parametrize("field,value", [("type", "security"), ("group", "stock")])
def test_legacy_or_non_etf_catalog_entry_is_not_a_real_etf(catalog_payload, field, value):
    catalog_payload["data"][0][field] = value
    with pytest.raises(SafetyError, match="expected_real_etf_not_found"):
        resolve_etf(catalog_payload, ISIN)


def test_multiple_real_listings_require_an_explicit_uuid(catalog_payload):
    catalog_payload["data"].append({**catalog_payload["data"][0], "id": OTHER_ID})
    with pytest.raises(SafetyError, match="ambiguous_real_etf_identity_bind_uuid"):
        resolve_etf(catalog_payload, ISIN)
    assert resolve_etf(catalog_payload, ISIN, OTHER_ID)["broker_asset_id"] == OTHER_ID


def test_changed_uuid_cannot_silently_replace_reviewed_listing(catalog_payload):
    with pytest.raises(SafetyError, match="expected_real_etf_not_found"):
        resolve_etf(catalog_payload, ISIN, OTHER_ID)


def test_duplicate_id_is_rejected_even_when_records_are_identical(catalog_payload):
    catalog_payload["data"].append(deepcopy(catalog_payload["data"][0]))
    with pytest.raises(SafetyError, match="duplicate_broker_asset_id"):
        resolve_etf(catalog_payload, ISIN, IDENTIFIER)


def test_truncated_result_cannot_hide_an_ambiguous_listing(catalog_payload):
    catalog_payload["has_next_page"] = True
    with pytest.raises(SafetyError, match="incomplete_asset_catalog_result"):
        resolve_etf(catalog_payload, ISIN)
    catalog_payload["has_next_page"] = False
    catalog_payload["next_cursor"] = "more"
    with pytest.raises(SafetyError, match="incomplete_asset_catalog_result"):
        resolve_etf(catalog_payload, ISIN)


@pytest.mark.parametrize(
    "payload",
    [{"error": "private upstream text"}, {"data": [], "has_next_page": "false"}, []],
)
def test_error_and_unknown_envelopes_fail_without_echoing_body(payload):
    with pytest.raises(SafetyError, match="unsupported_asset_catalog_response") as error:
        resolve_etf(payload, ISIN)
    assert "private upstream text" not in str(error.value)


def test_paused_catalog_identity_is_recorded_without_granting_permission(catalog_payload):
    catalog_payload["data"][0]["buy_active"] = False
    identity = resolve_etf(catalog_payload, ISIN)
    assert identity["buy_active"] is False
    catalog_payload["data"][0]["sell_active"] = "true"
    with pytest.raises(SafetyError, match="invalid_broker_asset_active_flags"):
        resolve_etf(catalog_payload, ISIN)


def test_metadata_read_is_one_call_and_saves_only_identity(monkeypatch, catalog_payload):
    calls = []

    class Client:
        catalog = {"list_assets": {"inputSchema": {"type": "object"}}}

        async def call(self, name, arguments):
            calls.append((name, arguments))
            return catalog_payload

    @asynccontextmanager
    async def connect(_):
        yield Client()

    monkeypatch.setattr(bitpanda, "connect", connect)
    evidence = asyncio.run(bitpanda.inspect_asset(None, ISIN, IDENTIFIER))
    assert calls == [
        (
            "list_assets",
            {"id": IDENTIFIER, "page_size": 1},
        )
    ]
    assert evidence["identity"]["broker_asset_id"] == IDENTIFIER
    assert evidence["account_eligibility"] == "unverified"
    assert evidence["trading"] == "unavailable"
    assert (
        digest({k: v for k, v in evidence.items() if k != "evidence_id"}) == evidence["evidence_id"]
    )


def test_invalid_identifier_is_rejected_before_connection(monkeypatch):
    def forbidden(_):
        pytest.fail("Invalid input must not open a broker connection")

    monkeypatch.setattr(bitpanda, "connect", forbidden)
    with pytest.raises(SafetyError, match="invalid_isin"):
        asyncio.run(bitpanda.inspect_asset(None, "broken"))


def test_offline_cli_needs_no_broker_sdk_or_credentials():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "etf_autopilot.cli",
            "asset-verify",
            "fixtures/asset_catalog.json",
            "--isin",
            ISIN,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "verified_local_payload"
    assert receipt["identity"]["broker_asset_id"] == IDENTIFIER


def test_saved_request_uuid_is_bound_even_without_repeating_cli_option(
    tmp_path, catalog_payload, capsys
):
    from etf_autopilot.cli import main

    packet = {"tool": "list_assets", "arguments": {"id": OTHER_ID}, "response": catalog_payload}
    path = tmp_path / "response.json"
    path.write_text(json.dumps(packet))
    assert main(["asset-verify", str(path), "--isin", ISIN]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "expected_real_etf_not_found"


def test_invalid_lookup_never_requests_a_secret(monkeypatch, tmp_path, capsys):
    from etf_autopilot import cli

    monkeypatch.setattr(
        cli.getpass, "getpass", lambda _: pytest.fail("No key prompt for invalid input")
    )
    assert cli.main(["broker-asset", "--isin", "bad", "--out", str(tmp_path / "out.json")]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_isin"
