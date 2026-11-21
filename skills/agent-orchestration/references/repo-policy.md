# Repository orchestration policy

A target repository may configure orchestration overrides in `.agents/orchestration.md` (the whole file is the policy; checked first) or in `AGENTS.md` under `## Orchestration` (optionally with a one-line pointer in `AGENTS.md` to `.agents/orchestration.md`). Every line is optional; absent policy or omitted lines fall back to skill defaults.

Run `python3 scripts/orch.py policy --check` (add `--repo <dir>` outside the repo) to validate the policy and follow its final `ok:` or `error:` line.

## Contract

Start the section (recommended) with the prose intro line `Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.` so agents without this skill understand what the section governs. Prose paragraph lines before the first `- Key: value` bullet are allowed; non-bullet lines after bullets start fail validation.

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: <free text with markdown links to skills/guides>
- Review: <free text with markdown links to review skills>
- PR: <free text with markdown links to PR skill>
- Land: <exact command or process with optional links>
- High risk: <glob> | source:<repo-relative path> | class:<short phrase> (; separated)
- Models: <task-type> → family:<catalog family> [(<note>)] (; separated)
- Conversations: <free text on opening new top-level conversations>
- Evidence: <free text with markdown links on verification evidence by change type>
- Checkpoints: <free text with markdown links on taste/design checkpoint triggers>
- Known failures: /<regex>/ => rerun | `<command>` (; separated)
```

### Typed fields

| Field | Grammar | Effect and validation (`orch.py policy --check`) |
|---|---|---|
| `Models` | `;`-separated `<task-type> → family:<catalog family>`, optional trailing `(<note>)` | `<catalog family>` must match a `family` in `catalog.yaml` (or `catalog.local.yaml`); relative links in `(<note>)` must resolve. Unmet preferences follow `references/selection.md`. |
| `High risk` | `;`-separated items: `<glob>` (`db/migrations/**`), `source:<repo-relative path>` (file defining risky paths), or `class:<short phrase>` (judgment class) | Additive to Step 2 (Classify) in `references/selection.md`. Globs must be repo-relative without spaces, commas, braces, or `..`; `source:` files must resolve inside the repo and exist. |
| `Known failures` | `;`-separated `/regex/ => <action>` where `<action>` is `rerun` or a command in backticks | Matched before code investigation (`references/verification.md`, `references/supervision.md`). Regex must compile (always case-insensitive; flags unsupported). `orch.py failure` loads these from `--repo` automatically. |

### Descriptive fields

`Implement`, `Review`, `PR`, `Land`, `Conversations`, `Evidence`, and `Checkpoints` accept free text with markdown links (`[label](path#anchor)`). `orch.py policy --check` verifies that every relative link resolves inside the repo and any linked `SKILL.md` file exists. Unknown keys, duplicate keys, nested/`+` bullets, and non-bullet lines after bullets start fail validation.

| Field | Effect |
|---|---|
| `Implement` | Skills or guides the implementing worker must follow (named explicitly in implementation briefs; may reference repo overlap tooling for `references/supervision.md`). |
| `Review` | Review skills or checklists the reviewer must apply (named explicitly in review briefs). |
| `PR` | Skill or workflow used to open or update pull requests (named explicitly in PR briefs). |
| `Land` | Exact command or process used in Step 5 (Land). |
| `Conversations` | Repo guidance for opening new top-level conversations (`references/harnesses.md`). |
| `Evidence` | Verification evidence expected by change type (`references/verification.md`), scaled by `preferences.evidence` in `assets.yaml` (Precedence 2). |
| `Checkpoints` | User-visible surfaces or taste choices that trigger a design checkpoint (`references/verification.md`). |

## Legacy compatibility (temporary, issue #19)

New installs accept only the typed format; legacy needs the compat flag (issue #19). On an `error:` from `orch.py policy`, the orchestrator proceeds with skill defaults for the invalid field(s) and tells the user once (converting the repo policy is the user's call). The user may temporarily set `compat: {legacy_orchestration_policy: true}` in `assets.yaml` (kept off in `assets/assets.example.yaml`) to parse legacy free text best-effort with `warn:` lines referencing issue #19; legacy `Known failures` remain display-only and are not matched by `orch.py failure`.

## Precedence

1. **User's explicit instruction** — overrides everything for the current request.
2. **User's `assets.yaml` and harness rules** — keep authority over quota limits (`low_threshold`, `exhausted_threshold`, `exhausted_until`), `preferences.ui_fallback` when a preferred family is exhausted, `preferences.below_tier` when every tier-meeting model is out of quota (`references/selection.md`), `preferences.evidence` (user-wide evidence priorities and deprioritized checks), `pay_per_use`, `policy: avoid`, and the ask-once gates in `SKILL.md` Step 7 (`preferences.new_conversations`, `preferences.design_checkpoints`, emergency spending). Assets and harness rules win on these, except new top-level conversations, where the stricter of `preferences.new_conversations` and the repo's `Conversations:` line applies.
3. **Repository policy (`.agents/orchestration.md` or `AGENTS.md` `## Orchestration`)** — overrides skill defaults for process (phase skills/commands), extra high-risk triggers, task-type model preferences (`Models:`), repo evidence expectations (`Evidence:`), checkpoint triggers (`Checkpoints:`), and known infrastructure remedies (`Known failures:`).
4. **Skill defaults** — apply for any omitted line or when no policy is present.

## Applying policy in briefs

Load repo policy via `python3 scripts/orch.py policy` during Step 1 (Discover) — or read `.agents/orchestration.md` / `AGENTS.md` directly when Python is unavailable. In Step 4 (Brief), name the active phase's skills, commands, evidence requirements, and checkpoint triggers explicitly in `CONSTRAINTS` and `VERIFICATION` (for example, `"follow the implement-feature skill"`, `"use the open-pr skill"`, or `"land with make land PR=<n>"`) in addition to the general `AGENTS.md` read.

## Example

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: [implement-feature](.agents/skills/implement-feature/SKILL.md) per [repo guide](docs/guide.md)
- Review: [code-review](.agents/skills/code-review/SKILL.md)
- PR: [open-pr](.agents/skills/open-pr/SKILL.md)
- Land: `make land PR=<n>` per [release guide](docs/release.md#land)
- High risk: source:policy/risky-paths.txt; db/migrations/**; class:payments; class:auth
- Models: UI/UX work → family:anthropic; SQL migrations → family:openai
- Conversations: cross-repo follow-ups may open a new thread when user permits
- Evidence: UI changes → screenshots; API changes → red→green integration tests
- Checkpoints: new screens and public CLI flags
- Known failures: /forge (?:502|503)|lost runner/ => rerun; /lockfile corrupt/ => `rm -rf node_modules && npm ci`
```
