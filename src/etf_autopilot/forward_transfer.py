"""Conditional controller handoff; unknown uploads keep the source fenced."""

from copy import deepcopy
from datetime import UTC, datetime

from .domain import SafetyError, canonical, digest, instant
from .forward_control import MODE, OWNERS, assert_owner, check_config, trial_identity
from .paper import reconcile
from .store import MAX_STATE_BYTES


def _at(now):
    return instant((now or datetime.now(UTC)).isoformat())


def _transfer_id(state, source_hash, target, epoch):
    return digest(
        {
            "trial": trial_identity(state),
            "source_state_hash": source_hash,
            "target": target,
            "epoch": epoch,
        }
    )


def freeze_transfer(store, base, expected_state_hash, target, *, now=None):
    at = _at(now)

    def transition(state):
        if state is None:
            raise SafetyError("forward_trial_required")
        check_config(state, base)
        control = state["control"]
        prior = control.get("transfer")
        if (
            control["status"] == "frozen"
            and prior
            and (prior["source_state_hash"] == expected_state_hash and prior["to"] == target)
        ):
            return deepcopy(state), state
        assert_owner(state, control["owner"])
        if target not in OWNERS or target == control["owner"]:
            raise SafetyError("different_forward_controller_required")
        if digest(state) != expected_state_hash:
            raise SafetyError("forward_transfer_source_changed")
        if len(canonical(state).encode()) > MAX_STATE_BYTES - 20_000:
            raise SafetyError("forward_transfer_archive_required")
        if at < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_clock")
        if state["halt"] or any(row["status"] == "reserved" for row in state["decisions"].values()):
            raise SafetyError("forward_transfer_requires_resolved_outcomes")
        for account in state["accounts"].values():
            reconcile(account, base["starting_cash"])
        record = {
            "from": control["owner"],
            "to": target,
            "at": at.isoformat(),
            "source_state_hash": expected_state_hash,
            "source_runtime_at": state["last_runtime_at"],
            "epoch": control["epoch"] + 1,
        }
        record["id"] = _transfer_id(state, expected_state_hash, target, record["epoch"])
        control.update(status="frozen", transfer=record)
        state["last_runtime_at"] = at.isoformat()
        state["audit"].append({"type": "forward_controller_frozen", **record})
        return deepcopy(state), state

    return store.update(transition)


def _validate_frozen(state, base):
    check_config(state, base)
    control = state["control"]
    record = control.get("transfer")
    if (
        control["status"] != "frozen"
        or record is None
        or record["from"] != control["owner"]
        or record["to"] not in OWNERS
        or record["to"] == record["from"]
        or record["epoch"] != control["epoch"] + 1
        or record["id"]
        != _transfer_id(state, record["source_state_hash"], record["to"], record["epoch"])
    ):
        raise SafetyError("forward_transfer_snapshot_required")
    before = deepcopy(state)
    if before["audit"][-1] != {"type": "forward_controller_frozen", **record}:
        raise SafetyError("forward_transfer_snapshot_changed")
    before["audit"].pop()
    before["control"].update(status="active", transfer=None)
    before["last_runtime_at"] = record["source_runtime_at"]
    if digest(before) != record["source_state_hash"]:
        raise SafetyError("forward_transfer_snapshot_changed")
    return record


def _proof(state, frozen):
    if (
        state is None
        or state.get("mode") != MODE
        or trial_identity(state) != trial_identity(frozen)
    ):
        return None
    record = frozen["control"]["transfer"]
    return next(
        (
            item
            for item in state["control"]["lineage"]
            if item["id"] == record["id"] and item["source_frozen_hash"] == digest(frozen)
        ),
        None,
    )


def _inactive_destination(existing, frozen):
    if existing is None:
        return True
    record = frozen["control"]["transfer"]
    if existing.get("mode") == "forward_transfer_tombstone":
        return (
            existing["trial_id"] == trial_identity(frozen)
            and record["id"] not in existing["blocked_transfers"]
            and set(existing["blocked_transfers"]) <= set(frozen["control"]["blocked_transfers"])
        )
    if existing.get("mode") != MODE or trial_identity(existing) != trial_identity(frozen):
        return False
    control = existing["control"]
    previous = control.get("transfer")
    return (
        control["status"] == "moved"
        and control["owner"] == record["to"]
        and control["epoch"] < frozen["control"]["epoch"]
        and record["id"] not in control["blocked_transfers"]
        and previous is not None
        and any(item["id"] == previous["id"] for item in frozen["control"]["lineage"])
    )


