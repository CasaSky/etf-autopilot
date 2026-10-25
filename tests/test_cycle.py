from copy import deepcopy
from datetime import timedelta

import pytest

from etf_autopilot.cycle import run_cycle
from etf_autopilot.domain import SafetyError, instant
from etf_autopilot.engine import run_event


def test_repeated_ticks_reuse_choice_orders_fees_and_state(store, config, events):
    config["operations"]["max_model_calls"] = 1
    calls = []

    def scripted(*_):
        calls.append(1)
        return '{"strategy":"passive"}'

    options = {
        "provider": scripted,
        "provider_name": "scripted_fixture",
        "reserved_cost_eur": "0.02",
    }
    now = instant(events[0]["at"]) + timedelta(minutes=1)
    first = run_cycle(store, config, events[0], now=now, **options)
    state = store.read()
    repeated = run_cycle(store, config, events[0], now=now + timedelta(hours=1), **options)
    assert first["status"] == "completed" and repeated["decision"]["duplicate"]
    assert calls == [1] and store.read() == state
    assert repeated["report"]["as_of"] == events[0]["market"]["as_of"]


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
