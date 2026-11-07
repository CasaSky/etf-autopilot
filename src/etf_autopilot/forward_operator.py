"""Explicit owner commands for reviewed controller transfer; never model tools."""

import re
from pathlib import Path

from .domain import SafetyError, digest, load_json
from .forward_cloud import REGION, STATE_KEY, build_runtime
from .forward_control import OwnedStore, review_predecision_code
from .forward_selection import abandon_forward_selection
from .forward_transfer import abort_transfer, complete_transfer, freeze_transfer, install_transfer
from .history import write_immutable_json
from .store import MAX_STATE_BYTES, LocalStore, S3Store


def add_operator_commands(commands):
    review = commands.add_parser(
        "forward-review-code",
        help="Explicit predecision code review; cannot reset any financial history",
    )
    review.add_argument("--expected-state-hash", required=True)
    review.add_argument("--expected-engine-fingerprint", required=True)
    review.add_argument("--allow-predecision-review", action="store_true", required=True)
    runtime = commands.add_parser(
        "forward-runtime", help="Prepare a sealed AWS runtime input without cloud writes"
    )
    runtime.add_argument("plan")
    runtime.add_argument("--model-enabled", action="store_true")
    runtime.add_argument("--out", required=True)
    abandon = commands.add_parser(
        "forward-cloud-choice-abandon",
        help="Close an unknown cloud inference without retrying or refunding its budget",
    )
    abandon.add_argument("decision_id")
    abandon.add_argument("--expected-state-hash", required=True)
    freeze = commands.add_parser(
        "forward-transfer-freeze",
        help="Fence the current controller and save an immutable handoff bundle",
    )
    freeze.add_argument("--owner", choices=("local", "aws"), default="local")
    freeze.add_argument("--target", choices=("local", "aws"), required=True)
    freeze.add_argument("--expected-state-hash", required=True)
    freeze.add_argument("--out", required=True)
    for name in ("install", "complete", "abort"):
        command = commands.add_parser(
            "forward-transfer-" + name,
            help=f"Explicitly {name} a previously frozen controller handoff",
        )
        command.add_argument("bundle")
    for command in [
        freeze,
        abandon,
        *[
            commands.choices["forward-transfer-" + name]
            for name in ("install", "complete", "abort")
        ],
    ]:
        command.add_argument("--allow-cloud-state", action="store_true")
        command.add_argument("--bucket")
        command.add_argument("--account-id")
        command.add_argument("--region")
        command.add_argument("--profile")


def _cloud_store(args):
    if not (
        args.allow_cloud_state
        and args.region == REGION
        and args.profile
        and isinstance(args.account_id, str)
        and re.fullmatch(r"[0-9]{12}", args.account_id)
        and isinstance(args.bucket, str)
        and args.bucket.startswith("etf-autopilot-")
        and args.bucket.endswith(f"-{args.account_id}-{REGION}")
    ):
        raise SafetyError("explicit_owned_cloud_state_parameters_required")
    import boto3
    from botocore.config import Config

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        settings = Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)
        identity = session.client("sts", config=settings).get_caller_identity()
        if identity["Account"] != args.account_id:
            raise SafetyError("forward_cloud_account_mismatch")
        client = session.client("s3", config=settings)
        client.head_bucket(Bucket=args.bucket, ExpectedBucketOwner=args.account_id)
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("forward_cloud_identity_or_bucket_check_failed") from None
    return S3Store(client, args.bucket, STATE_KEY)


def _bundle(path):
    path = Path(path)
    if path.stat().st_size > MAX_STATE_BYTES + 1000:
        raise SafetyError("forward_transfer_bundle_size_limit")
    body = load_json(path)
    if (
        not isinstance(body, dict)
        or set(body) != {"version", "snapshot", "snapshot_hash"}
        or body["version"] != 1
        or body["snapshot_hash"] != digest(body["snapshot"])
    ):
        raise SafetyError("forward_transfer_bundle_changed")
    return body["snapshot"]


def operator_command(args):
    local = LocalStore(args.state)
    config = load_json(args.config)
    if args.command == "forward-review-code":
        return review_predecision_code(
            local, config, args.expected_state_hash, args.expected_engine_fingerprint
        )
    if args.command == "forward-runtime":
        runtime = build_runtime(
            config, load_json(args.plan), local.read(), model_enabled=args.model_enabled
        )
        write_immutable_json(args.out, runtime)
        return {
            "status": "prepared",
            "runtime_hash": digest(runtime),
            "path": str(Path(args.out).resolve()),
            "cloud_writes": 0,
            "model_calls": 0,
        }
    if args.command == "forward-cloud-choice-abandon":
        return abandon_forward_selection(
            OwnedStore(_cloud_store(args), "aws"),
            args.decision_id,
            config=config,
            expected_state_hash=args.expected_state_hash,
        )
    if args.command == "forward-transfer-freeze":
        source = local if args.owner == "local" else _cloud_store(args)
        snapshot = freeze_transfer(source, config, args.expected_state_hash, args.target)
        bundle = {"version": 1, "snapshot": snapshot, "snapshot_hash": digest(snapshot)}
        write_immutable_json(args.out, bundle)
        return {
            "status": "frozen",
            "transfer_id": snapshot["control"]["transfer"]["id"],
            "snapshot_hash": bundle["snapshot_hash"],
            "path": str(Path(args.out).resolve()),
        }
    frozen = _bundle(args.bundle)
    record = frozen["control"]["transfer"]
    remote = _cloud_store(args)
    source = local if record["from"] == "local" else remote
    destination = local if record["to"] == "local" else remote
    if args.command == "forward-transfer-install":
        return install_transfer(destination, config, frozen)
    if args.command == "forward-transfer-complete":
        return complete_transfer(source, destination, config, frozen)
    if args.command == "forward-transfer-abort":
        return abort_transfer(source, destination, config, frozen)
    raise SafetyError("unsupported_forward_operator_command")
