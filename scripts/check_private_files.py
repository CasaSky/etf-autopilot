"""Reject maintainer-local files in the index or any inspected Git commit.

This is a path guard, not a content/secret scanner. Private content must also stay
out of public files under other names. Inspect the release candidate before a push.
"""

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_DIRECTORIES = {".agents", ".codex", ".aws", ".venv", "research", "private", "var"}
PRIVATE_FILENAMES = {"agents.md", "agent.md", "claude.md", "gemini.md"}
PRIVATE_PATHS = {"docs/setup.md", "docs/status.md"}


def private_path(path):
    parts = PurePosixPath(path).parts
    lower = [part.casefold() for part in parts]
    name = lower[-1] if lower else ""
    return (
        bool(PRIVATE_DIRECTORIES.intersection(lower))
        or name in PRIVATE_FILENAMES
        or name.startswith(("handoff", "mcp_review"))
        or ("research" in name and name.endswith((".md", ".txt", ".pdf", ".docx")))
        or "/".join(lower) in PRIVATE_PATHS
        or (name.startswith(".env") and name != ".env.example")
        or name.endswith((".sqlite3", ".pem", ".key"))
    )


def git(*arguments):
    return subprocess.check_output(["git", *arguments], stderr=subprocess.PIPE)


def inspect(staged=False, history=False, extra_revisions=()):
    violations = []
    staged_count, commit_count = 0, 0
    if staged:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        for raw in paths.split(b"\0"):
            if not raw:
                continue
            path = raw.decode("utf-8", errors="surrogateescape")
            staged_count += 1
            if private_path(path):
                violations.append(f"staged: {path}")
    if history:
        revisions = git("rev-list", "--all", "--reflog", *extra_revisions).decode().splitlines()
        for revision in revisions:
            commit_count += 1
            for raw in git("ls-tree", "-r", "--name-only", "-z", revision).split(b"\0"):
                if not raw:
                    continue
                path = raw.decode("utf-8", errors="surrogateescape")
                if private_path(path):
                    violations.append(f"history {revision[:12]}: {path}")
    return violations, staged_count, commit_count


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check staged files and Git history for private paths"
    )
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--push-stdin", action="store_true", help="Also inspect exact pre-push objects"
    )
    args = parser.parse_args(argv)
    extra = []
    if args.push_stdin:
        for line in sys.stdin:
            fields = line.split()
            if len(fields) != 4 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[1]):
                print(
                    "Invalid pre-push input; refusing to skip the privacy check.", file=sys.stderr
                )
                return 2
            if set(fields[1]) != {"0"}:  # A deleted ref does not introduce a commit.
                extra.append(fields[1])
    staged = args.staged or not (args.staged or args.history)
    history = args.history or args.push_stdin or not (args.staged or args.history)
    try:
        violations, staged_count, commit_count = inspect(staged, history, extra)
    except subprocess.CalledProcessError:
        print("Git privacy inspection failed; no commit/push should proceed.", file=sys.stderr)
        return 2
    if violations:
        print("Private paths found; commit/push blocked:", file=sys.stderr)
        for violation in sorted(set(violations)):
            print(f"  {violation}", file=sys.stderr)
        return 1
    print(f"Privacy check passed: {staged_count} staged files; {commit_count} history commits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
