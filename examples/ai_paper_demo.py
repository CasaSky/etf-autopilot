"""Exercise bounded choices with a scripted fixture: no model, credentials or network."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Scripted allocation workflow, not an AI backtest")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/ai-quickstart")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))

    from etf_autopilot.domain import load_json
    from etf_autopilot.report import markdown
    from etf_autopilot.selection import run_decision
    from etf_autopilot.store import LocalStore

    config = load_json(ROOT / "config/paper.json")
    config["operations"]["max_model_calls"] = 1
    store = LocalStore(args.output_dir / "paper.sqlite3")
    calls = []

    def scripted_fixture(prompt, output_limit):
        calls.append(1)
        return '{"strategy":"passive"}'

    results = [
        run_decision(
            store,
            config,
            event,
            provider=scripted_fixture,
            provider_name="scripted_fixture_not_ai",
            reserved_cost_eur="0.02",
        )
        for event in load_json(ROOT / "fixtures/monthly_events.json")
    ]
    report = args.output_dir / "report.md"
    report.write_text(markdown(store.read()))
    print("Scripted fixture only: no actual AI inference or investment-performance evidence.")
    print(f"Scripted calls this run: {len(calls)}; events: {len(results)}.")
    print(f"Saved report: {report.resolve()}")
    return 2 if any(result["status"] != "completed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
