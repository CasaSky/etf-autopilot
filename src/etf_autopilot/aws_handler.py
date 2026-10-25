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
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3", config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)
    )
    store = S3Store(s3, os.environ["STATE_BUCKET"])
    config = load_json(Path(__file__).with_name("paper.json"))
    action = event.get("action")
    try:
        if action == "run":
            # Input key is fixed by the deployment, never selected by a model or request.
            obj = s3.get_object(Bucket=store.bucket, Key="paper/inputs/current.json")
            if obj["ContentLength"] > MAX_STATE_BYTES:
                raise SafetyError("input_too_large")
            payload = json.loads(obj["Body"].read(), parse_float=str)
            result = run_event(store, config, payload)
        elif action == "reconcile":
            result = reconcile_state(store, config)
        elif action == "health":
            state = store.read()
            report = summary(state)
            result = {
                "status": "halted" if report["halt"] else "operational",
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
