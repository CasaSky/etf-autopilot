# Keeping maintainer context out of Git

Agent instructions, personal research, account setup notes and runtime state belong
outside public commits. The repository uses three checks around that boundary.

1. `.gitignore` keeps the known local files/folders out of ordinary staging.
2. `.githooks/pre-commit` rejects private paths in the index, including force-added files.
3. `.githooks/pre-push` inspects full local commit history/reflogs and the exact commit
   objects being pushed. Deleting a file later does not make its older commit safe.

CI also checks history after fetching it in full. CI is a backup check, not a way
to retract data already pushed. Install the local hooks before making commits:

```sh
git config --local core.hooksPath .githooks
python3 scripts/check_private_files.py --staged --history
```

The configuration applies only to this checkout, not other repositories. Re-run
the setup when cloning a new working copy; Git does not activate repository hooks
automatically. Keep hook scripts executable on systems that require it.

The checker protects agent/config directories, instruction files, handoff/research
notes, personal setup/status documents, credentials and runtime state by path.
It allows public agent implementation code and synthetic test fixtures. It does
not scan all file contents or detect private text copied into a public file under
another name. Review the source candidate and its history before publication.
Hooks can also be bypassed deliberately; do not use bypass flags to publish private
material. A raw workspace or `.git` archive is not a public source release.

## An ignored folder can still be visible

Ignoring a folder does not delete it or hide it from the editor's file browser.
Check Git's view instead:

```sh
git check-ignore -v .codex/config.toml
git ls-files --cached -- .codex
git status --short --ignored -- .codex
```

A matched ignore rule and `!! .codex/` mean ignored. An empty `ls-files` result means
no files from that folder are tracked in the current index. The history checker
separately verifies whether protected paths appear in older commits.

If a private file was previously committed, adding an ignore rule or deleting the
file does not remove that history. Stop before pushing and review a targeted
history-removal plan with the repository owner. Preserve original private notes
outside the public history. Never assume a clean current tree proves a clean past.
