"""Typed public ticker evidence; retrieval time never substitutes for price time."""

import re

from .domain import SafetyError, digest, instant, number
from .instruments import asset_uuid, validate_isin

ENDPOINT = "https://mcp.public.bitpanda.com"


def ticker_receipt(evidence):
    """Validate the observed response contract without granting execution eligibility.

    This response has no price timestamp, spread or verified currency-symbol mapping.
    It is useful integration evidence, not an admissible fresh market event.
    """
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            "version",
            "source",
            "retrieved_at",
            "catalog_hash",
            "identity",
            "ticker_response",
            "purpose",
            "data_mapping",
            "account_eligibility",
            "trading",
            "evidence_id",
        }
        or type(evidence["version"]) is not int
        or evidence["version"] != 1
        or evidence["source"] != ENDPOINT
        or evidence["purpose"] != "public_market_contract_review_only"
        or evidence["data_mapping"] != "unverified_not_accepted_by_engine"
        or evidence["account_eligibility"] != "unverified"
        or evidence["trading"] != "unavailable"
    ):
        raise SafetyError("unsupported_market_contract_evidence")
    body = {key: value for key, value in evidence.items() if key != "evidence_id"}
    if digest(body) != evidence["evidence_id"]:
        raise SafetyError("market_evidence_hash_mismatch")
    if not isinstance(evidence["catalog_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", evidence["catalog_hash"]
    ):
        raise SafetyError("invalid_market_catalog_hash")
    identity = evidence["identity"]
    if (
        not isinstance(identity, dict)
        or identity.get("type") != "equity_security"
        or identity.get("group") != "equity_etf"
    ):
        raise SafetyError("real_etf_identity_required")
    identifier = asset_uuid(identity.get("broker_asset_id"))
    isin = validate_isin(identity.get("isin"))
    payload = evidence["ticker_response"]
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        raise SafetyError("unsupported_ticker_response")
    data = payload["data"]
    if not isinstance(data, dict) or set(data) != {"asset_id", "currency_id", "price"}:
        raise SafetyError("unsupported_ticker_response")
    if asset_uuid(data["asset_id"]) != identifier:
        raise SafetyError("ticker_asset_identity_mismatch")
    currency_id = asset_uuid(data["currency_id"])
    if not isinstance(data["price"], str) or not 0 < number(data["price"]) <= 1_000_000_000:
        raise SafetyError("invalid_ticker_price")
    receipt = {
        "version": 1,
        "source": ENDPOINT,
        "source_evidence_id": evidence["evidence_id"],
        "broker_asset_id": identifier,
        "isin": isin,
        "currency_id": currency_id,
        "currency_mapping": "unverified",
        "price": str(number(data["price"])),
        "observed_at": instant(evidence["retrieved_at"]).isoformat(),
        "price_as_of": None,
        "price_age": "unknown",
        "spread": "unknown",
        "execution_eligible": False,
        "account_eligibility": "unverified",
        "trading": "unavailable",
    }
    return {**receipt, "receipt_id": digest(receipt)}
