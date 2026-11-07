"""AWS paper worker. No credentials, broker transport or model permission required."""

import json
import os
from pathlib import Path

from .domain import SafetyError, canonical, load_json
from .engine import reconcile_state, run_event
from .report import summary
from .store import MAX_STATE_BYTES, S3Store


def handler(event, context):
    if os.environ.get("EXECUTION_MODE", "paper") != "paper":
        raise SafetyError("paper_only_worker")
    action = event.get("action")
    forward_actions = {"forward_tick", "forward_health", "forward_alert_test"}
    if action in forward_actions and os.environ.get("FORWARD_ENABLED") != "true":
        result = {"status": "halted", "mode": "paper", "diagnosis": "forward_runtime_disabled"}
        print(canonical(result))
        return result
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3", config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)
    )
    if action in forward_actions:
        from .forward_cloud import REGION, dispatch

        def notify(message):
            client = boto3.client(
                "sns",
                region_name=REGION,
                config=Config(
                    retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10
                ),
            )
            client.publish(
                TopicArn=os.environ["FORWARD_ALERT_TOPIC"],
                Subject="ETF Autopilot paper needs review",
                Message=canonical(message),
            )

        try:
            if os.environ.get("AWS_REGION", REGION) != REGION:
                raise SafetyError("forward_reviewed_region_required")
            if action == "forward_tick" and context is None:
                raise SafetyError("forward_lambda_context_required")
            result = dispatch(
                s3,
                os.environ["STATE_BUCKET"],
                event,
                runtime_hash=os.environ.get("FORWARD_RUNTIME_HASH"),
                allow_model=os.environ.get("FORWARD_ALLOW_MODEL_CALLS") == "true",
                notify=notify if os.environ.get("FORWARD_ALERT_TOPIC") else None,
                remaining_ms=context.get_remaining_time_in_millis if context else None,
            )
        except SafetyError as exc:
            print(canonical({"status": "halted", "mode": "paper", "diagnosis": str(exc)}))
            raise
        except Exception:
            print(
                canonical(
                    {"status": "halted", "mode": "paper", "diagnosis": "forward_runtime_failure"}
                )
            )
            raise SafetyError("forward_runtime_failure") from None
        print(canonical(result))
        if (
            result.get("attention_required")
            or result["status"]
            in {
                "capture_failed",
                "awaiting_capture_evidence",
                "awaiting_runtime_headroom",
                "halted",
                "selection_blocked",
                "decision_expired",
            }
            or result.get("selection_status")
            in {"selection_failed", "selection_abandoned", "reserved"}
            or (result["status"] == "alert_test" and result["notification"] != "sent")
        ):
            raise SafetyError("forward_operation_requires_review")
        return result
    store = S3Store(s3, os.environ["STATE_BUCKET"])
    config = load_json(Path(__file__).with_name("paper.json"))
    try:
        if action in {"run", "cycle"}:
            # Input key is fixed by the deployment, never selected by a model or request.
            obj = s3.get_object(Bucket=store.bucket, Key="paper/inputs/current.json")
            if obj["ContentLength"] > MAX_STATE_BYTES:
                raise SafetyError("input_too_large")
            payload = json.loads(obj["Body"].read(), parse_float=str)
            if action == "cycle":
                from .cycle import run_cycle

                result = run_cycle(store, config, payload)
            else:
                result = run_event(store, config, payload)
        elif action == "reconcile":
            result = reconcile_state(store, config)
        elif action == "health":
            state = store.read()
            report = summary(state)
            result = {
                "status": (
                    "halted"
                    if report["halt"]
                    else "awaiting_allocation_result"
                    if report["pending_allocations"]
                    else "operational"
                ),
                "mode": "paper",
                "diagnosis": report["halt"],
                "events": len(state["events"]),
                "ai": "disabled",
            }
        else:
            raise SafetyError("unsupported_worker_action")
    except SafetyError as exc:
        result = {"status": "halted", "mode": "paper", "diagnosis": str(exc)}
    print(canonical(result))
    return result
