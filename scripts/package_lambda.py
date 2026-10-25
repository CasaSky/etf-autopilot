"""Build a paper-only Lambda zip from current sources; never package the repository root."""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var/lambda.zip"


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # boto3 is supplied by the managed Lambda runtime. No native macOS packages
    # or cached staging-directory files enter the archive.
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted((ROOT / "src/etf_autopilot").glob("*.py")):
            archive.write(source, f"etf_autopilot/{source.name}")
        archive.write(ROOT / "config/paper.json", "etf_autopilot/paper.json")
    print(OUTPUT)


if __name__ == "__main__":
    main()
