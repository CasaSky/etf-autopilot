"""Build a paper-only Lambda zip from current sources; never package the repository root."""

import re
import zipfile
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var/lambda.zip"
SDK_PREFIXES = {
    "boto3": "boto3/",
    "botocore": "botocore/",
    "jmespath": "jmespath/",
    "s3transfer": "s3transfer/",
    "python-dateutil": "dateutil/",
    "six": "six.py",
    "urllib3": "urllib3/",
}


def sdk_files():
    pins = dict(
        re.findall(
            r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)",
            (ROOT / "requirements-aws.lock").read_text(),
            re.MULTILINE,
        )
    )
    if set(pins) != set(SDK_PREFIXES):
        raise RuntimeError("Review changed AWS SDK dependencies before packaging")
    result = {}
    for name, prefix in SDK_PREFIXES.items():
        installed = distribution(name)
        if installed.version != pins[name]:
            raise RuntimeError(f"Install the exact locked {name} version before packaging")
        for entry in installed.files or []:
            path = entry.as_posix()
            if "__pycache__" in entry.parts or path.endswith(".pyc"):
                continue
            if path.endswith((".so", ".pyd", ".dylib")):
                raise RuntimeError("Native SDK files require a reviewed Linux build")
            info = len(entry.parts) > 1 and entry.parts[0].endswith(".dist-info")
            metadata = info and (
                entry.name in {"METADATA", "WHEEL", "entry_points.txt"}
                or "licenses" in entry.parts
                or entry.name.startswith(("LICENSE", "NOTICE"))
            )
            if not (path.startswith(prefix) or metadata):
                continue
            source = Path(installed.locate_file(entry))
            if source.is_symlink() or not source.is_file() or ".." in entry.parts:
                raise RuntimeError("Unsafe SDK package file")
            result[path] = source.read_bytes()
    return result


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    files = sdk_files()
    for source in sorted((ROOT / "src/etf_autopilot").glob("*.py")):
        files[f"etf_autopilot/{source.name}"] = source.read_bytes()
    files["etf_autopilot/paper.json"] = (ROOT / "config/paper.json").read_bytes()
    # Pure Python SDK sources come from the reviewed exact lock, never the whole
    # environment. Fixed timestamps keep the same inputs byte-for-byte reproducible.
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    print(OUTPUT)


if __name__ == "__main__":
    main()
