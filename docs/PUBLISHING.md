# First public release

This is a maintainer guide. The repository has no configured GitHub destination
at preparation time. Nothing in this guide creates a repository, changes visibility,
pushes source or promises a ranking.

## The project's public story

Suggested repository description:

> Paper-first ETF investing infrastructure for auditable AI experiments,
> deterministic risk checks and reproducible evaluation.

The README should let a newcomer understand the idea, run a useful example and
find a small contribution. Keep implemented behavior separate from planned AI
allocation and data/account integrations. Publish actual evaluation evidence only
when its source, assumptions, costs and limitations can be inspected.

Suggested relevant topics: `python`, `etf`, `paper-trading`, `backtesting`,
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

Before the initial commit, review `git add --dry-run .` and each staged file.
Ignored files are not a security boundary: force-adding, previously tracked files,
Git history and third-party uploads need separate review. Do not publish the whole
workspace as a raw archive. The public README/docs must not link to ignored local
files or include personal filesystem paths or account information.

The selected project license is MIT. Confirm the copyright attribution and that
contributed material can be shared under it before release. Source dependencies
keep their own licenses; historical datasets need independent redistribution rights.
See [GitHub's license guide](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/adding-a-license-to-a-repository).

## Publish only after the release candidate is concrete

1. Choose the GitHub owner/repository and authenticate through the user's normal
   local or connected-account flow. Never put tokens into chat or clone URLs.
2. Run the one-command example, development checks and package build from the public
   candidate. Inspect the Markdown report and confirm synthetic labels are visible.
3. Review the initial staged source and MIT attribution, then create/push the
   user-approved public repository. No publication has been performed by this work.
4. Set the concise description and relevant topics, verify CI in GitHub, and enable
   private vulnerability reporting with an actual maintainer contact.
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
