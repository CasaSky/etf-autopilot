"""Finite recurring paper entry point; a scheduler supplies ticks, not trading authority."""

from datetime import UTC, datetime

from .domain import SafetyError, instant, number, validate_config
from .report import summary
from .selection import run_decision, validate_provider_options
from .strategy import allocation, validate_market


def _equivalent_choice(store, config, event, options):
    if options.get("provider") is None or allocation(
        event["market"], config, "passive"
    ) != allocation(event["market"], config, "slow_trend"):
        return False
    try:
        state = store.read()
    except SafetyError as exc:
        if str(exc) != "state_not_initialized":
            raise
        state = None
    if state is not None:
        from .engine import assert_bound

        assert_bound(state, config)
        prior = next(
            (
                row
                for row in state["operations"].get("allocations", [])
                if row["event_id"] == event["id"]
            ),
            None,
        )
        if prior is not None and prior["source"] != "deterministic":
            return False  # Preserve a recorded/inflight model receipt, never retrofit it.
    return True


def run_cycle(store, config, event, *, now=None, **selection_options):
    """Require fresh current-month input against the runtime clock, then process once.

    No automatic halt clearance, inference retry or input-date rewriting occurs.
    The prepared input contract is still synthetic; a verified forward feed is separate.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise SafetyError("timezone_required")
    now = now.astimezone(UTC)
    validate_config(config)
    if "data_contract" in config:
        raise SafetyError("historical_contract_requires_evaluation_runner")
    try:
        validate_market(event, config)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SafetyError("invalid_market_input") from exc
    decision_at = instant(event["at"])
    market_at = instant(event["market"]["as_of"])
    reason = None
    if decision_at.strftime("%Y-%m") != now.strftime("%Y-%m"):
        reason = "current_month_input_required"
    elif (
        decision_at > now
        or market_at > now
        or number(str((now - market_at).total_seconds()))
        > number(config["risk"]["max_data_age_hours"]) * 3600
    ):
        reason = "fresh_runtime_input_required"
    if reason:
        return {"status": "waiting_for_input", "mode": "paper", "diagnosis": reason, "orders": 0}
    if set(selection_options) - {"provider", "provider_name", "reserved_cost_eur"}:
        raise SafetyError("unsupported_cycle_selection_option")
    validate_provider_options(
        selection_options.get("provider"),
        selection_options.get("provider_name"),
        selection_options.get("reserved_cost_eur", "0"),
    )
    skip = _equivalent_choice(store, config, event, selection_options)
    decision = run_decision(store, config, event, **({} if skip else selection_options))
    result = {
        "status": decision["status"],
        "mode": "paper",
        "decision": decision,
        "report": summary(store.read()),
    }
    if skip:
        result["inference_skipped"] = "equivalent_candidate_allocations"
    return result
