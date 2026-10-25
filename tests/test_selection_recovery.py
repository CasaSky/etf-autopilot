from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError
from etf_autopilot.engine import run_event
from etf_autopilot.selection import run_decision, select_strategy

OPTIONS = {"provider_name": "scripted_fixture", "reserved_cost_eur": "0.02"}


def test_model_call_is_outside_storage_transaction(store, config, events):
    config["operations"]["max_model_calls"] = 1

    class GuardedStore:
        active = False

        def update(self, transition):
            self.active = True
            try:
                return store.update(transition)
            finally:
                self.active = False

    guarded = GuardedStore()

    def provider(*_):
        assert not guarded.active
        assert store.read()["operations"]["allocations"][0]["status"] == "reserved"
        return '{"strategy":"passive"}'

    assert (
        run_decision(guarded, config, events[0], provider=provider, **OPTIONS)["status"]
        == "completed"
    )


def test_unknown_reservation_write_never_retries_inference(config, events):
    config["operations"]["max_model_calls"] = 1

    class UnknownWriteStore:
        state = None
        unknown = True

        def update(self, transition):
            result, state = transition(deepcopy(self.state))
            self.state = state
            if self.unknown:
                self.unknown = False
                raise SafetyError("state_write_unknown_replay_same_event")
            return result

    store = UnknownWriteStore()

    def provider(*_):
        pytest.fail("Unknown reservation must not invoke a model")

    with pytest.raises(SafetyError, match="state_write_unknown"):
        select_strategy(store, config, events[0], provider=provider, **OPTIONS)
    replay = run_decision(store, config, events[0], provider=provider, **OPTIONS)
    assert replay["status"] == "awaiting_allocation_result"
    assert store.state["operations"]["months"]["2026-08"]["model_calls"] == 1


def test_concurrent_duplicate_cannot_steal_or_repeat_inflight_decision(store, config, events):
    config["operations"]["max_model_calls"] = 1
    calls = []

    def provider(*_):
        calls.append(1)
        second = run_decision(store, config, events[0], provider=provider, **OPTIONS)
        assert second["status"] == "awaiting_allocation_result"
        assert store.read()["events"] == {}
        return '{"strategy":"slow_trend"}'

    assert (
        run_decision(store, config, events[0], provider=provider, **OPTIONS)["status"]
        == "completed"
    )
    assert calls == [1]


def test_stored_choice_mutation_cannot_change_a_prepared_decision(store, config, events):
    config["operations"]["max_model_calls"] = 1
    receipt = select_strategy(
        store, config, events[0], provider=lambda *_: '{"strategy":"passive"}', **OPTIONS
    )

    def corrupt(state):
        state["operations"]["allocations"][0]["strategy"] = "slow_trend"
        return None, state

    store.update(corrupt)
    result = run_event(store, config, events[0], allocation_id=receipt["id"])
    assert result["status"] == "halted" and result["orders"] == 0
    assert result["diagnosis"] == "allocation_choice_hash_mismatch"


def test_baseline_event_completed_during_call_cannot_be_reallocated(store, config, events):
    config["operations"]["max_model_calls"] = 1

    def provider(*_):
        assert run_event(store, config, events[0])["status"] == "completed"
        return '{"strategy":"passive"}'

    receipt = select_strategy(store, config, events[0], provider=provider, **OPTIONS)
    assert receipt["status"] == "superseded"
    state = store.read()
    assert "allocation_id" not in state["events"][events[0]["id"]]
    with pytest.raises(SafetyError, match="event_id_payload_conflict"):
        run_event(store, config, events[0], allocation_id=receipt["id"])
