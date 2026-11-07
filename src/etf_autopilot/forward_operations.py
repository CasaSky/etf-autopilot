"""Read-only operating diagnosis for a supervised forward paper trial."""

from datetime import UTC, datetime, timedelta

from .domain import SafetyError, instant, number
from .forward import MODE, trial_engine_fingerprint


def forward_status(state, *, now=None):
    if state.get("mode") != MODE:
        raise SafetyError("forward_trial_required")
    now = instant((now or datetime.now(UTC)).isoformat())
    received = instant(state["last_received_at"])
    if now < received or now < instant(state["last_runtime_at"]):
        raise SafetyError("forward_status_clock_before_recorded_state")
    age = (
        number(str((now - instant(state["last_value_date"] + "T00:00:00Z")).total_seconds())) / 3600
    )
    receipt_age = number(str((now - received).total_seconds())) / 3600
    limit = number(state["config"]["risk"]["max_data_age_hours"])
    month = now.strftime("%Y-%m")
    limits = state["config"]["operations"]
    bucket = state["operations"]["months"].get(month, {})
    pending = state["decisions"].get(state["pending"])
    status, action = (
        "ready_for_new_capture",
        "Capture and verify the next actual source observation.",
    )
    if state["engine_fingerprint"] != trial_engine_fingerprint():
        status, action = (
            "review_required",
            "Review changed financial/source code before advancing this trial.",
        )
    elif state["halt"]:
        status, action = (
            "reconciliation_required",
            "Inspect the halt and reconcile confirmed paper outcomes; do not retry orders.",
        )
    elif pending and pending["status"] == "reserved":
        status, action = (
            "unknown_model_outcome",
            "Inspect the unknown attempt; abandon it explicitly before further decisions.",
        )
    elif pending and now - instant(pending["plan"]["at"]) > timedelta(days=7):
        status, action = (
            "decision_deadline_passed",
            "Retain expiry on a new verified capture. The old opportunity cannot be filled.",
        )
    elif age > limit or receipt_age > limit:
        status, action = (
            "waiting_for_fresh_data",
            "Wait for a fresh joint issuer/ECB observation. Keep the existing freshness limit.",
        )
    elif pending:
        status, action = (
            "waiting_for_later_observation",
            "Capture an eligible NAV dated after both decision and saved response.",
        )
    elif month in state["slots"]:
        status, action = (
            "monthly_slot_consumed",
            "Continue valuations from new captures; this month's decision remains consumed.",
        )
    return {
        "mode": MODE,
        "checked_at": now.isoformat(),
        "status": status,
        "next_action": action,
        "last_value_date": state["last_value_date"],
        "last_received_at": state["last_received_at"],
        "observation_age_hours": str(age.quantize(number("0.01"))),
        "receipt_age_hours": str(receipt_age.quantize(number("0.01"))),
        "freshness_limit_hours": str(limit),
        "month": month,
        "reserved_model_attempts_this_month": bucket.get("model_calls", 0),
        "remaining_model_attempts_this_month": max(
            0, limits["max_model_calls"] - bucket.get("model_calls", 0)
        ),
        "reserved_spend_eur_this_month": bucket.get("spend_eur", "0"),
        "remaining_estimated_budget_eur_this_month": str(
            max(
                number("0"),
                number(limits["monthly_budget_eur"]) - number(bucket.get("spend_eur", "0")),
            )
        ),
        "pending_decision": state["pending"],
        "halt": state["halt"],
        "state_changes": False,
        "network_calls": 0,
    }


def forward_status_markdown(state, *, now=None):
    report = forward_status(state, now=now)
    return (
        "# Forward paper operating status\n\n"
        f"Status: **{report['status']}**.\n\n{report['next_action']}\n\n"
        f"Latest common value date: {report['last_value_date']}. "
        f"Age: {report['observation_age_hours']} hours "
        f"(limit {report['freshness_limit_hours']}).\n\n"
        f"Source received: {report['last_received_at']}.\n\n"
        f"Model attempts reserved this month: {report['reserved_model_attempts_this_month']}; "
        f"remaining: {report['remaining_model_attempts_this_month']}.\n\n"
        f"Estimated spend reserved: EUR {report['reserved_spend_eur_this_month']}. "
        f"Remaining estimate budget: EUR {report['remaining_estimated_budget_eur_this_month']}.\n\n"
        f"Pending decision: {report['pending_decision'] or 'none'}. "
        f"Halt: {report['halt'] or 'none'}.\n\n"
        "Read-only diagnosis; it starts no capture, model call, order or schedule.\n"
    )
