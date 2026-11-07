from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest
from etf_autopilot.forward import reconcile_forward, run_forward_cycle
from etf_autopilot.forward_control import OwnedStore, reserve_capture, review_predecision_code
from etf_autopilot.forward_selection import run_forward_decision
from etf_autopilot.forward_transfer import (
    abort_transfer,
    complete_transfer,
    freeze_transfer,
    install_transfer,
)
from etf_autopilot.store import LocalStore

from .test_forward import append, at, commit, later
from .test_forward import captured as captured
from .test_forward import daily as daily
from .test_forward import exports as exports
from .test_forward import plan as plan
from .test_forward_selection import bear as bear
from .test_forward_selection import choose


@pytest.fixture
def remote(tmp_path):
    return LocalStore(tmp_path / "remote.sqlite3")


def freeze(store, config):
    return freeze_transfer(
        store, config, digest(store.read()), "aws", now=at("2026-01-08T10:00:00Z")
    )


def test_cloud_cannot_initialize_a_second_budget_ledger(remote, config, daily):
    with pytest.raises(SafetyError, match="forward_cloud_requires_existing_trial"):
        run_forward_cycle(OwnedStore(remote, "aws"), config, daily, now=at("2026-01-08T09:00:00Z"))
    with pytest.raises(SafetyError, match="state_not_initialized"):
        remote.read()


def test_frozen_controller_blocks_replay_reconcile_and_provider_calls(store, config, daily):
    commit(store, config, daily)
    frozen = freeze(store, config)
    before = store.read()
    calls = []
    with pytest.raises(SafetyError, match="forward_controller_fenced"):
        run_forward_decision(
            store,
            config,
            later(daily),
            provider=lambda *_: calls.append(1),
            provider_name="fixture",
            reserved_cost_eur="0.02",
            clock=lambda: at("2026-01-10T09:00:00Z"),
        )
    with pytest.raises(SafetyError, match="forward_controller_fenced"):
        reconcile_forward(store)
    with pytest.raises(SafetyError, match="forward_controller_fenced"):
        run_forward_cycle(store, config, daily, now=at("2026-01-08T11:00:00Z"))
    assert store.read() == before == frozen and not calls


def test_transfer_preserves_model_reservation_then_executes_only_at_destination(
    store, remote, config, bear
):
    calls = []
    choose(store, config, bear, lambda *_: calls.append(1) or '{"strategy":"passive"}')
    frozen = freeze(store, config)
    install_transfer(remote, config, frozen, now=at("2026-01-08T10:01:00Z"))
    assert remote.read()["accounts"] == frozen["accounts"]
    assert remote.read()["operations"] == frozen["operations"]
    result = run_forward_cycle(
        OwnedStore(remote, "aws"), config, later(bear), now=at("2026-01-10T09:00:00Z")
    )
    assert result["status"] == "executed" and calls == [1]
    complete_transfer(store, remote, config, frozen, now=at("2026-01-10T10:00:00Z"))
    assert store.read()["control"]["status"] == "moved"
    assert remote.read()["operations"]["months"]["2026-01"]["model_calls"] == 1
    assert remote.read()["operations"]["months"]["2026-01"]["spend_eur"] == "0.03"


class LostAcknowledgement:
    def __init__(self, store):
        self.store = store

    def update(self, operation):
        self.store.update(operation)
        raise SafetyError("state_write_unknown_replay_same_event")


def test_unknown_install_never_unfreezes_source_and_replay_does_not_reset_destination(
    store, remote, config, daily
):
    commit(store, config, daily)
    frozen = freeze(store, config)
    with pytest.raises(SafetyError, match="state_write_unknown"):
        install_transfer(
            LostAcknowledgement(remote), config, frozen, now=at("2026-01-08T10:01:00Z")
        )
    assert store.read() == frozen
    before = remote.read()
    assert install_transfer(remote, config, frozen, now=at("2026-01-08T10:02:00Z"))["duplicate"]
    assert remote.read() == before
    complete_transfer(store, remote, config, frozen, now=at("2026-01-08T10:03:00Z"))


def test_abort_fences_late_install_and_retains_spent_capture_budget(store, remote, config, daily):
    commit(store, config, daily)
    reserve_capture(
        store, config, "2026-01-08", "context", cost_eur="0.002", now=at("2026-01-08T09:30:00Z")
    )
    frozen = freeze(store, config)
    operations = deepcopy(frozen["operations"])
    abort_transfer(store, remote, config, frozen, now=at("2026-01-08T10:01:00Z"))
    assert store.read()["control"]["status"] == "active"
    assert store.read()["operations"] == operations
    with pytest.raises(SafetyError, match="destination_not_inactive"):
        install_transfer(remote, config, frozen, now=at("2026-01-08T10:02:00Z"))
    assert abort_transfer(store, remote, config, frozen, now=at("2026-01-08T10:03:00Z"))[
        "duplicate"
    ]
    next_frozen = freeze_transfer(
        store, config, digest(store.read()), "aws", now=at("2026-01-08T10:04:00Z")
    )
    install_transfer(remote, config, next_frozen, now=at("2026-01-08T10:05:00Z"))
    assert remote.read()["operations"] == operations


def test_unknown_abort_leaves_source_frozen_until_destination_fence_is_confirmed(
    store, remote, config, daily
):
    commit(store, config, daily)
    frozen = freeze(store, config)
    with pytest.raises(SafetyError, match="state_write_unknown"):
        abort_transfer(
            store, LostAcknowledgement(remote), config, frozen, now=at("2026-01-08T10:01:00Z")
        )
    assert store.read() == frozen
    abort_transfer(store, remote, config, frozen, now=at("2026-01-08T10:02:00Z"))
    assert store.read()["control"]["status"] == "active"


