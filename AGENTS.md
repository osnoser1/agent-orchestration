# AGENTS.md

- Editing or reviewing agent-facing text (`skills/*/SKILL.md`, `references/`, `catalog.yaml`, `.agents/skills/`, this file): run [agent-doc-review-loop](.agents/skills/agent-doc-review-loop/SKILL.md).
- Harness parity: give every supported harness equal weight in alphabetical order inside one shared table or list; `tools/harnesses.json` is the single source, and `tools/repo.py check` enforces it on the README install table and the `references/harnesses.md` matrix.
- Validate before pushing (each exits 0): `agentskills validate skills/agent-orchestration` (from `pip install skills-ref`), `python3 skills/agent-orchestration/scripts/orch.py check`, `python3 tools/repo.py check` (add `--base origin/main` for the version-bump rule), and `npx skills add . --list` (lists only `agent-orchestration`).
- Update vendored skills under `.agents/skills/` only with `npx skills update` (tracked in `skills-lock.json`); mark repo-local maintainer skills under `.agents/skills/` with `metadata.internal: "true"` so `npx skills add . --list` exposes only `agent-orchestration`.
- Version and release: any change under `skills/agent-orchestration/` or `.claude-plugin/` runs `python3 tools/repo.py bump <X.Y.Z>`; from main, `python3 tools/repo.py release` dry-runs a release and `--publish` tags it and creates the GitHub release.
- Rules that must survive edits live in `tools/pinned-rules.json`; change a pinned rule and its entry together.
