# AGENTS.md

- Editing or reviewing agent-facing text (`skills/*/SKILL.md`, `references/`, `catalog.yaml`, `.agents/skills/`, this file): run [agent-doc-review-loop](.agents/skills/agent-doc-review-loop/SKILL.md).
- Harness parity: give every supported harness equal weight in alphabetical order inside one shared table or list.
- Validate before pushing: `agentskills validate skills/agent-orchestration` (from `pip install skills-ref`), `python3 skills/agent-orchestration/scripts/orch.py check`, and `npx skills add . --list` (lists only `agent-orchestration`).
- Update vendored skills under `.agents/skills/` only with `npx skills update` (tracked in `skills-lock.json`); mark repo-local maintainer skills under `.agents/skills/` with `metadata.internal: "true"` so `npx skills add . --list` exposes only `agent-orchestration`.