def test_racing_install_and_abort_cannot_activate_both_controllers(store, remote, config, daily):
    commit(store, config, daily)
    frozen = freeze(store, config)

    def attempt(function):
        try:
            function()
            return "done"
        except SafetyError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                attempt,
                [
                    lambda: install_transfer(
                        remote, config, frozen, now=at("2026-01-08T10:01:00Z")
                    ),
                    lambda: abort_transfer(
                        store, remote, config, frozen, now=at("2026-01-08T10:01:00Z")
                    ),
                ],
            )
        )
    assert sorted(results) == ["blocked", "done"]
    local_active = store.read()["control"]["status"] == "active"
    remote_active = remote.read().get("control", {}).get("status") == "active"
    assert local_active != remote_active


def test_return_to_local_carries_all_fills_and_budget_back(store, remote, config, daily):
    commit(store, config, daily)
    frozen = freeze(store, config)
    install_transfer(remote, config, frozen, now=at("2026-01-08T10:01:00Z"))
    complete_transfer(store, remote, config, frozen, now=at("2026-01-08T10:02:00Z"))
    run_forward_cycle(
        OwnedStore(remote, "aws"), config, later(daily), now=at("2026-01-10T09:00:00Z")
    )
    returning = freeze_transfer(
        remote, config, digest(remote.read()), "local", now=at("2026-01-10T10:00:00Z")
    )
    install_transfer(store, config, returning, now=at("2026-01-10T10:01:00Z"))
    complete_transfer(remote, store, config, returning, now=at("2026-01-10T10:02:00Z"))
    assert store.read()["accounts"] == returning["accounts"]
    assert store.read()["operations"] == returning["operations"]
    assert store.read()["control"]["owner"] == "local" and store.read()["control"]["epoch"] == 3
    following = append(later(daily), "2026-01-12", "2026-01-13T08:00:00Z")
    assert (
        run_forward_cycle(store, config, following, now=at("2026-01-13T09:00:00Z"))["orders"] == 0
    )


def test_modified_transfer_snapshot_and_active_destination_cannot_overwrite_a_ledger(
    store, remote, config, daily
):
    commit(store, config, daily)
    frozen = freeze(store, config)
    tampered = deepcopy(frozen)
    tampered["accounts"]["strategy"]["portfolio"]["cash"] = "9000"
    with pytest.raises(SafetyError, match="snapshot_changed"):
        install_transfer(remote, config, tampered, now=at("2026-01-08T10:01:00Z"))
    run_forward_cycle(remote, config, daily, now=at("2026-01-08T09:00:00Z"))
    before = remote.read()
    with pytest.raises(SafetyError, match="destination_not_inactive"):
        install_transfer(remote, config, frozen, now=at("2026-01-08T10:01:00Z"))
    assert remote.read() == before


def test_unresolved_model_attempt_must_be_closed_before_control_can_move(store, config, bear):
    run_forward_cycle(
        store,
        config,
        bear,
        now=at("2026-01-08T09:00:00Z"),
        selection={"provider_name": "fixture", "reserved_cost_eur": "0.02"},
    )
    before = store.read()
    with pytest.raises(SafetyError, match="resolved_outcomes"):
        freeze(store, config)
    assert store.read() == before


def legacy(store, config, daily):
    run_forward_cycle(store, config, daily, now=at("2026-01-12T09:00:00Z"))

    def change(state):
        state.pop("control")
        state["engine_fingerprint"] = "a" * 64
        return None, state

    store.update(change)
    return store.read()


def test_explicit_predecision_review_preserves_history_without_resetting_a_trial(
    store, config, daily
):
    before = legacy(store, config, daily)
    report = review_predecision_code(
        store, config, digest(before), "a" * 64, now=at("2026-01-12T10:00:00Z")
    )
    assert not report["duplicate"]
    after = store.read()
    for key in ("accounts", "operations", "events", "observed_prefix_hash", "trial_started_at"):
        assert before[key] == after[key]
    assert review_predecision_code(
        store, config, digest(before), "a" * 64, now=at("2026-01-12T11:00:00Z")
    )["duplicate"]
    assert store.read() == after


def test_predecision_review_refuses_prior_spend_or_conflicting_snapshot(store, config, daily):
    before = legacy(store, config, daily)
    with pytest.raises(SafetyError, match="review_context_conflict"):
        review_predecision_code(store, config, "b" * 64, "a" * 64, now=at("2026-01-12T10:00:00Z"))

    def spent(state):
        state["operations"]["months"]["2026-01"]["spend_eur"] = "0.01"
        return None, state

    store.update(spent)
    before = store.read()
    with pytest.raises(SafetyError, match="cannot_reset_prior_spend"):
        review_predecision_code(
            store, config, digest(before), "a" * 64, now=at("2026-01-12T10:00:00Z")
        )
    assert store.read() == before


def test_capture_budget_is_reserved_once_before_any_external_attempt(store, config, daily):
    commit(store, config, daily)
    first = reserve_capture(
        store, config, "2026-01-08", "ctx", cost_eur="0.002", now=at("2026-01-08T09:30:00Z")
    )
    before = store.read()
    assert not first["duplicate"]
    assert reserve_capture(
        store, config, "2026-01-08", "ctx", cost_eur="0.002", now=at("2026-01-08T09:40:00Z")
    )["duplicate"]
    assert store.read() == before
    with pytest.raises(SafetyError, match="capture_context_conflict"):
        reserve_capture(
            store, config, "2026-01-08", "changed", cost_eur="0.002", now=at("2026-01-08T09:40:00Z")
        )
    assert before["operations"]["months"]["2026-01"]["spend_eur"] == "0.012"
