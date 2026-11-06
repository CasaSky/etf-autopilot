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
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/forward-demo")
    args = parser.parse_args()
    source = args.output_dir / "source-demo"
    subprocess.run(
        [sys.executable, "-S", str(ROOT / "examples/capture_demo.py"), "--output-dir", str(source)],
        check=True,
        capture_output=True,
    )
    sys.path.insert(0, str(ROOT / "src"))
    from etf_autopilot.domain import digest, load_json, number
    from etf_autopilot.forward import forward_markdown, forward_summary, run_forward_cycle
    from etf_autopilot.history import write_immutable_json
    from etf_autopilot.observations import observations_for_capture
    from etf_autopilot.store import LocalStore

    config = load_json(ROOT / "config/paper.json")
    config["execution"]["sizing_policy"] = "bounded"
    write_immutable_json(args.output_dir / "config.json", config)
    initial = observations_for_capture(source / "plan.json", source / "snapshot")

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
            (fresh, "2026-01-09T09:00:00Z", "decision_committed"),
            (same, "2026-01-10T09:00:00Z", "waiting_for_future_observation"),
            (future, "2026-01-13T09:00:00Z", "executed"),
        ],
        1,
    ):
        write_immutable_json(args.output_dir / f"observations-{index}.json", data)
        result = run_forward_cycle(store, config, data, now=datetime.fromisoformat(now))
        assert result["status"] == expected, result
        steps.append(result)
    before = store.read()
    duplicate = run_forward_cycle(
        store, config, future, now=datetime.fromisoformat("2026-01-13T10:00:00Z")
    )
    assert duplicate["duplicate"] and store.read() == before
    report = forward_summary(store.read())
    write_immutable_json(
        args.output_dir / "results.json",
        {
            "steps": [{k: v for k, v in row.items() if k != "duplicate"} for row in steps],
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
