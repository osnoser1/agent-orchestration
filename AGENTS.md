# AGENTS.md

- Writing or reviewing any agent-facing text (`skills/*/SKILL.md`, `references/`, `catalog.yaml` comments, this file): load [writing-for-agents](.agents/skills/writing-for-agents/SKILL.md) and its `SKILL-MECHANICS.md` for skill frontmatter. Finish with a per-file self-review against it.
- Harness parity: give every supported harness equal weight in alphabetical order inside one shared table or list.
- Validate before pushing: `agentskills validate skills/agent-orchestration` (from `pip install skills-ref`), `python3 skills/agent-orchestration/scripts/orch.py check`, and `npx skills add . --list` (lists only `agent-orchestration`).
- Update vendored skills under `.agents/skills/` only with `npx skills update` (tracked in `skills-lock.json`).
