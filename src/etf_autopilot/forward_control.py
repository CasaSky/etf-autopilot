"""Fence paper controllers and reserve source costs before external capture."""

from copy import deepcopy
from datetime import UTC, datetime

from .domain import SafetyError, digest, instant, money, number, validate_config
from .paper import reconcile

MODE = "forward_research_paper"
OWNERS = frozenset({"local", "aws"})


def initial_control():
    return {
        "owner": "local",
        "status": "active",
        "epoch": 1,
        "lineage": [],
        "blocked_transfers": [],
        "transfer": None,
    }


def trial_identity(state):
    return digest({key: state[key] for key in ("trial_started_at", "config_hash", "identity_hash")})


def assert_owner(state, owner):
    control = state.get("control", initial_control())
    if state.get("mode") != MODE or owner not in OWNERS:
        raise SafetyError("forward_controller_required")
    if control["status"] != "active" or control["owner"] != owner:
        raise SafetyError("forward_controller_fenced")


class OwnedStore:
    """All financial reads/transitions require the currently active controller."""

    def __init__(self, store, owner):
        if owner not in OWNERS:
            raise SafetyError("unsupported_forward_controller")
        self.store, self.owner = store, owner

    def read(self):
        state = self.store.read()
        assert_owner(state, self.owner)
        return state

    def update(self, operation):
        def guarded(state):
            if state is None:
                if self.owner != "local":
                    raise SafetyError("forward_cloud_requires_existing_trial")
                before = initial_control()
            else:
                assert_owner(state, self.owner)
                before = deepcopy(state.get("control", initial_control()))
            result, updated = operation(state)
            if updated.get("control", initial_control()) != before:
                raise SafetyError("forward_controller_changed_during_financial_transition")
            assert_owner(updated, self.owner)
            return result, updated

        return self.store.update(guarded)


def owned_store(store):
    return store if isinstance(store, OwnedStore) else OwnedStore(store, "local")


def check_config(state, base):
    from .forward import trial_engine_fingerprint

    validate_config(base)
    if (
        state.get("mode") != MODE
        or state["base_config_hash"] != digest(base)
        or state["config_hash"] != digest(state["config"])
    ):
        raise SafetyError("forward_trial_config_changed")
    if state["engine_fingerprint"] != trial_engine_fingerprint():
        raise SafetyError("forward_trial_engine_changed_review_required")


def review_predecision_code(
    store, base, expected_state_hash, expected_engine_fingerprint, *, now=None
):
    """Explicit review before any decision/spend; preserve the source and event history."""
    from .forward import trial_engine_fingerprint

    current = trial_engine_fingerprint()
    at = instant((now or datetime.now(UTC)).isoformat())

    def transition(state):
        if state is None:
            raise SafetyError("forward_trial_required")
        assert_owner(state, "local")
        prior = next(
            (
                item
                for item in state["audit"]
                if item.get("type") == "forward_predecision_code_review"
                and item.get("before_state_hash") == expected_state_hash
                and item.get("to_engine_fingerprint") == current
            ),
            None,
        )
        if prior:
            check_config(state, base)
            return {"status": "reviewed", "duplicate": True}, state
        if (
            digest(state) != expected_state_hash
            or state["engine_fingerprint"] != expected_engine_fingerprint
        ):
            raise SafetyError("forward_review_context_conflict")
        validate_config(base)
        if state["base_config_hash"] != digest(base) or state["config_hash"] != digest(
            state["config"]
        ):
            raise SafetyError("forward_trial_config_changed")
        if state["decisions"] or state["slots"] or state["pending"] or state["halt"]:
            raise SafetyError("forward_review_requires_unstarted_financial_trial")
        for bucket in state["operations"]["months"].values():
            if number(bucket["spend_eur"]) != 0 or bucket["model_calls"] or bucket.get("captures"):
                raise SafetyError("forward_review_cannot_reset_prior_spend")
        for account in state["accounts"].values():
            reconcile(account, base["starting_cash"])
            if (
                account["ledger"]
                != [
                    {
                        "type": "deposit",
                        "id": "initial-capital",
                        "amount": str(money(base["starting_cash"])),
                    }
                ]
                or account["portfolio"]["positions"]
                or account["intents"]
                or account["broker"]["operations"]
                or account["broker"]["active_quote"]
                or number(account["portfolio"]["cash"]) != number(base["starting_cash"])
                or number(account["portfolio"]["fees"]) != 0
                or number(account["portfolio"]["spread"]) != 0
            ):
                raise SafetyError("forward_review_requires_untouched_accounts")
        if at < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_clock")
        state.update(
            engine_fingerprint=current, control=initial_control(), last_runtime_at=at.isoformat()
        )
        state["audit"].append(
            {
                "type": "forward_predecision_code_review",
                "at": at.isoformat(),
                "before_state_hash": expected_state_hash,
                "from_engine_fingerprint": expected_engine_fingerprint,
                "to_engine_fingerprint": current,
            }
        )
        return {"status": "reviewed", "duplicate": False}, state

    return store.update(transition)


def reserve_capture(
    store, base, day, context_hash, *, cost_eur, monthly_fixed_cost_eur="0", now=None
):
    """Reserve once per UTC day before HTTP; duplicate unknown captures never retry."""
    store = owned_store(store)
    at = instant((now or datetime.now(UTC)).isoformat())
    if day != at.date().isoformat():
        raise SafetyError("forward_current_capture_day_required")
    amount = number(cost_eur)
    fixed = number(monthly_fixed_cost_eur)
    if amount <= 0 or fixed < 0:
        raise SafetyError("positive_capture_cost_reservation_required")

    def transition(state):
        if state is None:
            raise SafetyError("forward_capture_requires_existing_trial")
        check_config(state, base)
        if at < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_clock")
        month = day[:7]
        bucket = state["operations"]["months"].setdefault(
            month, {"events": 0, "spend_eur": "0", "inference_spend_eur": "0", "model_calls": 0}
        )
        captures = bucket.setdefault("captures", {})
        prior_fixed = bucket.get("fixed_runtime_spend_eur")
        if prior_fixed is not None and number(prior_fixed) != fixed:
            raise SafetyError("forward_fixed_cost_context_conflict")
        if day in captures:
            row = captures[day]
            if row["context_hash"] != context_hash or number(row["cost_eur"]) != amount:
                raise SafetyError("forward_capture_context_conflict")
            return {**row, "duplicate": True}, state
        if len(captures) >= base["operations"]["max_events_per_month"]:
            raise SafetyError("capture_event_budget_exhausted")
        fixed_reserve = fixed if prior_fixed is None else number("0")
        if number(bucket["spend_eur"]) + amount + fixed_reserve > number(
            base["operations"]["monthly_budget_eur"]
        ):
            raise SafetyError("capture_cost_budget_exhausted")
        row = {
            "day": day,
            "at": at.isoformat(),
            "context_hash": context_hash,
            "cost_eur": str(amount),
            "status": "reserved",
        }
        captures[day] = row
        bucket["spend_eur"] = str(number(bucket["spend_eur"]) + amount + fixed_reserve)
        if fixed > 0:
            bucket["fixed_runtime_spend_eur"] = str(fixed)
        bucket["capture_spend_eur"] = str(number(bucket.get("capture_spend_eur", "0")) + amount)
        state["last_runtime_at"] = at.isoformat()
        state["audit"].append({"type": "forward_capture_reserved", **row})
        return {**row, "duplicate": False}, state

    return store.update(transition)
