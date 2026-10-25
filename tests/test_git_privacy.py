import os
import subprocess
import sys

import pytest

from .conftest import ROOT

CHECKER = ROOT / "scripts/check_private_files.py"


@pytest.fixture
def repository(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*arguments):
        return subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Privacy test",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                *arguments,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "--initial-branch=main")
    return tmp_path, git, env


def check(root, env, *arguments, stdin=None):
    return subprocess.run(
        [sys.executable, str(CHECKER), *arguments],
        cwd=root,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "docs/agents.md",
        "HANDOFF-v4.md",
        "MCP_REVIEW.md",
        ".agents/session.json",
        "research/strategy.md",
        "ai-investing-research.md",
        "docs/SETUP.md",
        ".env",
        "var/paper.sqlite3",
    ],
)
def test_force_added_private_paths_are_rejected(repository, path):
    root, git, env = repository
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("Synthetic privacy fixture, not actual private research.\n")
    git("add", "--force", path)
    result = check(root, env, "--staged")
    assert result.returncode == 1
    assert path in result.stderr
    assert "Synthetic privacy fixture" not in result.stderr


def test_public_ai_source_and_documentation_are_allowed(repository):
    root, git, env = repository
    for path in (
        "src/agents.py",
        "tests/test_research_cli.py",
        "docs/DATA_EVALUATION.md",
        ".env.example",
    ):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("Public fixture\n")
        git("add", path)
    assert check(root, env, "--staged").returncode == 0
    git("commit", "-m", "Public fixture")
    result = check(root, env, "--history")
    assert result.returncode == 0
    assert "1 history commits" in result.stdout


def test_removing_a_private_file_does_not_hide_its_previous_commit(repository):
    root, git, env = repository
    (root / "AGENTS.md").write_text("Synthetic private-file fixture\n")
    git("add", "AGENTS.md")
    git("commit", "-m", "Synthetic unsafe fixture")
    git("rm", "AGENTS.md")
    git("commit", "-m", "Synthetic removal fixture")
    assert not (root / "AGENTS.md").exists()
    result = check(root, env, "--history")
    assert result.returncode == 1
    assert "history" in result.stderr and "AGENTS.md" in result.stderr


def test_exact_push_object_is_checked_even_without_a_branch_ref(repository):
    root, git, env = repository
    (root / "README.md").write_text("Public fixture\n")
    git("add", "README.md")
    git("commit", "-m", "Public fixture")
    (root / "HANDOFF.md").write_text("Synthetic private-file fixture\n")
    git("add", "HANDOFF.md")
    tree = git("write-tree").stdout.strip()
    unreferenced = git("commit-tree", tree, "-m", "Synthetic unreferenced fixture").stdout.strip()
    stdin = f"(detached) {unreferenced} refs/heads/demo {'0' * 40}\n"
    result = check(root, env, "--history", "--push-stdin", stdin=stdin)
    assert result.returncode == 1
    assert "HANDOFF.md" in result.stderr


def test_pre_commit_hook_blocks_the_actual_git_commit(repository):
    root, git, env = repository
    (root / "AGENTS.md").write_text("Synthetic private-file fixture\n")
    git("add", "AGENTS.md")
    # Use the project's real hook and checker against the temporary test repository.
    (root / "scripts").mkdir()
    (root / "scripts/check_private_files.py").write_bytes(CHECKER.read_bytes())
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Privacy test",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=" + str(ROOT / ".githooks"),
            "commit",
            "-m",
            "Must be blocked",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "commit/push blocked" in result.stderr
    assert git("rev-list", "--all").stdout == ""