def install_transfer(destination, base, frozen, *, now=None):
    frozen = deepcopy(frozen)
    record = _validate_frozen(frozen, base)
    at = _at(now)
    if at < instant(frozen["last_runtime_at"]):
        raise SafetyError("forward_nonmonotonic_clock")

    def transition(existing):
        if _proof(existing, frozen):
            check_config(existing, base)
            return {"status": "installed", "duplicate": True, "id": record["id"]}, existing
        if not _inactive_destination(existing, frozen):
            raise SafetyError("forward_transfer_destination_not_inactive")
        updated = deepcopy(frozen)
        control = updated["control"]
        control.update(owner=record["to"], status="active", epoch=record["epoch"], transfer=None)
        control["lineage"].append(
            {**record, "source_frozen_hash": digest(frozen), "installed_at": at.isoformat()}
        )
        updated["last_runtime_at"] = at.isoformat()
        updated["audit"].append(
            {
                "type": "forward_controller_adopted",
                "id": record["id"],
                "at": at.isoformat(),
                "owner": record["to"],
            }
        )
        return {"status": "installed", "duplicate": False, "id": record["id"]}, updated

    return destination.update(transition)


def complete_transfer(source, destination, base, frozen, *, now=None):
    record = _validate_frozen(frozen, base)
    installed = destination.read()
    check_config(installed, base)
    if not _proof(installed, frozen):
        raise SafetyError("forward_transfer_destination_proof_required")
    at = _at(now)

    def transition(state):
        check_config(state, base)
        control = state["control"]
        if control["status"] == "moved" and control["transfer"] == record:
            return {"status": "moved", "duplicate": True, "id": record["id"]}, state
        if digest(state) != digest(frozen):
            raise SafetyError("forward_transfer_source_changed")
        if at < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_clock")
        control["status"] = "moved"
        state["last_runtime_at"] = at.isoformat()
        state["audit"].append(
            {
                "type": "forward_controller_released",
                "at": at.isoformat(),
                "id": record["id"],
                "destination_proof": digest(_proof(installed, frozen)),
            }
        )
        return {"status": "moved", "duplicate": False, "id": record["id"]}, state

    return source.update(transition)


def abort_transfer(source, destination, base, frozen, *, now=None):
    """Fence the destination first, so an unknown/late install cannot race an abort."""
    record = _validate_frozen(frozen, base)
    at = _at(now)

    def fence(existing):
        if _proof(existing, frozen):
            raise SafetyError("forward_transfer_already_installed")
        blocked = (
            existing.get("blocked_transfers", [])
            if existing and existing.get("mode") == "forward_transfer_tombstone"
            else existing.get("control", {}).get("blocked_transfers", [])
            if existing
            else []
        )
        if record["id"] in blocked:
            return None, existing
        if not _inactive_destination(existing, frozen):
            raise SafetyError("forward_transfer_destination_not_inactive")
        if existing and existing.get("mode") == MODE:
            existing["control"]["blocked_transfers"].append(record["id"])
            existing["audit"].append(
                {"type": "forward_transfer_blocked", "id": record["id"], "at": at.isoformat()}
            )
            return None, existing
        return None, {
            "mode": "forward_transfer_tombstone",
            "trial_id": trial_identity(frozen),
            "blocked_transfers": [*blocked, record["id"]],
            "at": at.isoformat(),
        }

    destination.update(fence)

    def restore(state):
        check_config(state, base)
        if (
            state["control"]["status"] == "active"
            and record["id"] in state["control"]["blocked_transfers"]
        ):
            return {"status": "aborted", "duplicate": True}, state
        if digest(state) != digest(frozen):
            raise SafetyError("forward_transfer_source_changed")
        if at < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_clock")
        state["control"].update(status="active", transfer=None)
        state["control"]["blocked_transfers"].append(record["id"])
        state["last_runtime_at"] = at.isoformat()
        state["audit"].append(
            {"type": "forward_transfer_aborted", "id": record["id"], "at": at.isoformat()}
        )
        return {"status": "aborted", "duplicate": False}, state

    return source.update(restore)
