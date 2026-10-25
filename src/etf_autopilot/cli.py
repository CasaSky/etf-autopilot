"""Offline paper commands and an explicitly selected read-only account check."""

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from .domain import SafetyError, load_json, number
from .engine import reconcile_state, run_event
from .report import markdown, summary
from .store import LocalStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="ETF Autopilot — paper only")
    parser.add_argument("--config", default="config/paper.json")
    parser.add_argument("--state", default="var/paper.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Replay deterministic synthetic monthly events")
    demo.add_argument("--fixture", default="fixtures/monthly_events.json")
    demo.add_argument("--report", default="var/paper-report.md")
    event = commands.add_parser("run", help="Process one paper event")
    event.add_argument("event_file")
    for name, help_text in (
        ("paper-decision", "Choose a fixed paper hypothesis, then run deterministic checks"),
        ("paper-cycle", "Run one finite cycle with fresh input checked against the runtime clock"),
    ):
        decision = commands.add_parser(name, help=help_text)
        decision.add_argument("event_file")
        decision.add_argument("--model-id", help="Explicit Bedrock model or inference profile ID")
        decision.add_argument("--allow-model-call", action="store_true")
        decision.add_argument("--reserved-cost-eur", default="0")
        decision.add_argument("--region", help="Explicit AWS region for a paid model call")
        decision.add_argument("--profile", help="AWS SDK profile; never pass access keys")
    abandon = commands.add_parser(
        "allocation-abandon", help="Close an unknown inference without retrying or refunding it"
    )
    abandon.add_argument("event_id")
    commands.add_parser("reconcile", help="Recover confirmed paper outcomes; never place orders")
    report = commands.add_parser("report")
    report.add_argument("--json", action="store_true")
    broker = commands.add_parser("broker-check", help="Explicit read-only live account check")
    broker.add_argument("--allow-account-read", action="store_true", required=True)
    broker.add_argument("--expires-at", help="UTC expiry timestamp; securely prompt for the key")
    asset = commands.add_parser("broker-asset", help="Read and bind one real ETF catalog identity")
    asset.add_argument("--isin", required=True)
    asset.add_argument("--asset-id", help="Require a previously reviewed listing UUID")
    asset.add_argument("--expires-at", help="UTC expiry timestamp; securely prompt for the key")
    asset.add_argument("--out", required=True, help="New immutable identity evidence JSON")
    verify_asset = commands.add_parser(
        "asset-verify", help="Check a saved catalog response offline"
    )
    verify_asset.add_argument("response")
    verify_asset.add_argument("--isin", required=True)
    verify_asset.add_argument("--asset-id")
    history = commands.add_parser(
        "history-import", help="Validate a local monthly return-index CSV"
    )
    history.add_argument("manifest")
    history.add_argument("csv")
    history.add_argument("--out", required=True)
    provider = commands.add_parser(
        "history-eodhd", help="Convert local daily adjusted-close exports"
    )
    provider.add_argument("spec")
    provider.add_argument("equity_json")
    provider.add_argument("bond_json")
    provider.add_argument("--out-dir", required=True)
    evaluation_plan = commands.add_parser(
        "evaluation-plan", help="Write a fixed evaluation protocol"
    )
    evaluation_plan.add_argument("dataset")
    for window in ("development", "validation", "holdout"):
        evaluation_plan.add_argument(f"--{window}", required=True, help="YYYY-MM:YYYY-MM")
    evaluation_plan.add_argument("--operating-cost", default="0.01")
    evaluation_plan.add_argument("--out", required=True)
    register = commands.add_parser("evaluation-register", help="Register a protocol before results")
    evaluate = commands.add_parser("evaluate", help="Run one registered historical research window")
    for research in (register, evaluate):
        research.add_argument("dataset")
        research.add_argument("protocol")
        research.add_argument("--registry", default="var/evaluations.sqlite3")
    evaluate.add_argument(
        "--window", choices=("development", "validation", "holdout"), required=True
    )
    evaluate.add_argument("--out-dir", default="var/evaluations")
    args = parser.parse_args(argv)
    try:
        if args.command == "asset-verify":
            from .instruments import asset_uuid, resolve_etf

            path = Path(args.response)
            if path.stat().st_size > 1_000_000:
                raise SafetyError("broker_response_too_large")
            payload = load_json(path)
            expected_id = args.asset_id
            if isinstance(payload, dict) and set(payload) == {"tool", "arguments", "response"}:
                if payload["tool"] != "list_assets" or not isinstance(payload["arguments"], dict):
                    raise SafetyError("asset_catalog_tool_required")
                arguments = payload["arguments"]
                if "isin" in arguments and arguments["isin"] != args.isin:
                    raise SafetyError("asset_request_isin_mismatch")
                if "id" in arguments:
                    if not isinstance(arguments["id"], str):
                        raise SafetyError("invalid_broker_asset_id")
                    identifiers = [asset_uuid(item.strip()) for item in arguments["id"].split(",")]
                    if expected_id is None and len(identifiers) == 1:
                        expected_id = identifiers[0]
                    if expected_id is None or asset_uuid(expected_id) not in identifiers:
                        raise SafetyError("asset_request_uuid_binding_required")
                payload = payload["response"]
            identity = resolve_etf(payload, args.isin, expected_id)
            print(json.dumps({"status": "verified_local_payload", "identity": identity}, indent=2))
            return 0
        if args.command in {"broker-check", "broker-asset"}:
            if args.command == "broker-asset":
                from .instruments import asset_uuid, validate_isin

                validate_isin(args.isin)
                if args.asset_id is not None:
                    asset_uuid(args.asset_id)
            try:
                from .bitpanda import (
                    Credentials,
                    check_account,
                    credentials_from_environment,
                    inspect_asset,
                )
            except ImportError:
                raise SafetyError("broker_dependencies_missing_install_broker_extra") from None

            if args.expires_at and not sys.stdin.isatty():
                raise SafetyError("interactive_terminal_required_for_secret_prompt")
            credentials = (
                Credentials(getpass.getpass("Bitpanda key (hidden): "), args.expires_at)
                if args.expires_at
                else credentials_from_environment()
            )
            if args.command == "broker-asset":
                from .history import write_immutable_json

                evidence = asyncio.run(inspect_asset(credentials, args.isin, args.asset_id))
                write_immutable_json(args.out, evidence)
                print(json.dumps({"status": "identity_saved", **evidence}, indent=2))
                return 0
            print(json.dumps(asyncio.run(check_account(credentials)), indent=2))
            return 0
        if args.command == "history-eodhd":
            from .history import dataset_receipt
            from .providers import import_eodhd

            dataset = import_eodhd(args.spec, args.equity_json, args.bond_json, args.out_dir)
            print(
                json.dumps(
                    {
                        **dataset_receipt(dataset),
                        "path": str(Path(args.out_dir).resolve() / "dataset.json"),
                    },
                    indent=2,
                )
            )
            return 0
        config = load_json(args.config)
        if args.command in {"history-import", "evaluation-plan", "evaluation-register", "evaluate"}:
            return research_command(args, config)
        if args.command in {"paper-decision", "paper-cycle"}:
            from .selection import bedrock_provider, run_decision

            options = {}
            model_options = args.model_id or args.allow_model_call or args.region or args.profile
            if model_options or number(args.reserved_cost_eur) != 0:
                if not (
                    args.model_id
                    and args.allow_model_call
                    and args.region
                    and number(args.reserved_cost_eur) > 0
                ):
                    raise SafetyError("model_requires_opt_in_id_region_and_positive_cost_reserve")
                options = {
                    "provider": bedrock_provider(args.model_id, args.region, args.profile),
                    "provider_name": f"bedrock:{args.region}:{args.model_id}",
                    "reserved_cost_eur": args.reserved_cost_eur,
                }
            runner = run_decision
            if args.command == "paper-cycle":
                from .cycle import run_cycle

                runner = run_cycle
            result = runner(LocalStore(args.state), config, load_json(args.event_file), **options)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "completed" else 2
        store = LocalStore(args.state)
        if args.command == "allocation-abandon":
            from .selection import abandon_selection

            print(json.dumps(abandon_selection(store, config, args.event_id), indent=2))
            return 0
        if args.command == "demo":
            results = [run_event(store, config, event) for event in load_json(args.fixture)]
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown(store.read()))
            print(json.dumps({"events": results, "report": str(path.resolve())}, indent=2))
            return 0 if all(r["status"] != "halted" for r in results) else 2
        if args.command == "run":
            result = run_event(store, config, load_json(args.event_file))
            print(json.dumps(result, indent=2))
            return 2 if result["status"] == "halted" else 0
        if args.command == "reconcile":
            result = reconcile_state(store, config)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "matched" else 2
        state = store.read()
        print(json.dumps(summary(state), indent=2) if args.json else markdown(state))
        return 0
    except (SafetyError, ValueError, KeyError, OSError) as exc:
        code = str(exc) if isinstance(exc, SafetyError) else "invalid_local_input_or_state"
        print(json.dumps({"status": "blocked", "code": code}))
        return 2


