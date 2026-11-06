"""Fictional causal decision/observation/execution workflow, entirely offline."""

import argparse
import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--scripted-choice", action="store_true", help="Use a fictional model response"
    )
    args = parser.parse_args()
    args.output_dir = args.output_dir or ROOT / "var" / (
        "forward-choice-demo" if args.scripted_choice else "forward-demo"
    )
    source = args.output_dir / "source-demo"
    subprocess.run(
        [sys.executable, "-S", str(ROOT / "examples/capture_demo.py"), "--output-dir", str(source)],
        check=True,
        capture_output=True,
    )
    sys.path.insert(0, str(ROOT / "src"))
    from etf_autopilot.domain import digest, load_json, number
    from etf_autopilot.forward import forward_markdown, forward_summary, run_forward_cycle
    from etf_autopilot.forward_selection import run_forward_decision
    from etf_autopilot.history import write_immutable_json
    from etf_autopilot.observations import observations_for_capture
    from etf_autopilot.store import LocalStore

    config = load_json(ROOT / "config/paper.json")
    config["execution"]["sizing_policy"] = "bounded"
    if args.scripted_choice:
        config["operations"]["max_model_calls"] = 1
    write_immutable_json(args.output_dir / "config.json", config)
    initial = observations_for_capture(source / "plan.json", source / "snapshot")
    if args.scripted_choice:
        for row in initial["rows"]:
            if row["date"] >= "2025-12-01":
                row["values"]["RESEARCH_EQUITY"] = "70"
        initial["observation_id"] = digest(
            {k: v for k, v in initial.items() if k != "observation_id"}
        )
    scripted_calls = []

    def scripted_provider(prompt, max_tokens):
        scripted_calls.append(1)
        return '{"strategy":"passive"}'

    def cycle(store, data, now):
        if args.scripted_choice:
            return run_forward_decision(
                store,
                config,
                data,
                clock=lambda: datetime.fromisoformat(now),
                provider=scripted_provider,
                provider_name="scripted_fixture_not_a_model",
                reserved_cost_eur="0.02",
            )
        return run_forward_cycle(store, config, data, now=datetime.fromisoformat(now))

    def next_snapshot(previous, day, received):
        data = deepcopy(previous)
        data["rows"].append(
            {
                "date": day,
                "values": {
                    key: str(number(value) * number("1.01"))
                    for key, value in previous["rows"][-1]["values"].items()
                },
            }
        )
        data["captured_at"] = received
        data["manifest"]["retrieved_at"] = received
        data["capture_id"] = digest(
            {"fictional_previous": previous["capture_id"], "received": received}
        )
        data["observation_id"] = digest({k: v for k, v in data.items() if k != "observation_id"})
        return data

    fresh = next_snapshot(initial, "2026-01-08", "2026-01-09T08:00:00Z")
    same = next_snapshot(fresh, "2026-01-09", "2026-01-10T08:00:00Z")
    future = next_snapshot(same, "2026-01-12", "2026-01-13T08:00:00Z")
    store = LocalStore(args.output_dir / "paper.sqlite3")
    steps = []
    for index, (data, now, expected) in enumerate(
        [
            (initial, "2026-01-09T01:00:00Z", "waiting_for_input"),
            (
                fresh,
                "2026-01-09T09:00:00Z",
                "decision_reserved" if args.scripted_choice else "decision_committed",
            ),
            (same, "2026-01-10T09:00:00Z", "waiting_for_future_observation"),
            (future, "2026-01-13T09:00:00Z", "executed"),
        ],
        1,
    ):
        write_immutable_json(args.output_dir / f"observations-{index}.json", data)
        result = cycle(store, data, now)
        assert result["status"] == expected, result
        steps.append(result)
    before = store.read()
    duplicate = cycle(store, future, "2026-01-13T10:00:00Z")
    assert duplicate["duplicate"] and store.read() == before
    report = forward_summary(store.read())
    write_immutable_json(
        args.output_dir / "results.json",
        {
            "steps": [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"duplicate", "selection_status", "selected_strategy"}
                }
                for row in steps
            ],
            "summary": report,
        },
    )
    rendered = forward_markdown(store.read())
    path = args.output_dir / "report.md"
    if path.exists() and path.read_text() != rendered:
        raise RuntimeError("Immutable report changed")
    path.write_text(rendered)
    print(
        json.dumps(
            {
                "status": "fictional_forward_workflow_completed",
                "network_calls": 0,
                "model_calls": 0,
                "scripted_provider_calls_this_run": len(scripted_calls),
                "reserved_model_attempts": report["model_calls"],
                "decision_before_future_value": True,
                "duplicate_state_unchanged": True,
                "investment_evidence": False,
                "report": str(path.resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
