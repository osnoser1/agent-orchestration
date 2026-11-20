# AGENTS.md

- Writing or reviewing any agent-facing text (`skills/*/SKILL.md`, `references/`, `catalog.yaml` comments, this file): load the [writing-for-agents](.agents/skills/writing-for-agents/SKILL.md) skill, and its `SKILL-MECHANICS.md` for skill frontmatter. Finish with a self-review of every changed file against it.
- Every harness is a peer: give each supported harness the same weight and order-neutral placement, side by side in one table or list.
- Validate before pushing: `agentskills validate skills/agent-orchestration` (from `pip install skills-ref`) and `python3 skills/agent-orchestration/scripts/orch.py check`.
- Vendored skills under `.agents/skills/` are tracked in `skills-lock.json`; update them only with `npx skills update`.
