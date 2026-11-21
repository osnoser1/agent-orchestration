# Verification, scope check, doc review, design checkpoints, and landing checks

Apply these gates across Step 4 (Brief), Step 5 (Quality gates), and Step 6 (Failure handling).

## Evidence proportional to user priorities

Scale verification effort and evidence collection to task risk and what the user cares about:

1. **Sources and precedence**: read persistent user priorities from `assets.yaml` (`preferences.evidence`) and repo expectations from `## Orchestration` `- Evidence:`, following the precedence order in `references/repo-policy.md`. When the user states an evidence priority in plain language, record it in `assets.yaml` (`references/assets-updates.md`).
2. **Scale and prune**: require the evidence the user and repo prioritize (such as red→green unit/integration output, or browser/device screenshots on visible changes), and drop evidence classes the user explicitly deprioritized (such as video recordings or secondary-platform captures on non-visual work) unless the task is specifically about that deprioritized area.
3. **Forward into briefs**: list the exact commands and required evidence in each child's `VERIFICATION` block (`references/brief-template.md`).

## Scope check before review

When the implementer reports, and before the review pass, run `python3 scripts/orch.py scope --base <base-ref> --max-files <N> --max-lines <N> --expect <GLOB>...` (budget and globs from the brief) and follow its `action:` line. The line names manifest and lockfile changes, files outside the expected globs, and stray scratch files (first 10 files; `--full` lists all with the counts). When the diff outgrows the size budget or the acceptance criteria (`action: trim`), send the implementing child a trim request first: name what the acceptance criteria require and ask for a one-line justification per production file. Review the trimmed diff. For a non-notifying child, run the same check from the supervision loop (`references/supervision.md`) while it works. Without Python, compare `git diff --stat` and any manifest or lockfile changes against the brief's size budget and the acceptance criteria.

*Done when*: the diff fits the size budget, or each file over it carries an accepted justification, before review starts.

## Agent-facing doc review

Agent-facing docs (`AGENTS.md`, `CLAUDE.md`, skills, references, or catalog text that agents read) get the review → fix → re-review loop from `references/brief-template.md` as part of the independent review (`SKILL.md` Step 5.3): the reviewer runs it on those files, so the author's own pass never stands alone. For docs you edit yourself as orchestrator, delegate that review to a fresh reviewer chosen by the reviewer order in `references/selection.md` from any usable model (the order ends at the same model in a fresh conversation, so quota is the only blocker). When none is usable, say so in the report before opening the PR.

*Done when*: a delegated reviewer's final pass finds zero defects, or its residual findings are listed under `## Obstacles / not verified`, or the report names that no model was usable, before the PR.

## Design checkpoints

For user-visible changes whose look, layout, copy, or interaction behavior is a matter of taste (plus any surface matching `- Checkpoints:` in the repo's `## Orchestration`, `references/repo-policy.md`), apply `preferences.design_checkpoints` from `assets.yaml` (Precedence 2):

- **`ask` (default)**: pause once at the checkpoint (`SKILL.md` Step 7) with concrete evidence (screenshot, recording, rendered preview, or CLI transcript) for user approval before building dependent polish or landing.
- **`delegate`**: evaluate the concrete evidence yourself against repo standards, proceed immediately, and report the decision in one line.
- **Persist approval**: record every approved or delegated checkpoint (decision + evidence reference) under **Approved checkpoints** in the user-local durable state (`references/supervision.md`) so post-compaction turns and follow-up batches reuse the recorded decision.

## Final check before landing (Step 5.4)

After cross-family review findings are resolved and before landing, verify five items:

1. **Diff and one key test**: read the diff and run one key verification command yourself. When a diff is too large to read cheaply in orchestrator context, delegate reading it to a `cheap` subagent (or reuse the reviewer **evidence pack** from `references/selection.md`) and judge its `path:line` summary yourself.
2. **Dependency and tooling proportionality**: re-run `orch.py scope` (**Scope check before review**) on the diff after review fixes, and flag new dependencies, scripts, or abstractions that are disproportionately large for the problem solved.
3. **User-visible evidence**: when the change is user-visible, verify concrete visual or behavioral evidence (screenshots, recordings, or transcripts) scaled to `preferences.evidence` and repo `Evidence:`.
4. **Repo budgets and limits**: verify any size, bundle, performance, lint, or schema budgets the target repository defines.
5. **Maintainability**: confirm clean module boundaries and proportionate tests; `orch.py scope` lists stray scratch files (check debug leftovers yourself).

Send any follow-up fixes to the implementing child via the harness's continue mechanism (`references/harnesses.md`).

## Known infrastructure failures and unstarted PR checks

When a test run, CI check, or git/forge command fails, or a PR waits on required checks while none has started, classify the state before investigating code or waiting:

- **Infrastructure classification**: transient network refused/timeout to the forge or package registry, CI runner lost or preempted, or a signature matching `- Known failures:` in the target repo's `## Orchestration` (`references/repo-policy.md`).
- **One rerun or repo remedy**: run the remedy named in `- Known failures:` or rerun the failed jobs/command once (using the harness's CI rerun in `references/harnesses.md`, or one foreground retry of the failed command).
- **Escalate on repeat**: if the check fails a second time after the single rerun, treat it as a real failure and diagnose the logs and code.
- **Unstarted PR checks**: read the PR's merge state first (`gh pr view <n> --json mergeable,mergeStateStatus` on GitHub). On `CONFLICTING` or `DIRTY`, the forge cannot build the test merge commit, so `pull_request` workflows never start; send the implementing child a continue message to rebase onto the current base branch, revalidate, and push.
