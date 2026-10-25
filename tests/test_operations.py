import json

import pytest

from etf_autopilot.domain import SafetyError
from etf_autopilot.engine import run_event
from etf_autopilot.operations import BedrockDiagnosis, diagnose


def test_default_zero_model_budget_never_calls_provider(store, config, events):
    run_event(store, config, events[0])
    calls = []
    with pytest.raises(SafetyError, match="model_call_budget_exhausted"):
        diagnose(
            store,
            config,
            event_id="failure",
            at=events[0]["at"],
            code="stale_data",
            provider=lambda *args: calls.append(args),
            reserved_cost_eur="0.02",
        )
    assert not calls


@pytest.mark.parametrize(
    "response", ["exception", "not json", '{"max_drawdown":"0.99"}', '{"action":"accept_quote"}']
)
def test_model_errors_consume_reservation_but_cannot_trade_or_change_policy(
    store, config, events, response
):
    config["operations"]["max_model_calls"] = 1
    run_event(store, config, events[0])
    before = store.read()
    calls = []

    def provider(prompt, max_tokens):
        calls.append(prompt)
        if response == "exception":
            raise RuntimeError("model unavailable")
        return response

    receipt = diagnose(
        store,
        config,
        event_id="failure",
        at=events[0]["at"],
        code="stale_data",
        provider=provider,
        reserved_cost_eur="0.02",
    )
    assert receipt["status"] == "failed"
    state = store.read()
    assert state["accounts"] == before["accounts"]
    assert state["config"] == config
    month = state["operations"]["months"]["2026-08"]
    assert month["model_calls"] == 1
    assert month["spend_eur"] == "0.03"
    duplicate = diagnose(
        store,
        config,
        event_id="failure",
        at=events[0]["at"],
        code="stale_data",
        provider=provider,
        reserved_cost_eur="0.02",
    )
    assert duplicate["duplicate"]
    assert len(calls) == 1


def test_successful_model_can_only_propose_allowed_diagnostic_action(store, config, events):
    config["operations"]["max_model_calls"] = 1
    run_event(store, config, events[0])
    receipt = diagnose(
        store,
        config,
        event_id="x",
        at=events[0]["at"],
        code="outcome_unknown",
        provider=lambda *_: json.dumps({"action": "reconcile"}),
        reserved_cost_eur="0.02",
    )
    assert receipt["proposed_action"] == "reconcile"
    assert receipt["ai"] == "proposal_only"
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == 2


def test_bedrock_adapter_uses_bounded_converse_without_tools():
    class Client:
        def converse(self, **kwargs):
            assert "toolConfig" not in kwargs
            assert kwargs["inferenceConfig"]["maxTokens"] == 100
            assert kwargs["modelId"] == "explicit-approved-model"
            return {"output": {"message": {"content": [{"text": '{"action":"none"}'}]}}}

    assert BedrockDiagnosis(Client(), "explicit-approved-model")("diagnose", 100) == (
        '{"action":"none"}'
    )


def test_without_ai_keeps_routine_costs_and_excludes_only_inference(store, config, events):
    from etf_autopilot.domain import money
    from etf_autopilot.report import summary

    config["operations"]["max_model_calls"] = 1
    run_event(store, config, events[0])
    before = summary(store.read())["accounts"]
    assert before["strategy"]["net_equity_eur"] == before["without_ai"]["net_equity_eur"]
    diagnose(
        store,
        config,
        event_id="x",
        at=events[0]["at"],
        code="outcome_unknown",
        provider=lambda *_: '{"action":"reconcile"}',
        reserved_cost_eur="0.02",
    )
    after = summary(store.read())["accounts"]
    assert money(after["without_ai"]["net_equity_eur"]) - money(
        after["strategy"]["net_equity_eur"]
    ) == money("0.02")
    assert after["without_ai"]["operations_eur"] == "0.01"
