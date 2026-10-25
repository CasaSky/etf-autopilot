# Maintaining the public repository

This is a maintainer guide for
[CasaSky/etf-autopilot](https://github.com/CasaSky/etf-autopilot). The public source
uses MIT. Local agent instructions, personal research and account setup stay
outside the published source and its history.

## Access and workflow policy

Only the repository owner can create, update or delete upstream branches and tags,
including through a pull request merge. Issues and pull requests remain open to
everyone. Do not add collaborators, deploy keys or ruleset bypass actors without
the owner's authorization.

Every upstream branch requires GitHub-verified commit signatures, including owner
pushes. The signature ruleset has no bypass actors. Verify the signing key is
registered on the correct GitHub account before creating commits.

The default workflow runs on owner pushes to `main`. It has no pull request or
manual trigger, and checks both the original actor and the actor requesting a
rerun. All external fork workflows require approval in repository settings.
Workflow tokens are read-only, Actions cannot approve pull requests, and automatic
merging is disabled. Contributors can run checks in their own forks and include
the results in their pull requests.

## The project's public story

Repository description:

> Paper-first ETF investing infrastructure for auditable AI experiments,
> deterministic risk checks and reproducible evaluation.

The README should let a newcomer understand the idea, run a useful example and
find a small contribution. Keep implemented behavior separate from planned AI
allocation and data/account integrations. Publish actual evaluation evidence only
when its source, assumptions, costs and limitations can be inspected.

Relevant topics: `python`, `etf`, `paper-trading`, `backtesting`,
`portfolio-management`, `ai-agents`, `model-context-protocol`.
Topics describe scope and help discovery; they are not a ranking mechanism.
Do not use unrelated topics to attract traffic. See
[GitHub's topic guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Public source versus maintainer-local context

The source checkout contains private setup/research context retained locally for
its owner. `.gitignore` excludes that context, local agent settings, credentials,
runtime ledgers, evaluation artifacts and environments from ordinary staging.
The original research files remain intact locally; they are not rewritten as
marketing copy.

Before each commit, review the staged paths and content and run the privacy guard.
Ignored files are not a security boundary: force-adding, previously tracked files,
Git history and third-party uploads need separate review. Do not publish the whole
workspace as a raw archive. The public README/docs must not link to ignored local
files or include personal filesystem paths or account information.

The selected project license is MIT. Confirm the copyright attribution and that
contributed material can be shared under it before release. Source dependencies
keep their own licenses; historical datasets need independent redistribution rights.
See [GitHub's license guide](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/adding-a-license-to-a-repository).

## Verify each release candidate

1. Authenticate through the owner's normal local or connected-account flow. Never
   put tokens into chat or clone URLs. Check the active account before changing
   repository settings.
2. Run the one-command example, development checks and package build from the public
   candidate. Inspect the Markdown report and confirm synthetic labels are visible.
3. Review staged source, history and MIT attribution. Sign commits and verify their
   signatures before pushing. Assistant-created commits include accurate AI
   co-author attribution; do not attribute unaided human work to an AI assistant.
4. Verify repository access controls, CI, description, relevant topics and private
   vulnerability reporting. Report an account-level CI blocker honestly rather
   than treating it as a passed check.
5. Review the contribution/issue templates and publish a short release note with
   runnable examples, current limits and a few specific contribution opportunities.

## Build interest through useful evidence

Share one reproducible result or engineering explanation at a time: for example,
how an unknown order outcome is reconciled, how holdout exposure is tracked, or how
a bounded AI decision is compared after costs once that integration works. Include
runnable code and acknowledge limitations. Respond to issues and keep accepted
contributions small enough to review.

We can control clarity, usefulness, maintenance and openness. Stars, third-party
coverage and placement among popular repositories remain outcomes, not guarantees.
