import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from etf_autopilot.bitpanda import (
    BLOCKED_TOOLS,
    Credentials,
    ReadOnlyPublicMCP,
    normalize_result,
    validate_catalog,
)
from etf_autopilot.domain import SafetyError, load_json

from .conftest import ROOT

CONTRACT = load_json(ROOT / "fixtures/mcp_contract.json")
KEY = "fixture-secret-never-log"


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult.model_validate(CONTRACT["result"])


def client(session=None, expiry="2099-01-01T00:00:00Z"):
    return ReadOnlyPublicMCP(
        session or FakeSession(), Credentials(KEY, expiry), validate_catalog(CONTRACT["tools"])
    )


@pytest.mark.parametrize(
    "name", sorted(BLOCKED_TOOLS | {"earn_stake", "get_crypto_asset_networks"})
)
def test_allowlist_blocks_trading_limits_quotes_and_unrelated_tools(name):
    session = FakeSession()
    with pytest.raises(SafetyError, match="broker_tool_not_allowed"):
        asyncio.run(client(session).call(name, {}))
    assert session.calls == []


def test_catalog_never_exposes_mutations_or_arbitrary_server_tools():
    assert set(validate_catalog(CONTRACT["tools"])) == {"get_portfolio_holdings"}


def test_expired_key_fails_before_any_network_call():
    session = FakeSession()
    with pytest.raises(SafetyError, match="credentials_expired"):
        asyncio.run(client(session, "2000-01-01T00:00:00Z").call("get_portfolio_holdings", {}))
    assert session.calls == []
    assert KEY not in repr(Credentials(KEY, "2000-01-01T00:00:00Z"))


def test_credentials_expiry_boundary_and_timezone():
    now = datetime(2026, 10, 24, tzinfo=UTC)
    with pytest.raises(SafetyError, match="credentials_expired"):
        Credentials(KEY, now.isoformat()).validate(now)
    Credentials(KEY, (now + timedelta(seconds=1)).isoformat()).validate(now)
    with pytest.raises(SafetyError, match="timezone_required"):
        Credentials(KEY, "2026-10-24").validate(now)


def test_actual_discovered_argument_schema_is_enforced_before_call():
    session = FakeSession()
    with pytest.raises(SafetyError, match="broker_arguments_schema_mismatch"):
        asyncio.run(client(session).call("get_portfolio_holdings", {"api_key": KEY}))
    assert session.calls == []


def test_remote_schema_refs_cannot_trigger_outbound_requests():
    tools = [{"name": "list_assets", "inputSchema": {"type": "object", "$ref": "https://x"}}]
    with pytest.raises(SafetyError, match="remote_schema_reference_blocked"):
        validate_catalog(tools)


def test_structured_content_and_json_text_contracts():
    assert normalize_result(CONTRACT["result"]) == {"fixture_holdings": []}
    assert normalize_result({"content": [{"type": "text", "text": '{"fixture_holdings":[]}'}]}) == {
        "fixture_holdings": []
    }
    with pytest.raises(SafetyError, match="unverified_tool_response_shape"):
        normalize_result({"content": [{"type": "text", "text": "upstream error"}]})
    with pytest.raises(SafetyError, match="broker_output_schema_mismatch"):
        normalize_result(
            {"structuredContent": {"unexpected": []}}, CONTRACT["tools"][0]["outputSchema"]
        )


def test_server_tool_errors_and_transport_exceptions_do_not_leak_secrets():
    with pytest.raises(SafetyError) as error:
        normalize_result({"isError": True, "content": [{"type": "text", "text": KEY}]})
    assert KEY not in str(error.value)

    class ExplodingSession:
        async def call_tool(self, *args, **kwargs):
            raise RuntimeError(f"sensitive upstream response: {KEY}")

    with pytest.raises(SafetyError, match="broker_read_failed_no_automatic_retry") as error:
        asyncio.run(client(ExplodingSession()).call("get_portfolio_holdings", {}))
    assert KEY not in str(error.value)


def test_session_call_budget_prevents_unbounded_reads():
    async def run():
        c = client()
        for _ in range(10):
            await c.call("get_portfolio_holdings", {})
        with pytest.raises(SafetyError, match="broker_session_call_limit"):
            await c.call("get_portfolio_holdings", {})

    asyncio.run(run())


def test_official_sdk_protocol_over_mock_http_without_contacting_bitpanda():
    """Exercise initialize/list_tools/call_tool with SDK serialization and transport."""
    methods = []

    def respond(request):
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "protocol-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": CONTRACT["tools"]}
        elif method == "tools/call":
            assert body["params"]["name"] == "get_portfolio_holdings"
            result = CONTRACT["result"]
        else:
            pytest.fail(f"Unexpected method: {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with streamable_http_client(
                "https://fixture.invalid", http_client=http
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    page = await session.list_tools()
                    catalog = validate_catalog([tool.model_dump() for tool in page.tools])
                    c = ReadOnlyPublicMCP(
                        session, Credentials(KEY, "2099-01-01T00:00:00Z"), catalog
                    )
                    assert await c.call("get_portfolio_holdings", {}) == {"fixture_holdings": []}

    asyncio.run(run())
    assert methods.count("tools/call") == 1
