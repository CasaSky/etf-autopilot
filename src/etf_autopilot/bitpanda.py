"""Read-only boundary to Bitpanda's official hosted Public MCP.

Tool schemas are discovered from the authenticated server, never reconstructed
from REST. Fixture envelopes test protocol handling, not live account compatibility.
There is deliberately no quote acceptance or trading-limit mutation capability.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from jsonschema import Draft202012Validator, ValidationError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .domain import SafetyError, canonical, digest, instant
from .instruments import asset_uuid, resolve_etf, validate_isin

ENDPOINT = "https://mcp.public.bitpanda.com"
READ_TOOLS = frozenset(
    {
        "list_assets",
        "list_available_currencies",
        "get_ticker",
        "get_portfolio_holdings",
        "get_portfolio_history",
        "list_operation_history",
    }
)
BLOCKED_TOOLS = frozenset(
    {"request_for_quote", "accept_quote", "set_trading_limit", "get_trading_limit"}
)
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    expires_at: str

    def validate(self, now=None):
        now = now or datetime.now(UTC)
        if not self.api_key or "\n" in self.api_key or "\r" in self.api_key:
            raise SafetyError("invalid_credentials")
        if instant(self.expires_at) <= now:
            raise SafetyError("credentials_expired")


def _local_schema(schema):
    if isinstance(schema, dict):
        if "$ref" in schema and not str(schema["$ref"]).startswith("#"):
            raise SafetyError("remote_schema_reference_blocked")
        for value in schema.values():
            _local_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _local_schema(value)


def validate_catalog(tools):
    catalog = {}
    for tool in tools:
        name = tool["name"]
        if name not in READ_TOOLS:
            continue
        if name in catalog:
            raise SafetyError("duplicate_tool_schema")
        schema = tool["inputSchema"]
        _local_schema(schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise SafetyError("object_tool_schema_required")
        output = tool.get("outputSchema")
        if output is not None:
            _local_schema(output)
            Draft202012Validator.check_schema(output)
        catalog[name] = {"name": name, "inputSchema": schema, "outputSchema": output}
    if not catalog:
        raise SafetyError("no_allowed_tools_discovered")
    return catalog


def normalize_result(result, output_schema=None):
    if result.get("isError", False):
        raise SafetyError("broker_tool_error_check_credentials_and_scopes")
    payload = result.get("structuredContent")
    if payload is None:
        blocks = result.get("content", [])
        if len(blocks) != 1 or blocks[0].get("type") != "text":
            raise SafetyError("unverified_tool_response_shape")
        try:
            payload = json.loads(blocks[0]["text"], parse_float=str)
        except (ValueError, KeyError):
            raise SafetyError("unverified_tool_response_shape") from None
    if len(canonical(payload).encode()) > MAX_RESPONSE_BYTES:
        raise SafetyError("broker_response_too_large")
    if not isinstance(payload, (dict, list)):
        raise SafetyError("unverified_tool_response_shape")
    if output_schema:
        try:
            Draft202012Validator(output_schema).validate(payload)
        except ValidationError:
            raise SafetyError("broker_output_schema_mismatch") from None
    return payload


class ReadOnlyPublicMCP:
    def __init__(self, session, credentials, catalog):
        self._session, self._credentials, self.catalog = session, credentials, catalog
        self.calls = 0

    async def call(self, name, arguments):
        if name not in READ_TOOLS:
            raise SafetyError("broker_tool_not_allowed")
        self._credentials.validate()
        if name not in self.catalog:
            raise SafetyError("required_tool_missing")
        if self.calls >= 10:
            raise SafetyError("broker_session_call_limit")
        try:
            Draft202012Validator(self.catalog[name]["inputSchema"]).validate(arguments)
        except ValidationError:
            raise SafetyError("broker_arguments_schema_mismatch") from None
        self.calls += 1
        try:
            result = await self._session.call_tool(name, arguments=arguments)
            return normalize_result(
                result.model_dump(by_alias=True, exclude_none=True),
                self.catalog[name]["outputSchema"],
            )
        except SafetyError:
            raise
        except Exception:
            # Do not expose upstream messages/response bodies or transport details.
            raise SafetyError("broker_read_failed_no_automatic_retry") from None


@asynccontextmanager
async def connect(credentials):
    credentials.validate()
    try:
        async with (
            asyncio.timeout(45),
            httpx.AsyncClient(
                headers={"x-api-key": credentials.api_key},
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            ) as http,
        ):
            async with streamable_http_client(ENDPOINT, http_client=http) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=20)
                ) as s:
                    await s.initialize()
                    discovered, cursor = [], None
                    for _ in range(5):
                        page = await s.list_tools(cursor=cursor)
                        discovered.extend(
                            t.model_dump(by_alias=True, exclude_none=True) for t in page.tools
                        )
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    if cursor:
                        raise SafetyError("tool_catalog_page_limit")
                    yield ReadOnlyPublicMCP(s, credentials, validate_catalog(discovered))
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("broker_connection_failed_check_key_expiry_scopes") from None


async def check_account(credentials):
    """Opt-in network operation; return a small receipt, never holdings or key data."""
    async with connect(credentials) as client:
        # Empty arguments are only used if the actual discovered schema permits them.
        holdings = await client.call("get_portfolio_holdings", {})
        return {
            "endpoint": ENDPOINT,
            "status": "read_succeeded",
            "tools": sorted(client.catalog),
            "catalog_hash": digest(client.catalog),
            "response_kind": type(holdings).__name__,
            "domain_mapping": "unverified_pending_account_contract_review",
            "trading": "unavailable",
        }


async def inspect_asset(credentials, isin, expected_asset_id=None):
    """One catalog read with explicit identity checks; no account data or ticker call."""
    validate_isin(isin)
    if expected_asset_id is not None:
        asset_uuid(expected_asset_id)
    async with connect(credentials) as client:
        arguments = (
            {"id": asset_uuid(expected_asset_id), "page_size": 1}
            if expected_asset_id is not None
            else {"isin": isin, "type": "equity_security", "group": "equity_etf", "page_size": 100}
        )
        payload = await client.call("list_assets", arguments)
        identity = resolve_etf(payload, isin, expected_asset_id)
        evidence = {
            "version": 1,
            "source": ENDPOINT,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "catalog_hash": digest(client.catalog),
            "identity": identity,
            "purpose": "catalog_identity_only",
            "account_eligibility": "unverified",
            "trading": "unavailable",
        }
        return {**evidence, "evidence_id": digest(evidence)}


def credentials_from_environment():
    key = os.environ.get("BITPANDA_API_KEY")
    expiry = os.environ.get("BITPANDA_KEY_EXPIRES_AT")
    if not key or not expiry:
        raise SafetyError("credentials_not_configured_use_secure_prompt")
    return Credentials(key, expiry)
