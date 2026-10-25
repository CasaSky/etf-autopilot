"""Run the complete offline paper workflow with Python alone, from any directory."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="ETF Autopilot: offline paper example")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/quickstart")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))

    from etf_autopilot.domain import load_json
    from etf_autopilot.engine import run_event
    from etf_autopilot.report import markdown
    from etf_autopilot.store import LocalStore

    config = load_json(ROOT / "config/paper.json")
    store = LocalStore(args.output_dir / "paper.sqlite3")
    results = [
        run_event(store, config, event)
        for event in load_json(ROOT / "fixtures/monthly_events.json")
    ]
    rendered = markdown(store.read())
    report = args.output_dir / "report.md"
    report.write_text(rendered)
    print(rendered)
    print(f"Events: {len(results)}; already recorded: {sum(r['duplicate'] for r in results)}.")
    print(f"Saved report: {report.resolve()}")
    return 2 if any(result["status"] == "halted" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
