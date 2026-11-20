# Brief template

One outcome per child. Supply known facts directly so children skip re-discovery; give reviewers the raw diff (or shared evidence pack) alone so judgments stay independent. When resending after a quota switch, include partial work completed so far.

```
GOAL: <one-sentence outcome>
CONTEXT / KNOWN FACTS: <exact paths, findings, decisions already made>
CONSTRAINTS: <read target repo AGENTS.md/CLAUDE.md first; name this phase's repo skill/command from `## Orchestration` explicitly when present (e.g. "use the create-pr skill", "land with bun run land <pr>"); scope boundaries; if writing agent-facing docs, follow target repo's writing guide or writing-for-agents when none exists>
VERIFICATION: <red→green: failing test/repro before the fix, passing after; exact commands + expected evidence>
REPORT (short): what changed · exact command + output evidence · commit/PR (link back to originating conversation if launched as a new top-level conversation) · closing `## Obstacles / not verified` section
IF BLOCKED: report the blocker and stop before destructive or irreversible actions.
```

Every child report ends with `## Obstacles / not verified` listing skipped checks, missing tools, unverified branches, or `"None"`.
