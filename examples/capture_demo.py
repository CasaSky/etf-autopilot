"""Fictional source capture and replay, without network, keys, models or trading."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "var/capture-demo")
    parser.add_argument(
        "--forward-tick", action="store_true", help="Also run a finite paper operation"
    )
    args = parser.parse_args()
    source = args.output_dir / "fictional-sources"
    subprocess.run(
        [sys.executable, "-S", str(ROOT / "examples/issuer_demo.py"), "--output-dir", str(source)],
        check=True,
        capture_output=True,
    )
    sys.path.insert(0, str(ROOT / "src"))
    from etf_autopilot.capture import capture_issuer
    from etf_autopilot.domain import load_json
    from etf_autopilot.history import write_immutable_json

    plan = {
        "version": 1,
        "spec": load_json(source / "spec.json"),
        "downloads": {
            "RESEARCH_EQUITY": {"provider": "ishares", "portfolio_id": "111"},
            "RESEARCH_BOND": {"provider": "ishares", "portfolio_id": "222"},
        },
    }
    plan_path = args.output_dir / "plan.json"
    write_immutable_json(plan_path, plan)
    calls = []

    def fixture(url, limit, deadline):
        calls.append(url)
        parsed = urlparse(url)
        if parsed.hostname == "www.ecb.europa.eu":
            filename = "fx.xml"
        else:
            filename = (
                "equity.xls" if parse_qs(parsed.query)["portfolioId"] == ["111"] else "bond.xls"
            )
        return (source / filename).read_bytes()

    directory = args.output_dir / "snapshot"
    result = capture_issuer(
        plan_path, directory, fetch=fixture, clock=lambda: datetime(2026, 1, 9, tzinfo=UTC)
    )

    def forbidden(*args):
        raise AssertionError("Completed capture must replay without a source request")

    repeated = capture_issuer(plan_path, directory, fetch=forbidden)
    assert repeated == {**result, "duplicate": True}
    tick_info = {}
    if args.forward_tick:
        from etf_autopilot.forward_tick import run_forward_tick
        from etf_autopilot.store import LocalStore

        store = LocalStore(args.output_dir / "paper.sqlite3")
        config = load_json(ROOT / "config/paper.json")
        tick_path = args.output_dir / "tick"
        tick = run_forward_tick(
            store,
            config,
            plan_path,
            tick_path,
            fetch=fixture,
            clock=lambda: datetime(2026, 1, 8, 9, tzinfo=UTC),
        )
        before = store.read()
        replay = run_forward_tick(
            store, config, plan_path, tick_path, fetch=forbidden, clock=forbidden
        )
        assert replay["duplicate"] and store.read() == before
        assert tick["cycle"]["status"] == "decision_committed" and tick["cycle"]["orders"] == 0
        tick_info = {
            "paper_tick_status": tick["cycle"]["status"],
            "paper_report": str((tick_path / "report.md").resolve()),
        }
    print(
        json.dumps(
            {
                "status": "fictional_capture_replayed",
                "origin": result["origin"],
                "evidence_id": result["evidence_id"],
                "path": str(directory.resolve()),
                "fixture_reads": len(calls),
                "network_calls": 0,
                "model_calls": 0,
                "execution_eligible": False,
                "investment_evidence": False,
                **tick_info,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
