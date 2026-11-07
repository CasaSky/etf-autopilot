"""One finite source capture -> bounded paper decision -> retained report operation."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from .capture import capture_issuer
from .domain import SafetyError, digest, load_json, number, validate_config
from .forward import MODE, forward_markdown, forward_summary, trial_engine_fingerprint
from .forward_control import owned_store
from .forward_selection import run_forward_decision
from .history import write_immutable_json
from .observations import save_observations
from .selection import validate_provider_options


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_forward_tick(
    store,
    config,
    plan_path,
    output_directory,
    *,
    provider=None,
    provider_name=None,
    reserved_cost_eur="0",
    operating_cost_eur="0.01",
    fetch=None,
    clock=None,
):
    """A stable directory identifies one attempt; replay never recaptures or retries AI.

    A scheduler may supply stable attempt IDs. This function does not start one,
    sleep, retry failed captures, upload data or grant permissions.
    """
    store = owned_store(store)
    validate_config(config)
    if "data_contract" in config:
        raise SafetyError("forward_base_config_required")
    amount = validate_provider_options(provider, provider_name, reserved_cost_eur)
    cost = number(operating_cost_eur)
    if cost < 0:
        raise SafetyError("negative_operating_cost")
    plan_path, directory = Path(plan_path), Path(output_directory)
    if plan_path.stat().st_size > 100_000:
        raise SafetyError("issuer_capture_plan_size_limit")
    context = {
        "version": 1,
        "config_hash": digest(config),
        "plan_hash": digest(load_json(plan_path)),
        "engine_fingerprint": trial_engine_fingerprint(),
        "runner_hash": _file_hash(Path(__file__)),
        "provider_name": provider_name if provider is not None else None,
        "reserved_cost_eur": str(amount),
        "operating_cost_eur": str(cost),
    }
    context_path = directory / "context.json"
    if directory.exists():
        if not context_path.is_file() or load_json(context_path) != context:
            raise SafetyError("forward_tick_context_conflict")
    else:
        directory.mkdir(parents=True, mode=0o700)
        write_immutable_json(context_path, context)
    try:
        state = store.read()
    except SafetyError as exc:
        if str(exc) != "state_not_initialized":
            raise
        state = None
    if state is not None and (
        state.get("mode") != MODE
        or state["base_config_hash"] != digest(config)
        or state["engine_fingerprint"] != trial_engine_fingerprint()
    ):
        raise SafetyError("forward_trial_config_or_engine_changed")
    receipt_path = directory / "tick.json"
    if receipt_path.is_file():
        receipt = load_json(receipt_path)
        if receipt.get("receipt_id") != digest(
            {k: v for k, v in receipt.items() if k != "receipt_id"}
        ):
            raise SafetyError("forward_tick_receipt_changed")
        if receipt.get("context_hash") != digest(context):
            raise SafetyError("forward_tick_context_conflict")
        if set(receipt.get("file_hashes", {})) != {"observations.json", "report.json", "report.md"}:
            raise SafetyError("forward_tick_files_changed")
        for name, value in receipt["file_hashes"].items():
            if _file_hash(directory / name) != value:
                raise SafetyError("forward_tick_files_changed")
        # Verify the immutable capture too, without consulting its network or clock.
        capture_issuer(plan_path, directory / "capture", fetch=fetch, clock=clock)
        if (
            state is None
            or receipt["observation_id"] not in state["events"]
            or state["events"][receipt["observation_id"]]["hash"] != receipt.get("event_hash")
        ):
            raise SafetyError("forward_tick_state_missing")
        return {**receipt, "duplicate": True}
    capture = capture_issuer(plan_path, directory / "capture", fetch=fetch, clock=clock)
    data = save_observations(plan_path, directory / "capture", directory / "observations.json")
    result = run_forward_decision(
        store,
        config,
        data,
        provider=provider,
        provider_name=provider_name,
        reserved_cost_eur=str(amount),
        operating_cost_eur=str(cost),
        clock=clock or (lambda: datetime.now(UTC)),
    )
    snapshot = store.read()
    write_immutable_json(directory / "report.json", forward_summary(snapshot))
    rendered = forward_markdown(snapshot)
    report_path = directory / "report.md"
    if report_path.exists() and report_path.read_text() != rendered:
        raise SafetyError("forward_tick_report_changed")
    if not report_path.exists():
        with report_path.open("x") as target:
            target.write(rendered)
        report_path.chmod(0o600)
    receipt = {
        "version": 1,
        "mode": MODE,
        "context_hash": digest(context),
        "capture_id": capture["evidence_id"],
        "observation_id": data["observation_id"],
        "captured_at": data["captured_at"],
        "cycle": result,
        "event_hash": snapshot["events"][data["observation_id"]]["hash"],
        "file_hashes": {
            name: _file_hash(directory / name)
            for name in ("observations.json", "report.json", "report.md")
        },
    }
    receipt["receipt_id"] = digest(receipt)
    write_immutable_json(receipt_path, receipt)
    return {**receipt, "duplicate": False}
