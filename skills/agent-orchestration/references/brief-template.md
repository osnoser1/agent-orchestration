# Brief template

One outcome per child. Supply known facts directly so children skip re-discovery; give reviewers the raw diff (or shared evidence pack) alone so judgments stay independent. When starting a new child after a quota error (because the harness cannot switch the existing child's model in place; `references/harnesses.md`), include the partial work completed so far.

```
GOAL: <one-sentence outcome>
PHASE: <only for work staged under preferences.below_tier: stage (references/selection.md): "Phase 1: read the code, plan the approach, write the failing tests (red); stop and report before the change itself" or "Phase 2: the change, destructive steps, and review">
CONTEXT / KNOWN FACTS: <exact paths, findings, decisions already made>
CONSTRAINTS: <read target repo AGENTS.md/CLAUDE.md first; name this phase's repo skill/command from `## Orchestration` explicitly when present (e.g. "use the create-pr skill", "land with bun run land <pr>"); scope boundaries; size budget (expected files and lines) and "no new dependency unless the brief allows one" when the task builds tooling, scripts, or infrastructure (`references/verification.md`); design checkpoint trigger if user-visible taste decision (`references/verification.md`); doc review-loop (max 3 passes) if editing agent-facing docs; on a non-notifying surface (`references/supervision.md`) where the child can reach the orchestrator (the `Continue` tool in `references/harnesses.md`; supply the orchestrator's conversation ID), message it at each milestone: PR opened, PR landed or ejected from a landing queue, blocked, finished>
WAITING: one wait, one turn. For any wait over a minute, use <the orchestrator pastes the wait from the Wait column of `references/harnesses.md` for this child's harness, e.g. `t3_background_wait` in T3 Code> or one blocking command with a long timeout; done when the awaited command has exited and you have read its output. Every poll is a full model turn that carries the whole conversation and spends quota; when you must re-check, sleep at least 30 s between checks.
VERIFICATION: <red→green: failing test/repro before the fix, passing after; exact commands + expected evidence scaled to user `preferences.evidence` and repo `Evidence:` (`references/verification.md`)>
REPORT (short): what changed · exact command + output/visual evidence · doc review-loop passes when agent-facing docs changed · commit/PR (link back to originating conversation if launched as a new top-level conversation) · closing `## Obstacles / not verified` section
IF BLOCKED: report the blocker and stop before destructive or irreversible actions.
```

A child's report is for the orchestrator: relay to the user only the one-line summary per `SKILL.md` Step 7 (**Reporting to the user**); the details go in the PR body or report.

When a child creates or edits agent-facing docs (`AGENTS.md`, `CLAUDE.md`, skills, or references), require it to run the target repo's writing guide (or `writing-for-agents` when none exists) as a review → fix → re-review loop capped at 3 passes, report the rounds, and list any remaining findings under `## Obstacles / not verified`. Every child report ends with `## Obstacles / not verified` listing skipped checks, missing tools, unverified branches, residual doc-review findings, or `"None"`. The independent review then repeats the loop (`references/verification.md`).
