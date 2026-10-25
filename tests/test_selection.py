import json
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, money
from etf_autopilot.engine import run_event
from etf_autopilot.report import markdown, summary
from etf_autopilot.selection import (
    BedrockAllocation,
    abandon_selection,
    run_decision,
    select_strategy,
)


def model_options(provider, **extra):
    return {
        "provider": provider,
        "provider_name": "scripted_fixture",
        "reserved_cost_eur": "0.02",
        **extra,
    }


def test_default_zero_budget_prevents_inference_and_orders_but_values_market(store, config, events):
    calls = []
    result = run_decision(
        store, config, events[0], **model_options(lambda *args: calls.append(args))
    )
    assert result["status"] == "halted" and result["orders"] == 0
    assert result["selection"]["failure_code"] == "model_call_budget_exhausted"
    assert calls == []
    state = store.read()
    assert state["operations"]["months"]["2026-08"]["model_calls"] == 0
    assert state["accounts"]["strategy"]["marks"][-1]["at"] == events[0]["at"]
    assert summary(state)["comparable"] is False


def test_deterministic_selection_does_not_enable_ai_or_change_policy(store, config, events):
    original = deepcopy(config)
    result = run_decision(store, config, events[0])
    assert result["status"] == "completed"
    assert result["selection"]["strategy"] == config["strategy"]
    assert result["selection"]["source"] == "deterministic"
    assert config == original == store.read()["config"]
    assert summary(store.read())["allocation_choices"] == []


def test_valid_model_choice_affects_only_strategy_and_preserves_comparators(store, config, events):
    config["operations"]["max_model_calls"] = 1
    event = deepcopy(events[0])
    event["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "1"
    original = deepcopy(config)

    def provider(prompt, tokens):
        pending = store.read()
        assert pending["operations"]["allocations"][0]["status"] == "reserved"
        assert pending["operations"]["months"]["2026-08"]["model_calls"] == 1
        assert pending["events"] == {}
        context = json.loads(prompt)
        assert set(context["features"]) == {"equity", "bond"}
        assert "2026" not in prompt and "5000" not in prompt
        assert tokens == config["operations"]["max_output_tokens"]
        return '{"strategy":"passive"}'

    result = run_decision(store, config, event, **model_options(provider))
    assert result["status"] == "completed"
    state = store.read()
    assert (
        money(
            state["accounts"]["strategy"]["portfolio"]["positions"]["SIM_GLOBAL_EQUITY"]["quantity"]
        )
        > 0
    )
    assert "SIM_GLOBAL_EQUITY" not in state["accounts"]["without_ai"]["portfolio"]["positions"]
    assert state["config"] == original == config
    report = summary(state)
    assert report["allocation_choices"][0]["source"] == "scripted_fixture"
    assert report["accounts"]["strategy"]["operations_eur"] == "0.03"
    assert report["accounts"]["without_ai"]["operations_eur"] == "0.01"
    assert "scripted_fixture" in markdown(state)


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"strategy":"accept_quote"}',
        '{"strategy":"passive","max_drawdown":"0.99"}',
        '{"strategy":"passive","sizing_policy":"bounded"}',
        '{"strategy":["passive"]}',
        '{"strategy":"passive","strategy":"slow_trend"}',
        "x" * 401,
        None,
    ],
)
def test_invalid_model_output_cannot_trade_or_modify_limits(store, config, events, response):
    config["operations"]["max_model_calls"] = 1
    original = deepcopy(config)
    result = run_decision(store, config, events[0], **model_options(lambda *_: response))
    assert result["selection"]["status"] == "failed"
    assert result["status"] == "halted" and result["orders"] == 0
    state = store.read()
    assert state["config"] == original == config
    assert state["operations"]["months"]["2026-08"]["inference_spend_eur"] == "0.02"
    assert all(not a["broker"]["operations"] for a in state["accounts"].values())


