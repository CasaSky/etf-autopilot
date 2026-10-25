"""Deterministic identity checks, independent of credentials and broker execution.

A catalog identity is not UCITS evidence, account eligibility or a trade permission.
Do not infer asset allocation roles from names or symbols.
"""

import re
from uuid import UUID

from .domain import SafetyError


def validate_isin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value):
        raise SafetyError("invalid_isin")
    digits = "".join(str(int(char, 36)) if char.isalpha() else char for char in value)
    checksum = 0
    for offset, char in enumerate(reversed(digits)):
        digit = int(char) * (2 if offset % 2 else 1)
        checksum += digit // 10 + digit % 10
    if checksum % 10:
        raise SafetyError("invalid_isin_check_digit")
    return value


def asset_uuid(value):
    if not isinstance(value, str):
        raise SafetyError("invalid_broker_asset_id")
    try:
        normalized = str(UUID(value))
    except ValueError:
        raise SafetyError("invalid_broker_asset_id") from None
    if normalized != value.lower():
        raise SafetyError("invalid_broker_asset_id")
    return normalized


def _label(value, limit):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SafetyError("invalid_broker_asset_label")
    return value


def resolve_etf(payload, expected_isin, expected_asset_id=None):
    """Resolve one complete list_assets result; never choose between real listings.

    Legacy records may share an ISIN. Only the explicit equity_security/equity_etf
    combination qualifies. An expected UUID binds an already reviewed listing.
    Active flags are recorded, not used as account-specific execution permission.
    """
    validate_isin(expected_isin)
    expected_id = asset_uuid(expected_asset_id) if expected_asset_id is not None else None
    if (
        not isinstance(payload, dict)
        or not {"data", "has_next_page"} <= set(payload)
        or set(payload) - {"data", "has_next_page", "next_cursor"}
        or not isinstance(payload["data"], list)
        or len(payload["data"]) > 100
        or type(payload["has_next_page"]) is not bool
    ):
        raise SafetyError("unsupported_asset_catalog_response")
    if payload["has_next_page"] or payload.get("next_cursor"):
        raise SafetyError("incomplete_asset_catalog_result")
    candidates = []
    seen = set()
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            raise SafetyError("malformed_asset_catalog_record")
        if raw.get("isin") != expected_isin:
            continue
        if raw.get("type") != "equity_security" or raw.get("group") != "equity_etf":
            continue
        identifier = asset_uuid(raw.get("id"))
        if identifier in seen:
            raise SafetyError("duplicate_broker_asset_id")
        seen.add(identifier)
        if type(raw.get("buy_active")) is not bool or type(raw.get("sell_active")) is not bool:
            raise SafetyError("invalid_broker_asset_active_flags")
        identity = {
            "broker_asset_id": identifier,
            "isin": expected_isin,
            "name": _label(raw.get("name"), 160),
            "symbol": _label(raw.get("symbol"), 32),
            "type": "equity_security",
            "group": "equity_etf",
            "buy_active": raw["buy_active"],
            "sell_active": raw["sell_active"],
        }
        if expected_id is None or identifier == expected_id:
            candidates.append(identity)
    if not candidates:
        raise SafetyError("expected_real_etf_not_found")
    if len(candidates) != 1:
        raise SafetyError("ambiguous_real_etf_identity_bind_uuid")
    return candidates[0]
