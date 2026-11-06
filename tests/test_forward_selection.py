import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest, number
from etf_autopilot.forward import forward_summary, run_forward_cycle
from etf_autopilot.forward_selection import abandon_forward_selection, run_forward_decision

from .test_forward import append, at
from .test_forward import captured as captured
from .test_forward import daily as daily
from .test_forward import exports as exports
from .test_forward import plan as plan

NOW = "2026-01-08T09:00:00Z"
DECISION = "forward-decision:2026-01"


@pytest.fixture
def bear(daily, config):
    config["operations"]["max_model_calls"] = 1
    result = deepcopy(daily)
    for row in result["rows"]:
        if row["date"] >= "2025-12-01":
            row["values"]["RESEARCH_EQUITY"] = "70"
    result["observation_id"] = digest({k: v for k, v in result.items() if k != "observation_id"})
    return result


def choose(store, config, data, provider, **options):
    return run_forward_decision(
        store,
        config,
        data,
        provider=provider,
        provider_name="fixture:bounded",
        reserved_cost_eur="0.02",
        clock=lambda: at(NOW),
        **options,
    )


def test_reserve_before_call_anonymous_prompt_and_only_strategy_changes(store, config, bear):
    calls = []

    def provider(prompt, tokens):
        state = store.read()
        assert state["decisions"][DECISION]["status"] == "reserved"
        assert state["operations"]["months"]["2026-01"]["model_calls"] == 1
        assert state["operations"]["months"]["2026-01"]["spend_eur"] == "0.03"
        assert not any(
            row["type"] == "fill"
            for account in state["accounts"].values()
            for row in account["ledger"]
        )
        context = json.loads(prompt)
        assert set(context) == {"features", "candidates", "scope"}
        assert set(context["features"]) == {"equity", "bond"}
        assert number(context["features"]["equity"]["latest_month_change"]) < 0
        assert all(value not in prompt for value in ("ISIN", "RESEARCH_", "cash", "2026-", "5000"))
        assert tokens == 400
        calls.append(prompt)
        return '{"strategy":"passive"}'

    result = choose(store, config, bear, provider)
    assert result["orders"] == 0 and result["selection_status"] == "ready"
    before = store.read()
    assert choose(store, config, bear, provider)["duplicate"]
    assert store.read() == before and len(calls) == 1
    future = append(bear, "2026-01-09", "2026-01-10T08:00:00Z")
    assert run_forward_cycle(store, config, future, now=at("2026-01-10T09:00:00Z"))["orders"] > 0
    report = forward_summary(store.read())
    assert report["comparable"] and report["model_calls"] == 1
    assert number(report["accounts"]["strategy"]["positions"]["RESEARCH_EQUITY"]["quantity"]) > 0
    assert report["accounts"]["without_ai"]["positions"].get("RESEARCH_EQUITY") is None
    assert report["accounts"]["strategy"]["operations_eur"] == "0.03"
    assert report["accounts"]["without_ai"]["operations_eur"] == "0.01"
    assert report["accounts"]["passive"]["operations_eur"] == "0.00"


def test_concurrent_replay_never_calls_twice(store, config, bear):
    calls = []

    def provider(*_):
        calls.append(1)
        return '{"strategy":"passive"}'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: choose(store, config, bear, provider), range(2)))
    assert len(calls) == 1 and sum(row["duplicate"] for row in results) == 1


def test_identical_weights_skip_model_even_with_zero_call_budget(store, config, daily):
    def forbidden(*_):
        raise AssertionError("Equivalent choices should not spend")

    result = choose(store, config, daily, forbidden)
    assert result["inference_skipped"] == "equivalent_candidate_allocations"
    assert result["status"] == "decision_committed"
    bucket = store.read()["operations"]["months"]["2026-01"]
    assert bucket["model_calls"] == 0 and bucket["spend_eur"] == "0.01"


@pytest.mark.parametrize(
    "gate,reason",
    [
        ("max_model_calls", "model_call_budget_exhausted"),
        ("monthly_budget_eur", "model_cost_budget_exhausted"),
        ("max_input_tokens", "model_input_budget_exhausted"),
        ("max_output_tokens", "model_input_budget_exhausted"),
    ],
)
def test_budget_gates_consume_month_without_call_or_inference_charge(
    store, config, bear, gate, reason
):
    config["operations"][gate] = {
        "max_model_calls": 0,
        "monthly_budget_eur": "0.02",
        "max_input_tokens": 512,
        "max_output_tokens": 0,
    }[gate]
    calls = []
    result = choose(store, config, bear, lambda *_: calls.append(1))
    assert result["status"] == "selection_blocked" and result["failure_code"] == reason
    assert not calls and store.read()["pending"] is None
    assert store.read()["slots"]["2026-01"] == DECISION
    assert store.read()["operations"]["months"]["2026-01"]["model_calls"] == 0
    assert not forward_summary(store.read())["comparable"]


@pytest.mark.parametrize(
    "response",
    [
        '{"strategy":"passive","orders":[]}',
        '{"strategy":"passive","strategy":"slow_trend"}',
        '{"strategy":"raise_risk"}',
        "x" * 401,
        None,
    ],
)
def test_invalid_output_retains_cost_month_and_positions_without_orders(
    store, config, bear, response
):
    result = choose(store, config, bear, lambda *_: response)
    assert result["selection_status"] == "selection_failed" and result["orders"] == 0
    future = append(bear, "2026-01-09", "2026-01-10T08:00:00Z")
    assert run_forward_cycle(store, config, future, now=at("2026-01-10T09:00:00Z"))["orders"] == 0
    state = store.read()
    assert state["operations"]["months"]["2026-01"]["spend_eur"] == "0.03"
    assert state["decisions"][DECISION]["failure_code"] == "provider_or_response_failure"
    assert state["last_value_date"] == "2026-01-09" and not forward_summary(state)["comparable"]


