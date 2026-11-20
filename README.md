# agent-orchestration

An [Agent Skill](https://agentskills.io/specification) that turns a coding-agent conversation into a **quota-aware orchestrator**: it routes work to subagents and models by task complexity, risk, and live provider quota, and enforces quality gates (red→green evidence, cross-family review, and a cheap final check by the orchestrator).

Works in multi-provider harnesses and inside single-provider harnesses. Models are read from harness discovery and `catalog.yaml`.

## How it is organized

| File | What | Changes |
|---|---|---|
| `skills/agent-orchestration/SKILL.md` | Process steps with completion criteria: discover → classify → choose → brief → gates → land | rarely |
| `skills/agent-orchestration/catalog.yaml` | Shared model facts: Artificial Analysis scores per effort, cost class, strengths, quirks | PRs |
| `~/.config/agent-orchestration/assets.yaml` | **Your** providers, probe config, policy, and live quota state (outside the repo; template: `skills/agent-orchestration/assets/assets.example.yaml`) | automatic probe / you |
| `skills/agent-orchestration/references/` | Selection & fallback rules, repo policy contract, delegation surface & harness matrix, brief template, quota probe/error rules, unrated-model research, improvement loop | rarely |
| `skills/agent-orchestration/scripts/orch.py` | **Optional**, stdlib-only Python 3.9+ helper (`probe`, `select`, `followups`, `log`, `exhaust`, `status`, `check`) | rarely |

Selection summary (authoritative rules in `skills/agent-orchestration/references/selection.md`): complexity × risk → tier floor → filter out pay-per-use, exhausted, emergency, unrated, and below-floor models → rank by UI/UX strength (`--ui`) or repo family preference (`--prefer-family`), quota health, policy (`ending` > `spend-first` > `normal` > `preserve`), sooner reset/renewal, lower cost class, and lower effort (with `<3`-point effort knee) → reviewer from a different model family. When a preferred UI/UX or family model is offered here but exhausted, `preferences.ui_fallback` (`proceed|defer|ask`, default `proceed`) either marks the work provisional and queues a follow-up review in `assets.yaml`, recommends deferring until reset, or asks once; when the preferred family is not offered by the current harness, selection proceeds with the best available model, emits one informational note, and queues no follow-up.

Emergency models, `max`/`ultra` efforts, and pay-per-use APIs require one explicit approval. A missing or stale assets file falls back to defaults (all discovered providers `ok`, policy `normal`) and reports that in one line.

## Install

Use the current [Node.js LTS](https://nodejs.org/en/about/previous-releases) for the installer, which itself needs Node.js 22.20 or later ([`skills`](https://github.com/vercel-labs/skills)).

```bash
npx skills add osnoser1/agent-orchestration --skill agent-orchestration -g
npx skills update agent-orchestration
npx skills remove agent-orchestration
```

For project-level installs, omit `-g`.

| Harness | User-level path | Native install option | Config-dir override |
|---|---|---|---|
| Antigravity | `~/.gemini/antigravity/skills/` (or `~/.agents/skills/`) | Copy `skills/agent-orchestration/` or pass `-a antigravity` | — (project `.agents/skills/`) |
| Claude Code | `~/.claude/skills/` | `/plugin marketplace add osnoser1/agent-orchestration` then `/plugin install agent-orchestration@agent-orchestration` | `CLAUDE_CONFIG_DIR` (symlink `~/.agents/skills/agent-orchestration` into `$CLAUDE_CONFIG_DIR/skills/`) |
| Codex | `~/.agents/skills/` | `codex plugin marketplace add osnoser1/agent-orchestration` then `codex plugin add agent-orchestration@agent-orchestration` (also ships `agents/openai.yaml`) | `CODEX_HOME` |
| Gemini CLI | `~/.gemini/skills/` (or `~/.agents/skills/`) | `gemini skills install` or pass `-a gemini-cli` | — (project `.gemini/skills/`) |
| T3 Code | Inherits linked provider harness skill paths | Uses each linked provider's skill install | Per-provider (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) |
| Others via `skills` | Harness-specific path managed by `npx skills` | `npx skills add osnoser1/agent-orchestration --skill agent-orchestration -g -a <harness>` | Per-harness |

## Automatic quota tracking

Copy the example assets file (override the path with `$AGENT_ORCHESTRATION_ASSETS`):

```bash
mkdir -p ~/.config/agent-orchestration
curl -fsSL https://raw.githubusercontent.com/osnoser1/agent-orchestration/main/skills/agent-orchestration/assets/assets.example.yaml \
  -o ~/.config/agent-orchestration/assets.yaml
```

During discovery, `orch.py probe` updates `used_percent`, `window`, and `resets_at` automatically, marks providers `low` at `>=85%` (configurable via `preferences.low_threshold`) and `exhausted` at `100%`, and clears expired lockouts back to `ok`:

- **Antigravity**: queries `agy -p "/usage" --output-format json < /dev/null` (zero-token cost; requires one-time sign-in in the interactive `agy` TUI) and persists per-group quotas under `groups:` so an exhausted model group excludes only its models; otherwise reports `unverified`.
- **Claude**: calls `GET https://api.anthropic.com/api/oauth/usage` per configured directory (`~/.claude` and any `$CLAUDE_CONFIG_DIR` via `probe: {kind: claude, config_dir: ...}`). Credentials stay local and are omitted from logs and `assets.yaml`; missing tokens or HTTP 401 report `unverified`.
- **Codex**: scans today + yesterday `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` `token_count` events (`payload.rate_limits.{primary,secondary}`), ignoring expired windows (`resets_at <= now`).

When a child hits a quota error mid-run, the skill parses the reset time (`orch.py exhaust`), switches to the next candidate, and resends the brief with partial results.

*Manual fallback*: for facts no tool exposes (cancelling a plan, renewal dates, manual resets), tell the agent in plain language (e.g., `"I cancelled Codex, ends Nov 30"` or `"Claude reset"`); see `references/assets-updates.md`.

## Optional helper

```bash
python3 skills/agent-orchestration/scripts/orch.py probe
python3 skills/agent-orchestration/scripts/orch.py select --complexity hard --risk high \
  --available antigravity:gemini-4-neon,claude:claude-opus-5-5,codex:gpt-6-2-sol
python3 skills/agent-orchestration/scripts/orch.py select --complexity standard --risk low --ui --ref "PR #42" \
  --available claude:claude-sonnet-5-5,antigravity:gemini-4-neon
python3 skills/agent-orchestration/scripts/orch.py followups --due
python3 skills/agent-orchestration/scripts/orch.py log --complexity hard --risk high \
  --provider antigravity --model gemini-4-neon --effort high --outcome ok --findings 1 --repo my-app
python3 skills/agent-orchestration/scripts/orch.py exhaust antigravity "Individual quota reached… Resets in 4h29m1s"
python3 skills/agent-orchestration/scripts/orch.py status claude ok
python3 skills/agent-orchestration/scripts/orch.py check
```

Exit codes: `0` ok · `1` error · `2` no tier match · `3` blocked by pre-flight threshold or policy gate (`low` ≥85%, gated, or `ui_fallback: defer|ask`) · `4` quota hit (`exhausted` / 100%).

## Extending per repository

All extension points live at two layers:

1. **Per-repository policy (`AGENTS.md` `## Orchestration`)** — add an optional section to any target repository's `AGENTS.md` (reached by Claude Code via `CLAUDE.md` and directly by other harnesses) to override process defaults, extra high-risk paths, model preferences, and conversation guidance. Every line is optional; absent sections use defaults. See `skills/agent-orchestration/references/repo-policy.md`.

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: <skill names or guide>
- Review: <skill names>
- PR: <skill name>
- Land: <command or process>
- High risk: <extra paths/areas that count as high risk>
- Models: <task-type preferences, e.g. UI/UX work → Anthropic>
- Conversations: <when new top-level conversations are preferred or restricted>
```

2. **Per-user configuration (outside git)** — lives in `~/.config/agent-orchestration/` (or beside `$AGENT_ORCHESTRATION_ASSETS`):
   - `assets.yaml`: your providers, probe config, policies, live quota state, `preferences.ui_fallback` (`proceed|defer|ask`), `preferences.new_conversations` (`ask|allow`), `preferences.tier_floors`, and deferred `followups`.
   - `catalog.local.yaml`: local model ratings or overrides merged on top of `catalog.yaml`.

**Precedence**: explicit user instruction > `assets.yaml` (quota, pay-per-use, emergency spending, conversation authorization) and harness rules > target repo `## Orchestration` (phase skills/commands, high-risk paths, model preferences) > skill defaults.

## Improving the skill

Consumer repositories feed orchestration findings back upstream rather than patching installed skill copies locally (`skills/agent-orchestration/references/improvement.md`):

- **Decision ledger**: `orch.py log` appends one structured JSON line per delegation (selection + completion) to `ledger.jsonl` next to `assets.yaml` — timestamp, repo basename, delegation surface (`subagent|conversation`), task class, chosen model/effort, reviewer, outcome (`ok|failed|quota|stall|rework`), and review findings count. Prompts, code, secrets, full paths, and personal data are never recorded.
- **Retrospective findings**: during a repo's retrospective (`retro` / `retro-dispatch`), file an upstream issue on `osnoser1/agent-orchestration` using `.github/ISSUE_TEMPLATE/retro-finding.md` with ledger evidence for wrong model/effort choices, quota surprises, stalled children, brief gaps, or missing harness quirks. Submit new or updated model ratings as a PR editing `skills/agent-orchestration/catalog.yaml` with `orch.py check` passing.
- **Keeping installs current**: check and update at the start of every retrospective (or weekly):
  - Global install: `npx skills check -g` then `npx skills update agent-orchestration -g` (add `-y` for non-interactive runs).
  - Project install: `npx skills check -p` then `npx skills update agent-orchestration -p`.
  - `orch.py check` also performs an offline staleness check on local skill lockfiles (`~/.agents/.skill-lock.json` and `./skills-lock.json`) and suggests updating when an install is older than 7 days.

## Contributing

To add or re-rate models, edit `skills/agent-orchestration/catalog.yaml` (scores per effort, `source`, retrieval date, `confidence`; `rating: unrated` when data is unavailable), run `python3 skills/agent-orchestration/scripts/orch.py check`, and open a PR. Keep personal quota data, emails, and private project names out of commits.

## Standards followed

- [Agent Skills specification](https://agentskills.io/specification) (validated with `skills-ref`)
- [`skills` installer layout](https://github.com/vercel-labs/skills/blob/main/docs/creating-skills.md) (`skills/<name>/SKILL.md`)
- [Claude Code plugin marketplace format](https://code.claude.com/docs/en/plugin-marketplaces) (`.claude-plugin/marketplace.json`)
- Codex/agent plugin marketplace and UI metadata (`.agents/plugins/marketplace.json`, `skills/agent-orchestration/agents/openai.yaml`)

MIT licensed.
