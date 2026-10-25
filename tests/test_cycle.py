from copy import deepcopy
from datetime import timedelta

import pytest

from etf_autopilot.cycle import run_cycle
from etf_autopilot.domain import SafetyError, instant
from etf_autopilot.engine import run_event
from etf_autopilot.selection import run_decision, select_strategy


def test_repeated_ticks_reuse_choice_orders_fees_and_state(store, config, events):
    config["operations"]["max_model_calls"] = 1
    event = deepcopy(events[0])
    event["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "1"
    calls = []

    def scripted(*_):
        calls.append(1)
        return '{"strategy":"passive"}'

    options = {
        "provider": scripted,
        "provider_name": "scripted_fixture",
        "reserved_cost_eur": "0.02",
    }
    now = instant(event["at"]) + timedelta(minutes=1)
    first = run_cycle(store, config, event, now=now, **options)
    state = store.read()
    repeated = run_cycle(store, config, event, now=now + timedelta(hours=1), **options)
    assert first["status"] == "completed" and repeated["decision"]["duplicate"]
    assert calls == [1] and store.read() == state
    assert repeated["report"]["as_of"] == events[0]["market"]["as_of"]


def test_equivalent_allocations_need_no_model_call_or_expense_even_with_zero_call_budget(
    store, config, events
):
    options = {
        "provider": lambda *_: pytest.fail("Equivalent weights need no inference"),
        "provider_name": "scripted_fixture",
        "reserved_cost_eur": "0.02",
    }
    result = run_cycle(store, config, events[0], now=instant(events[0]["at"]), **options)
    assert result["status"] == "completed"
    assert result["inference_skipped"] == "equivalent_candidate_allocations"
    state = store.read()
    assert state["operations"]["months"]["2026-08"]["model_calls"] == 0
    assert state["operations"]["months"]["2026-08"]["inference_spend_eur"] == "0"
    assert result["decision"]["selection"]["source"] == "deterministic"
    assert result["report"]["allocation_choices"] == []
    replay = run_cycle(store, config, events[0], now=instant(events[0]["at"]), **options)
    assert replay["decision"]["duplicate"] and store.read() == state


def test_equivalence_does_not_replace_a_recorded_model_choice(store, config, events):
    config["operations"]["max_model_calls"] = 1
    options = {"provider_name": "scripted_fixture", "reserved_cost_eur": "0.02"}
    run_decision(store, config, events[0], provider=lambda *_: '{"strategy":"passive"}', **options)
    before = store.read()
    result = run_cycle(
        store,
        config,
        events[0],
        now=instant(events[0]["at"]),
        provider=lambda *_: pytest.fail("Recorded choice cannot call again"),
        **options,
    )
    assert result["decision"]["duplicate"] and store.read() == before
    assert "inference_skipped" not in result


def test_equivalence_never_turns_an_unknown_call_into_a_deterministic_choice(store, config, events):
    config["operations"]["max_model_calls"] = 1
    options = {"provider_name": "scripted_fixture", "reserved_cost_eur": "0.02"}
    calls = []

    def unknown(*_):
        calls.append(1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        select_strategy(store, config, events[0], provider=unknown, **options)
    result = run_cycle(
        store, config, events[0], now=instant(events[0]["at"]), provider=unknown, **options
    )
    assert result["status"] == "awaiting_allocation_result" and calls == [1]
    assert store.read()["events"] == {}
    assert "inference_skipped" not in result


def test_concurrent_reservation_during_equivalence_preview_still_fails_closed(
    store, config, events
):
    config["operations"]["max_model_calls"] = 1
    options = {"provider_name": "scripted_fixture", "reserved_cost_eur": "0.02"}
    calls = []

    def interrupted(*_):
        calls.append(1)
        raise KeyboardInterrupt

    class RacingStore:
        first = True

        def read(self):
            if self.first:
                self.first = False
                with pytest.raises(KeyboardInterrupt):
                    select_strategy(store, config, events[0], provider=interrupted, **options)
                raise SafetyError("state_not_initialized")
            return store.read()

        def update(self, transition):
            return store.update(transition)

    raced = RacingStore()
    with pytest.raises(SafetyError, match="allocation_event_context_conflict"):
        run_cycle(
            raced, config, events[0], now=instant(events[0]["at"]), provider=interrupted, **options
        )
    resumed = run_cycle(
        raced, config, events[0], now=instant(events[0]["at"]), provider=interrupted, **options
    )
    assert resumed["status"] == "awaiting_allocation_result" and calls == [1]
    assert store.read()["events"] == {}


@pytest.mark.parametrize("elapsed", [timedelta(days=2), timedelta(days=32), -timedelta(minutes=1)])
def test_scheduler_cannot_replay_stale_or_future_input_as_fresh(store, config, events, elapsed):
    result = run_cycle(
        store,
        config,
        events[0],
        now=instant(events[0]["at"]) + elapsed,
        provider=lambda *_: pytest.fail("No call for stale or future input"),
    )
    assert result["status"] == "waiting_for_input" and result["orders"] == 0
    with pytest.raises(SafetyError, match="state_not_initialized"):
        store.read()


def test_cycle_never_automatically_clears_halt(store, config, events):
    run_event(store, config, events[0], fault="unknown")
    before = store.read()
    result = run_cycle(store, config, events[1], now=instant(events[1]["at"]))
    assert result["status"] == "halted"
    assert result["report"]["halt"] == before["halt"]
    assert result["decision"]["orders"] == 0
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == 1


def test_changed_event_cannot_bypass_monthly_limit(store, config, events):
    run_cycle(store, config, events[0], now=instant(events[0]["at"]))
    another = deepcopy(events[0])
    another["id"] = "another-tick"
    another["at"] = "2026-08-23T11:00:00Z"
    result = run_cycle(store, config, another, now=instant(another["at"]))
    assert result["decision"]["status"] == "skipped_monthly_cadence"
    assert result["decision"]["orders"] == 0
