"""Finite AWS paper orchestration over the existing financial/source primitives."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .capture import capture_issuer
from .domain import SafetyError, digest, instant, load_json, number, validate_config
from .forward import forward_markdown, forward_summary, trial_engine_fingerprint
from .forward_artifacts import ForwardArtifacts
from .forward_control import OwnedStore, check_config, reserve_capture, trial_identity
from .forward_operations import forward_status
from .forward_selection import run_forward_decision
from .history import write_immutable_json
from .observations import save_observations
from .selection import bedrock_provider
from .store import S3Store

STATE_KEY = "paper/forward/state.json"
INPUT_PREFIX = "paper/forward/inputs"
MODEL_ID = "eu.amazon.nova-micro-v1:0"
REGION = "eu-central-1"
CAPTURE_COST = "0.002"


def validate_runtime(config, plan, runtime, *, allow_model):
    validate_config(config)
    if not isinstance(runtime, dict) or set(runtime) != {
        "version",
        "config_hash",
        "plan_hash",
        "trial_id",
        "model_enabled",
        "model_id",
        "model_cost_eur",
        "capture_cost_eur",
        "monthly_fixed_cost_eur",
        "operating_cost_eur",
    }:
        raise SafetyError("unsupported_forward_runtime")
    if (
        type(runtime["version"]) is not int
        or runtime["version"] != 1
        or runtime["config_hash"] != digest(config)
        or runtime["plan_hash"] != digest(plan)
        or type(runtime["model_enabled"]) is not bool
        or runtime["model_id"] != MODEL_ID
        or runtime["model_cost_eur"] != "0.02"
        or runtime["capture_cost_eur"] != CAPTURE_COST
        or runtime["monthly_fixed_cost_eur"] != "0.30"
        or runtime["operating_cost_eur"] != "0.01"
    ):
        raise SafetyError("forward_runtime_binding_or_cost_changed")
    limits = config["operations"]
    if (
        limits["max_model_calls"] > 1
        or limits["max_input_tokens"] > 2000
        or limits["max_output_tokens"] > 400
        or limits["max_events_per_month"] > 40
        or number(limits["monthly_budget_eur"]) > 1
    ):
        raise SafetyError("forward_runtime_budget_exceeds_reviewed_caps")
    if runtime["model_enabled"] and not allow_model:
        raise SafetyError("forward_model_permission_disabled")


def build_runtime(config, plan, state, *, model_enabled=False):
    result = {
        "version": 1,
        "config_hash": digest(config),
        "plan_hash": digest(plan),
        "trial_id": trial_identity(state),
        "model_enabled": model_enabled,
        "model_id": MODEL_ID,
        "model_cost_eur": "0.02",
        "capture_cost_eur": CAPTURE_COST,
        "monthly_fixed_cost_eur": "0.30",
        "operating_cost_eur": "0.01",
    }
    validate_runtime(config, plan, result, allow_model=True)
    return result


def read_inputs(s3, bucket, expected_runtime_hash):
    from botocore.exceptions import BotoCoreError, ClientError

    values = {}
    for name in ("runtime", "config", "plan"):
        try:
            response = s3.get_object(Bucket=bucket, Key=f"{INPUT_PREFIX}/{name}.json")
            body = response["Body"]
            try:
                if not 0 < response["ContentLength"] <= 100_000:
                    raise SafetyError("forward_runtime_input_size_limit")
                raw = body.read(100_001)
            finally:
                body.close()
            if len(raw) != response["ContentLength"]:
                raise SafetyError("forward_runtime_input_length_mismatch")
            values[name] = json.loads(raw, parse_float=str)
        except ClientError, BotoCoreError, OSError, ValueError, UnicodeError:
            raise SafetyError("forward_runtime_input_unavailable") from None
    if not expected_runtime_hash or digest(values["runtime"]) != expected_runtime_hash:
        raise SafetyError("forward_runtime_not_reviewed")
    return values["config"], values["plan"], values["runtime"]


def dispatch(s3, bucket, event, *, runtime_hash, allow_model, notify=None, remaining_ms=None):
    action = event.get("action")
    if (
        (action == "forward_tick" and set(event) != {"action", "scheduled_at"})
        or (action in {"forward_health", "forward_alert_test"} and set(event) != {"action"})
        or action not in {"forward_tick", "forward_health", "forward_alert_test"}
    ):
        raise SafetyError("unsupported_forward_worker_request")
    config, plan, runtime = read_inputs(s3, bucket, runtime_hash)
    # Health and alert delivery tests never invoke inference, even with model IAM disabled.
    validate_runtime(config, plan, runtime, allow_model=True)
    if action == "forward_alert_test":
        now = datetime.now(UTC)
        result = _notify_once(
            ForwardArtifacts(s3, bucket),
            now.date().isoformat(),
            {"runtime_hash": runtime_hash, "kind": "delivery_test"},
            "notification_delivery_test",
            notify,
            test=True,
        )
        return {
            "mode": "forward_research_paper",
            "status": "alert_test",
            "notification": result,
            "model_calls": 0,
            "orders": 0,
        }
    if action == "forward_health":
        state = S3Store(s3, bucket, STATE_KEY).read()
        if (
            state["base_config_hash"] != digest(config)
            or state["config_hash"] != digest(state["config"])
            or trial_identity(state) != runtime["trial_id"]
        ):
            raise SafetyError("forward_runtime_trial_mismatch")
        status = forward_status(state)
        return {
            "mode": status["mode"],
            "status": status["status"],
            "last_value_date": status["last_value_date"],
            "observation_age_hours": status["observation_age_hours"],
            "remaining_model_attempts": status["remaining_model_attempts_this_month"],
            "controller": status["controller"],
            "model_permission_enabled": allow_model,
            "state_changes": False,
        }
    return run_cloud_tick(
        s3,
        bucket,
        config,
        plan,
        runtime,
        event["scheduled_at"],
        allow_model=allow_model,
        notify=notify,
        remaining_ms=remaining_ms,
    )


def _notify_once(artifacts, day, context, reason, notify, *, test=False):
    if notify is None:
        return "not_configured"
    marker_name = "alert-test.json" if test else "alert.json"
    result_name = "alert-test-result.json" if test else "alert-result.json"
    existing = artifacts.get_json(day, marker_name)
    if existing is not None:
        if existing.get("context_hash") != digest(context) or existing.get("day") != day:
            raise SafetyError("forward_alert_context_changed")
        result = artifacts.get_json(day, result_name)
        if result is not None and all(result.get(k) == v for k, v in existing.items()):
            return "sent" if result.get("outcome") == "sent" else "unknown"
        return "unknown"
    marker = {"version": 1, "context_hash": digest(context), "day": day, "reason": reason}
    try:
        reservation = artifacts.put_json(day, marker_name, marker)
        if reservation["duplicate"]:
            return "unknown"
    except SafetyError as exc:
        if str(exc) == "immutable_forward_artifact_conflict":
            return "unknown"
        raise
    message = {
        "project": "ETF Autopilot paper",
        "day": day,
        "status": reason,
        "evidence_prefix": f"paper/forward/ticks/{day}/",
    }
    try:
        notify(message)
        outcome = "sent"
    except Exception:
        outcome = "unknown"
    try:
        artifacts.put_json(day, result_name, {**marker, "outcome": outcome})
    except SafetyError:
        return "unknown"
    return outcome


def _compact(outcome, *, duplicate):
    event = outcome["event"]
    return {
        "mode": "forward_research_paper",
        "status": event["status"],
        "selection_status": event.get("selection_status"),
        "duplicate": duplicate,
        "day": outcome["day"],
        "receipt_id": outcome["receipt_id"],
        "last_value_date": outcome["report"]["last_value_date"],
        "model_attempts_reserved": outcome["report"]["model_calls"],
        "orders": event["orders"],
    }


def _result(outcome, artifacts, day, context, notify, *, duplicate):
    result = _compact(outcome, duplicate=duplicate)
    event, status = outcome["event"], outcome["operations"]
    critical = {
        "review_required",
        "reconciliation_required",
        "unknown_model_outcome",
        "decision_deadline_passed",
        "controller_fenced",
    }
    if (
        status["status"] in critical
        or event["status"] in {"selection_blocked", "halted", "decision_expired"}
        or event.get("selection_status") in {"selection_failed", "selection_abandoned"}
        or (
            status["status"] == "waiting_for_fresh_data"
            and number(status["observation_age_hours"]) > 168
        )
    ):
        result["notification"] = _notify_once(artifacts, day, context, status["status"], notify)
        result["attention_required"] = True
    return result


def run_cloud_tick(
    s3,
    bucket,
    config,
    plan,
    runtime,
    scheduled_at,
    *,
    allow_model=False,
    clock=None,
    fetch=None,
    provider=None,
    notify=None,
    remaining_ms=None,
):
    """Capture once per UTC day; resume only verified evidence, never unknown reads."""
    capture_clock = clock
    clock = clock or (lambda: datetime.now(UTC))
    now = instant(clock().isoformat())
    scheduled = instant(scheduled_at)
    if scheduled.date() != now.date() or scheduled > now or (now - scheduled).total_seconds() > 300:
        raise SafetyError("forward_current_scheduled_tick_required")
    validate_runtime(config, plan, runtime, allow_model=allow_model)
    store = OwnedStore(S3Store(s3, bucket, STATE_KEY), "aws")
    state = store.read()
    check_config(state, config)
    if runtime["trial_id"] != trial_identity(state):
        raise SafetyError("forward_runtime_trial_mismatch")
    day = now.date().isoformat()
    context = {
        "version": 1,
        "day": day,
        "config_hash": digest(config),
        "plan_hash": digest(plan),
        "runtime_hash": digest(runtime),
        "engine_fingerprint": trial_engine_fingerprint(),
        "runner_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    reservation = reserve_capture(
        store,
        config,
        day,
        digest(context),
        cost_eur=runtime["capture_cost_eur"],
        monthly_fixed_cost_eur=runtime["monthly_fixed_cost_eur"],
        now=now,
    )
    artifacts = ForwardArtifacts(s3, bucket)
    with TemporaryDirectory(prefix="etf-paper-") as temporary:
        directory = Path(temporary)
        ready = artifacts.restore(day, directory, context)
        if ready is None and reservation["duplicate"]:
            status = "awaiting_capture_evidence"
            alert = _notify_once(artifacts, day, context, status, notify)
            return {
                "status": status,
                "mode": "forward_research_paper",
                "orders": 0,
                "duplicate": True,
                "day": day,
                "notification": alert,
            }
        if ready is None:
            write_immutable_json(directory / "context.json", context)
            write_immutable_json(directory / "plan.json", plan)
            try:
                capture_issuer(
                    directory / "plan.json", directory / "capture", fetch=fetch, clock=capture_clock
                )
                save_observations(
                    directory / "plan.json", directory / "capture", directory / "observations.json"
                )
            except Exception as exc:
                code = str(exc) if isinstance(exc, SafetyError) else "forward_capture_failed"
                # One preservation attempt; an unknown checkpoint is not retried here.
                artifacts.checkpoint(day, directory, context, complete=False)
                artifacts.put_json(
                    day, "failure.json", {"context_hash": digest(context), "code": code}
                )
                alert = _notify_once(artifacts, day, context, code, notify)
                return {
                    "status": "capture_failed",
                    "diagnosis": code,
                    "orders": 0,
                    "mode": "forward_research_paper",
                    "day": day,
                    "notification": alert,
                }
            artifacts.checkpoint(day, directory, context)
        if load_json(directory / "plan.json") != plan:
            raise SafetyError("forward_capture_plan_changed")
        # Re-derive the immutable daily artifact from its original captured bytes.
        data = save_observations(
            directory / "plan.json", directory / "capture", directory / "observations.json"
        )
        prior = artifacts.get_json(day, "outcome.json")
        if prior is not None:
            if (
                prior.get("context_hash") != digest(context)
                or prior.get("receipt_id")
                != digest({k: v for k, v in prior.items() if k != "receipt_id"})
                or prior.get("observation_id") != data["observation_id"]
            ):
                raise SafetyError("forward_outcome_changed")
            return _result(prior, artifacts, day, context, notify, duplicate=True)
        if remaining_ms is not None and remaining_ms() < 65_000:
            return {
                "status": "awaiting_runtime_headroom",
                "mode": "forward_research_paper",
                "orders": 0,
                "day": day,
            }
        options = {}
        if runtime["model_enabled"]:
            invoke = provider or bedrock_provider(MODEL_ID, REGION)

            def guarded_provider(prompt, max_tokens):
                if remaining_ms is not None and remaining_ms() < 55_000:
                    raise SafetyError("forward_inference_headroom_required")
                return invoke(prompt, max_tokens)

            options = {
                "provider": guarded_provider,
                "provider_name": f"bedrock:{REGION}:{MODEL_ID}",
                "reserved_cost_eur": runtime["model_cost_eur"],
            }
        event = run_forward_decision(
            store,
            config,
            data,
            clock=clock,
            operating_cost_eur=runtime["operating_cost_eur"],
            **options,
        )
        snapshot = store.read()
        status = forward_status(snapshot, now=clock())
        outcome = {
            "version": 1,
            "day": day,
            "context_hash": digest(context),
            "capture_id": data["capture_id"],
            "observation_id": data["observation_id"],
            "event": event,
            "report": forward_summary(snapshot),
            "markdown": forward_markdown(snapshot),
            "operations": status,
            "financial_state_hash": digest(snapshot),
        }
        outcome["receipt_id"] = digest(outcome)
        artifacts.put_json(day, "outcome.json", outcome)
        return _result(outcome, artifacts, day, context, notify, duplicate=False)
