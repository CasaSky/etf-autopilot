"""Generate synthetic daily exports and exercise the provider adapter offline.

No market history is downloaded. Prices/calendars are fictional contract data.
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="var/provider-demo")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from etf_autopilot.domain import load_json
    from etf_autopilot.history import dataset_receipt, write_immutable_json
    from etf_autopilot.providers import import_eodhd

    directory = Path(args.output_dir)
    instruments = load_json(root / "fixtures/history_contract/manifest.json")["instruments"]
    bindings = {
        asset: {
            "symbol": "FIXTURE_" + item["class"].upper() + ".XETRA",
            "isin": item["isin"],
            "currency": "EUR",
            "evidence_url": "https://example.invalid/synthetic/" + item["class"],
        }
        for asset, item in instruments.items()
    }
    spec = {
        "version": 1,
        "origin": "synthetic_fixture",
        "retrieved_at": "2026-01-08T00:00:00Z",
        "signal_start": "2022-01",
        "signal_end": "2025-12",
        "instruments": instruments,
        "bindings": bindings,
        "usage_rights": "synthetic_fixture",
    }
    write_immutable_json(directory / "spec.json", spec)
    paths = {}
    for role in ("equity", "bond"):
        rows = []
        day = date(2022, 1, 1)
        while day <= date(2026, 1, 7):
            if day.weekday() < 5:
                month = (day.year - 2022) * 12 + day.month - 1
                adjusted = 100 + (month + month % 5 if role == "equity" else month // 3)
                close = adjusted * 2
                rows.append(
                    {
                        "date": day.isoformat(),
                        "open": close,
                        "high": close + 1,
                        "low": close - 1,
                        "close": close,
                        "adjusted_close": adjusted,
                        "volume": 1000,
                    }
                )
            day += timedelta(days=1)
        paths[role] = directory / f"{role}.json"
        write_immutable_json(paths[role], rows)
    dataset = import_eodhd(
        directory / "spec.json", paths["equity"], paths["bond"], directory / "converted"
    )
    print(
        json.dumps(
            {
                **dataset_receipt(dataset),
                "path": str((directory / "converted/dataset.json").resolve()),
                "investment_evidence": False,
                "network_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
