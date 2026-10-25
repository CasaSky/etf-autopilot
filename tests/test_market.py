from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.market import ticker_receipt

from .conftest import ROOT

ASSET = "00000000-0000-4000-8000-000000000001"
CURRENCY = "00000000-0000-4000-8000-000000000002"


def evidence():
    body = {
        "version": 1,
        "source": "https://mcp.public.bitpanda.com",
        "retrieved_at": "2026-10-25T17:00:00Z",
        "catalog_hash": "0" * 64,
        "identity": {
            **load_json(ROOT / "fixtures/asset_catalog.json")["data"][0],
            "broker_asset_id": ASSET,
        },
        "ticker_response": {
            "data": {"asset_id": ASSET, "currency_id": CURRENCY, "price": "23.31550000"}
        },
        "purpose": "public_market_contract_review_only",
        "data_mapping": "unverified_not_accepted_by_engine",
        "account_eligibility": "unverified",
        "trading": "unavailable",
    }
    return {**body, "evidence_id": digest(body)}


def rehash(body):
    body["evidence_id"] = digest(
        {key: value for key, value in body.items() if key != "evidence_id"}
    )
    return body


def test_retrieval_timestamp_does_not_claim_freshness_or_an_executable_quote():
    result = ticker_receipt(evidence())
    assert result["price"] == "23.31550000"
    assert result["observed_at"] == "2026-10-25T17:00:00+00:00"
    assert result["price_as_of"] is None and result["price_age"] == "unknown"
    assert result["currency_mapping"] == "unverified" and result["spread"] == "unknown"
    assert result["execution_eligible"] is False and result["trading"] == "unavailable"


@pytest.mark.parametrize("price", [0.1, 23, True, "0", "-1", "NaN", "Infinity", "1000000001"])
def test_invalid_or_lossy_price_is_not_accepted(price):
    body = evidence()
    body["ticker_response"]["data"]["price"] = price
    with pytest.raises(SafetyError):
        ticker_receipt(rehash(body))


def test_ticker_cannot_be_bound_to_another_asset_or_changed_after_capture():
    body = evidence()
    body["ticker_response"]["data"]["asset_id"] = CURRENCY
    with pytest.raises(SafetyError, match="market_evidence_hash_mismatch"):
        ticker_receipt(body)
    with pytest.raises(SafetyError, match="ticker_asset_identity_mismatch"):
        ticker_receipt(rehash(body))


def test_response_schema_change_requires_review_instead_of_guessed_timestamp_mapping():
    body = evidence()
    body["ticker_response"]["data"]["timestamp"] = "2026-10-25T17:00:00Z"
    with pytest.raises(SafetyError, match="unsupported_ticker_response"):
        ticker_receipt(rehash(body))
    legacy = deepcopy(evidence())
    legacy["identity"]["type"] = "security"
    with pytest.raises(SafetyError, match="real_etf_identity_required"):
        ticker_receipt(rehash(legacy))