def research_command(args, config):
    from .domain import digest
    from .evaluation import EvaluationRegistry, evaluation_markdown, protocol_template
    from .history import dataset_receipt, import_history, write_immutable_json

    if args.command == "history-import":
        dataset = import_history(args.manifest, args.csv)
        write_immutable_json(args.out, dataset)
        print(
            json.dumps(
                {**dataset_receipt(dataset), "path": str(Path(args.out).resolve())}, indent=2
            )
        )
        return 0
    dataset = load_json(args.dataset)
    if args.command == "evaluation-plan":
        windows = {}
        for window in ("development", "validation", "holdout"):
            values = getattr(args, window).split(":")
            if len(values) != 2:
                raise SafetyError("evaluation_window_requires_start_colon_end")
            windows[window] = dict(zip(("start", "end"), values, strict=True))
        protocol = protocol_template(dataset, config, windows, args.operating_cost)
        write_immutable_json(args.out, protocol)
        print(
            json.dumps(
                {
                    "protocol_id": digest(protocol),
                    "status": "planned_not_registered",
                    "path": str(Path(args.out).resolve()),
                },
                indent=2,
            )
        )
        return 0
    protocol = load_json(args.protocol)
    registry = EvaluationRegistry(args.registry)
    if args.command == "evaluation-register":
        print(json.dumps(registry.register(protocol, dataset, config), indent=2))
        return 0
    result = registry.run(protocol, dataset, config, args.window)
    directory = Path(args.out_dir)
    output = directory / f"{result['run_id']}.json"
    write_immutable_json(output, {key: value for key, value in result.items() if key != "cached"})
    report = output.with_suffix(".md")
    rendered = evaluation_markdown(result)
    if report.exists() and report.read_text() != rendered:
        raise SafetyError("immutable_evaluation_report_changed")
    report.write_text(rendered)
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "window": args.window,
                "origin": result["origin"],
                "cached": result["cached"],
                "ai_effect_tested": False,
                "automatic_promotion": False,
                "result": str(output.resolve()),
                "report": str(report.resolve()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