def test_failed_inference_still_marks_unsold_losses(store, config, events):
    config["operations"]["max_model_calls"] = 1
    run_event(store, config, events[0])
    before = deepcopy(store.read()["accounts"]["strategy"]["portfolio"])
    event = deepcopy(events[1])
    event["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "10"
    result = run_decision(store, config, event, **model_options(lambda *_: "invalid"))
    state = store.read()
    assert result["orders"] == 0 and result["status"] == "halted"
    assert state["accounts"]["strategy"]["portfolio"] == before
    report = summary(state)
    assert money(report["accounts"]["strategy"]["max_drawdown_percent"]) > 50
    assert report["as_of"] == event["market"]["as_of"]
    assert report["comparable"] is False


def test_replay_calls_model_once_and_does_not_repeat_fees_or_costs(store, config, events):
    config["operations"]["max_model_calls"] = 1
    calls = []

    def provider(*_):
        calls.append(1)
        return '{"strategy":"slow_trend"}'

    first = run_decision(store, config, events[0], **model_options(provider))
    before = store.read()
    replay = run_decision(store, config, events[0], **model_options(provider))
    assert first["status"] == "completed" and replay["duplicate"] is True
    assert calls == [1]
    assert store.read() == before


def test_same_id_cannot_change_market_or_provider(store, config, events):
    config["operations"]["max_model_calls"] = 1

    def provider(*_):
        return '{"strategy":"passive"}'

    run_decision(store, config, events[0], **model_options(provider))
    different = deepcopy(events[0])
    different["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "101"
    with pytest.raises(SafetyError, match="allocation_event_context_conflict"):
        run_decision(store, config, different, **model_options(provider))
    with pytest.raises(SafetyError, match="allocation_event_context_conflict"):
        run_decision(
            store, config, events[0], **model_options(provider, provider_name="other-model")
        )


def test_replaying_preexisting_baseline_event_never_retrofits_ai(store, config, events):
    config["operations"]["max_model_calls"] = 1
    run_event(store, config, events[0])
    result = run_decision(
        store,
        config,
        events[0],
        **model_options(lambda *_: pytest.fail("No retroactive inference")),
    )
    assert result["duplicate"] and result["selection"]["status"] == "event_already_processed"


def test_unrelated_event_in_same_month_cannot_reserve_another_allocation(store, config, events):
    config["operations"]["max_model_calls"] = 2

    def provider(*_):
        return '{"strategy":"passive"}'

    select_strategy(store, config, events[0], **model_options(provider))
    other = deepcopy(events[0])
    other["id"] = "same-month-different-id"
    receipt = select_strategy(
        store, config, other, **model_options(lambda *_: pytest.fail("One selection per month"))
    )
    assert (
        receipt["status"] == "blocked"
        and receipt["failure_code"] == "monthly_allocation_already_reserved"
    )


def test_cost_and_input_caps_block_before_external_call(store, config, events):
    config["operations"].update(max_model_calls=1, monthly_budget_eur="0.02")
    result = run_decision(
        store, config, events[0], **model_options(lambda *_: pytest.fail("Budget"))
    )
    assert result["selection"]["failure_code"] == "model_cost_budget_exhausted"


def test_input_cap_prevents_external_call(store, config, events):
    config["operations"].update(max_model_calls=1, max_input_tokens=10)
    receipt = select_strategy(
        store, config, events[0], **model_options(lambda *_: pytest.fail("Input cap"))
    )
    assert receipt["failure_code"] == "model_input_budget_exhausted"
    assert store.read()["operations"]["months"]["2026-08"]["model_calls"] == 0


def test_exhausted_event_budget_prevents_unusable_inference(store, config, events):
    config["operations"].update(max_model_calls=1, max_events_per_month=0)
    receipt = select_strategy(
        store, config, events[0], **model_options(lambda *_: pytest.fail("Event budget"))
    )
    assert receipt["failure_code"] == "event_budget_exhausted"
    assert store.read()["operations"]["months"]["2026-08"]["model_calls"] == 0


def test_malformed_market_is_rejected_before_reservation_or_call(store, config):
    with pytest.raises(SafetyError):
        select_strategy(
            store, config, {"market": None}, **model_options(lambda *_: pytest.fail("Invalid"))
        )
    with pytest.raises(SafetyError, match="state_not_initialized"):
        store.read()


def test_interrupted_inference_is_not_retried_and_requires_explicit_abandonment(
    store, config, events
):
    config["operations"]["max_model_calls"] = 1
    calls = []

    def interrupted(*_):
        calls.append(1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        select_strategy(store, config, events[0], **model_options(interrupted))
    assert summary(store.read())["pending_allocations"] == 1
    replay = run_decision(store, config, events[0], **model_options(interrupted))
    assert replay["status"] == "awaiting_allocation_result" and calls == [1]
    abandon_selection(store, config, events[0]["id"])
    result = run_decision(store, config, events[0], **model_options(interrupted))
    assert result["status"] == "halted" and result["orders"] == 0 and calls == [1]


def test_receipt_cannot_be_used_for_a_different_event(store, config, events):
    receipt = select_strategy(store, config, events[0])
    different = deepcopy(events[0])
    different["id"] = "different-event"
    result = run_event(store, config, different, allocation_id=receipt["id"])
    assert result["status"] == "halted" and result["orders"] == 0


def test_bedrock_choice_has_no_tools_and_uses_explicit_model_and_output_cap():
    class Client:
        def converse(self, **kwargs):
            assert kwargs["modelId"] == "explicit-approved-model"
            assert "toolConfig" not in kwargs and kwargs["inferenceConfig"]["maxTokens"] == 100
            return {"output": {"message": {"content": [{"text": '{"strategy":"passive"}'}]}}}

    assert (
        BedrockAllocation(Client(), "explicit-approved-model")("bounded input", 100)
        == '{"strategy":"passive"}'
    )
