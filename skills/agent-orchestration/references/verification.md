# Verification, design checkpoints, and landing checks

Apply these gates across Step 4 (Brief), Step 5 (Quality gates), and Step 6 (Failure handling).

## Evidence proportional to user priorities

Scale verification effort and evidence collection to task risk and what the user cares about:

1. **Sources and precedence**: read persistent user priorities from `assets.yaml` (`preferences.evidence`) and repo expectations from `## Orchestration` `- Evidence:`, following the precedence order in `references/repo-policy.md`. When the user states an evidence priority in plain language, record it in `assets.yaml` (`references/assets-updates.md`).
2. **Scale and prune**: require the evidence the user and repo prioritize (such as red→green unit/integration output, or browser/device screenshots on visible changes), and drop evidence classes the user explicitly deprioritized (such as video recordings or secondary-platform captures on non-visual work) unless the task is specifically about that deprioritized area.
3. **Forward into briefs**: list the exact commands and required evidence in each child's `VERIFICATION` block (`references/brief-template.md`).

## Design checkpoints

For user-visible changes whose look, layout, copy, or interaction behavior is a matter of taste (plus any change matching `- Checkpoints:` in the repo's `## Orchestration`, `references/repo-policy.md`):

- **`preferences.design_checkpoints: ask` (default)**: pause once at the checkpoint (`SKILL.md` Step 7) with concrete evidence (screenshot, recording, rendered preview, or CLI transcript) for user approval before building dependent polish or landing.
- **`preferences.design_checkpoints: delegate`**: evaluate the concrete evidence yourself against repo standards, proceed immediately, and report the decision in one line. (When `preferences.design_checkpoints` is unset in `assets.yaml`, follow the repo's `- Checkpoints:` mode if specified, else `ask`.)
- **Persist approval**: record every approved or delegated checkpoint (decision + evidence reference) under **Approved checkpoints** in the user-local durable state (`references/supervision.md`) so post-compaction turns and follow-up batches reuse the recorded decision.

## Final check before landing (Step 5.4)

After cross-family review findings are resolved and before landing, verify five items:

1. **Diff and one key test**: read the diff and run one key verification command yourself. When a diff is too large to read cheaply in orchestrator context, delegate reading it to a `cheap` subagent (or reuse the reviewer **evidence pack** from `references/selection.md`) and judge its `path:line` summary yourself.
2. **Dependency and tooling proportionality**: inspect manifest, lockfile, and build/config changes; flag new dependencies, scripts, or abstractions that are disproportionately large for the problem solved.
3. **User-visible evidence**: when the change is user-visible, verify concrete visual or behavioral evidence (screenshots, recordings, or transcripts) scaled to `preferences.evidence` and repo `Evidence:`.
4. **Repo budgets and limits**: verify any size, bundle, performance, lint, or schema budgets the target repository defines.
5. **Maintainability**: confirm clean module boundaries, proportionate tests, and absence of stray scratch or debug files.

Send any follow-up fixes to the implementing child via the harness's continue mechanism (`references/harnesses.md`).

## Known infrastructure failures

When a test run, CI check, or git/forge command fails, classify the failure before investigating application code:

- **Infrastructure classification**: transient network refused/timeout to the forge or package registry, CI runner lost or preempted, or a signature matching `- Known failures:` in the target repo's `## Orchestration` (`references/repo-policy.md`).
- **One rerun or repo remedy**: run the remedy named in `- Known failures:` or rerun the failed jobs/command once (for example `ci_rerun` on the failing check in T3 Code, or one foreground retry of the failed command).
- **Escalate on repeat**: if the check fails a second time after the single rerun, treat it as a real failure and diagnose the logs and code.
