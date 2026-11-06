"""One durably reserved model choice, outside forward state transactions."""

from copy import deepcopy
from datetime import UTC, datetime

from .domain import SafetyError, canonical, digest, instant, number
from .selection import SYSTEM, decode_choice, validate_provider_options


def choice_hash(decision):
    return digest(
        {
            "plan_hash": decision["plan_hash"],
            "strategy": decision["selected_strategy"],
            "choice_at": decision["choice_at"],
        }
    )


def reserve_choice(decision, config, bucket, selection):
    """Called inside the same atomic transition that consumes the monthly slot."""
    plan = decision["plan"]
    limits = config["operations"]
    decision.update(
        selected_strategy=config["strategy"], choice_at=plan["at"], source="deterministic"
    )
    if selection is None or plan["choices"]["passive"] == plan["choices"]["slow_trend"]:
        decision["choice_hash"] = choice_hash(decision)
        if selection is not None:
            decision["inference_skipped"] = "equivalent_candidate_allocations"
        return
    amount = number(selection["reserved_cost_eur"])
    reason = None
    if amount <= 0:
        reason = "positive_inference_reservation_required"
    elif bucket["model_calls"] >= limits["max_model_calls"]:
        reason = "model_call_budget_exhausted"
    elif number(bucket["spend_eur"]) + amount > number(limits["monthly_budget_eur"]):
        reason = "model_cost_budget_exhausted"
    elif (
        len(SYSTEM.encode()) + len(canonical(plan["context"]).encode()) + 512
        > limits["max_input_tokens"]
        or limits["max_output_tokens"] < 1
    ):
        reason = "model_input_budget_exhausted"
    decision.update(selected_strategy=None, choice_at=None)
    if reason:
        decision.update(status="selection_blocked", failure_code=reason)
        return
    bucket["model_calls"] += 1
    bucket["spend_eur"] = str(number(bucket["spend_eur"]) + amount)
    bucket["inference_spend_eur"] = str(number(bucket["inference_spend_eur"]) + amount)
    decision.update(
        status="reserved", reserved_cost_eur=str(amount), source=selection["provider_name"]
    )


def verify_choice(decision, config):
    plan = decision["plan"]
    if (
        digest(plan) != decision["plan_hash"]
        or plan["config_hash"] != digest(config)
        or plan["system_hash"] != digest(SYSTEM)
        or decision.get("selected_strategy") not in plan["choices"]
        or decision.get("choice_hash") != choice_hash(decision)
        or instant(decision["choice_at"]) < instant(plan["at"])
        or (
            decision.get("reserved_cost_eur") is None
            and decision["selected_strategy"] != config["strategy"]
        )
        or (
            decision.get("reserved_cost_eur") is not None
            and decision.get("response_hash") != digest({"strategy": decision["selected_strategy"]})
        )
    ):
        raise SafetyError("forward_committed_choice_changed")
    return decision["selected_strategy"]


def run_forward_decision(
    store,
    config,
    data,
    *,
    provider=None,
    provider_name=None,
    reserved_cost_eur="0",
    clock=None,
    operating_cost_eur="0.01",
):
    from .forward import run_forward_cycle, trial_engine_fingerprint

    clock = clock or (lambda: datetime.now(UTC))
    amount = validate_provider_options(provider, provider_name, reserved_cost_eur)
    selection = (
        {"provider_name": provider_name, "reserved_cost_eur": str(amount)}
        if provider is not None
        else None
    )
    result = run_forward_cycle(
        store, config, data, now=clock(), operating_cost_eur=operating_cost_eur, selection=selection
    )
    if result["status"] != "decision_reserved" or result["duplicate"]:
        if result.get("decision_id"):
            receipt = store.read()["decisions"][result["decision_id"]]
            result = {
                **result,
                "selection_status": receipt["status"],
                "selected_strategy": receipt.get("selected_strategy"),
            }
        return result
    decision_id = result["decision_id"]
    snapshot = store.read()
    reserved = deepcopy(snapshot["decisions"][decision_id])
    # An operator may have closed the reservation before this process reached it.
    if reserved["status"] != "reserved":
        return {**result, "selection_status": reserved["status"]}
    try:
        proposal = decode_choice(
            provider(
                canonical(reserved["plan"]["context"]), config["operations"]["max_output_tokens"]
            ),
            config["operations"]["max_output_tokens"],
        )
        status, failure = "ready", None
    except Exception:
        proposal, status, failure = None, "selection_failed", "provider_or_response_failure"
    finished = instant(clock().isoformat())

    def finish(state):
        if (
            state is None
            or state["engine_fingerprint"] != trial_engine_fingerprint()
            or state["base_config_hash"] != digest(config)
        ):
            raise SafetyError("forward_trial_config_or_engine_changed")
        decision = state["decisions"][decision_id]
        if (
            decision["plan_hash"] != reserved["plan_hash"]
            or digest(decision["plan"]) != reserved["plan_hash"]
        ):
            raise SafetyError("forward_committed_plan_changed")
        if decision["status"] != "reserved":
            return deepcopy(decision), state
        if finished < instant(state["last_runtime_at"]):
            raise SafetyError("forward_nonmonotonic_choice_clock")
        decision.update(status=status, choice_at=finished.isoformat())
        if proposal:
            decision.update(selected_strategy=proposal["strategy"], response_hash=digest(proposal))
            decision["choice_hash"] = choice_hash(decision)
        else:
            decision["failure_code"] = failure
            state["pending"] = None
        state["last_runtime_at"] = finished.isoformat()
        state["audit"].append(
            {
                "type": "forward_selection_result",
                "at": finished.isoformat(),
                "id": decision_id,
                "status": decision["status"],
            }
        )
        return deepcopy(decision), state

    receipt = store.update(finish)
    return {**result, "selection_status": receipt["status"]}


def abandon_forward_selection(store, decision_id):
    """Close an unknown attempt explicitly; preserve the month and all reserved cost."""
    from .forward import MODE

    def transition(state):
        if state is None or state.get("mode") != MODE:
            raise SafetyError("forward_trial_required")
        decision = state["decisions"].get(decision_id)
        if decision is None or decision["status"] != "reserved":
            raise SafetyError("no_reserved_forward_selection_to_abandon")
        decision.update(
            status="selection_abandoned", failure_code="operator_abandoned_unknown_inference"
        )
        state["pending"] = None
        state["audit"].append({"type": "forward_selection_abandoned", "id": decision_id})
        return deepcopy(decision), state

    return store.update(transition)
