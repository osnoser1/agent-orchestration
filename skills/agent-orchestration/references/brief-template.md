# Brief template

One outcome per child. Supply known facts directly so children skip re-discovery; give reviewers the raw diff (or shared evidence pack) alone so judgments stay independent. When starting a new child after a quota error (because the harness cannot switch the existing child's model in place; `references/harnesses.md`), include the partial work completed so far.

```
GOAL: <one-sentence outcome>
CONTEXT / KNOWN FACTS: <exact paths, findings, decisions already made>
CONSTRAINTS: <read target repo AGENTS.md/CLAUDE.md first; name this phase's repo skill/command from `## Orchestration` explicitly when present (e.g. "use the create-pr skill", "land with bun run land <pr>"); scope boundaries; design checkpoint trigger if user-visible taste decision (`references/verification.md`); doc review-loop (max 3 passes) if editing agent-facing docs>
VERIFICATION: <red→green: failing test/repro before the fix, passing after; exact commands + expected evidence scaled to user `preferences.evidence` and repo `Evidence:` (`references/verification.md`)>
REPORT (short): what changed · exact command + output/visual evidence · doc review-loop passes when agent-facing docs changed · commit/PR (link back to originating conversation if launched as a new top-level conversation) · closing `## Obstacles / not verified` section
IF BLOCKED: report the blocker and stop before destructive or irreversible actions.
```

When a child creates or edits agent-facing docs (`AGENTS.md`, `CLAUDE.md`, skills, or references), require it to run the target repo's writing guide (or `writing-for-agents` when none exists) as a review → fix → re-review loop capped at 3 passes, report the rounds, and list any remaining findings under `## Obstacles / not verified`. Every child report ends with `## Obstacles / not verified` listing skipped checks, missing tools, unverified branches, residual doc-review findings, or `"None"`.