def test_crash_stays_reserved_past_deadline_until_explicit_abandon(store, config, bear):
    class ProcessStopped(BaseException):
        pass

    def stopped(*_):
        raise ProcessStopped()

    with pytest.raises(ProcessStopped):
        choose(store, config, bear, stopped)
    assert choose(store, config, bear, stopped)["duplicate"]
    future = append(bear, "2026-01-16", "2026-01-19T08:00:00Z")
    result = run_forward_cycle(store, config, future, now=at("2026-01-19T09:00:00Z"))
    assert result["status"] == "waiting_for_input" and result["orders"] == 0
    assert store.read()["decisions"][DECISION]["status"] == "reserved"
    abandon_forward_selection(store, DECISION)
    state = store.read()
    assert state["pending"] is None and state["slots"]["2026-01"] == DECISION
    assert state["operations"]["months"]["2026-01"]["spend_eur"] == "0.03"
    with pytest.raises(SafetyError, match="no_reserved_forward_selection"):
        abandon_forward_selection(store, DECISION)


def test_finalization_cannot_overwrite_operator_abandonment(store, config, bear):
    def provider(*_):
        abandon_forward_selection(store, DECISION)
        return '{"strategy":"passive"}'

    assert choose(store, config, bear, provider)["selection_status"] == "selection_abandoned"
    assert store.read()["decisions"][DECISION].get("choice_hash") is None


def test_data_received_before_choice_commit_cannot_be_used_as_fill(store, config, bear):
    seen = append(bear, "2026-01-09", "2026-01-10T08:00:00Z")
    ticks = iter([at(NOW), at("2026-01-10T10:00:00Z")])

    def provider(*_):
        result = run_forward_cycle(store, config, seen, now=at("2026-01-10T09:00:00Z"))
        assert result["status"] == "awaiting_selection" and result["orders"] == 0
        return '{"strategy":"passive"}'

    result = run_forward_decision(
        store,
        config,
        bear,
        provider=provider,
        provider_name="fixture",
        reserved_cost_eur="0.02",
        clock=lambda: next(ticks),
    )
    assert result["selection_status"] == "ready"
    assert store.read()["decisions"][DECISION]["choice_at"].startswith("2026-01-10")
    later = append(seen, "2026-01-12", "2026-01-13T08:00:00Z")
    settled = run_forward_cycle(store, config, later, now=at("2026-01-13T09:00:00Z"))
    assert settled["modeled_value_date"] == "2026-01-12"


def test_context_conflict_and_mutated_choice_fail_before_orders(store, config, bear):
    choose(store, config, bear, lambda *_: '{"strategy":"passive"}')
    with pytest.raises(SafetyError, match="forward_event_context_conflict"):
        run_forward_cycle(store, config, bear, now=at(NOW))

    def tamper(state):
        state["decisions"][DECISION]["selected_strategy"] = "slow_trend"
        return None, state

    store.update(tamper)
    future = append(bear, "2026-01-09", "2026-01-10T08:00:00Z")
    result = run_forward_cycle(store, config, future, now=at("2026-01-10T09:00:00Z"))
    assert result["status"] == "halted" and result["orders"] == 0
    assert result["diagnosis"] == "forward_committed_choice_changed"


def test_reversed_finish_clock_retains_unknown_reservation(store, config, bear):
    ticks = iter([at(NOW), at("2026-01-08T08:00:00Z")])
    with pytest.raises(SafetyError, match="forward_nonmonotonic_choice_clock"):
        run_forward_decision(
            store,
            config,
            bear,
            provider=lambda *_: '{"strategy":"passive"}',
            provider_name="fixture",
            reserved_cost_eur="0.02",
            clock=lambda: next(ticks),
        )
    assert store.read()["decisions"][DECISION]["status"] == "reserved"
    assert store.read()["operations"]["months"]["2026-01"]["spend_eur"] == "0.03"


def test_cli_zero_budget_does_not_construct_sdk_client(
    store, config, bear, tmp_path, monkeypatch, capsys
):
    from etf_autopilot import forward_selection, selection
    from etf_autopilot.cli import main

    class Clock:
        @staticmethod
        def now(tz):
            return at(NOW)

    def forbidden(*_):
        raise AssertionError("Zero call budget must not create an SDK client")

    config["operations"]["max_model_calls"] = 0
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "daily.json").write_text(json.dumps(bear))
    monkeypatch.setattr(forward_selection, "datetime", Clock)
    monkeypatch.setattr(selection, "BedrockAllocation", forbidden)
    args = [
        "--config",
        str(tmp_path / "config.json"),
        "--state",
        str(store.path),
        "forward-cycle",
        str(tmp_path / "daily.json"),
        "--allow-model-call",
        "--model-id",
        "fixture-only",
        "--region",
        "eu-central-1",
        "--reserved-cost-eur",
        "0.02",
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["failure_code"] == "model_call_budget_exhausted"
    assert store.read()["operations"]["months"]["2026-01"]["model_calls"] == 0


def test_delayed_response_cannot_extend_original_decision_deadline(store, config, bear):
    ticks = iter([at(NOW), at("2026-01-16T09:00:00Z")])
    run_forward_decision(
        store,
        config,
        bear,
        provider=lambda *_: '{"strategy":"passive"}',
        provider_name="fixture",
        reserved_cost_eur="0.02",
        clock=lambda: next(ticks),
    )
    future = append(bear, "2026-01-16", "2026-01-17T08:00:00Z")
    result = run_forward_cycle(store, config, future, now=at("2026-01-17T09:00:00Z"))
    assert result["status"] == "decision_expired" and result["orders"] == 0
