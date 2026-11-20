# agent-orchestration

An [Agent Skill](https://agentskills.io/specification) that turns any coding-agent conversation into a **quota-aware orchestrator**: it routes work to subagents and models by task complexity, risk and remaining provider quota, and enforces quality gates (test-first evidence, independent cross-family review, a cheap final check by the orchestrator).

Works in multi-provider harnesses (T3 Code `delegate_task`) and inside a single harness with its native models (Claude Code `Agent`, Codex subagents, Gemini/Antigravity). It never assumes a model exists: it discovers what the harness offers.

## How it is organized

| File | What | Changes |
|---|---|---|
| `skills/agent-orchestration/SKILL.md` | Short, stable process: classify → choose model+effort → brief → gates → land | rarely |
| `skills/agent-orchestration/catalog.yaml` | Shared model facts: score per effort (with source+date), cost class, strengths, quirks | PRs |
| `~/.config/agent-orchestration/assets.yaml` | **Your** providers, reset dates, policy, status. Lives outside the repo. Example: `skills/agent-orchestration/assets/assets.example.yaml` | you / the agent |
| `skills/agent-orchestration/references/` | Selection tables, brief template, quota-error parsing, harness notes | rarely |
| `skills/agent-orchestration/scripts/orch.py` | **Optional**, dependency-free Python 3 helper (`select`, `exhaust`, `status`, `check`) | rarely |

Selection in one line: required tier from complexity × risk → candidates = available in harness ∩ not exhausted ∩ not emergency ∩ not pay-per-use ∩ tier met → prefer quota that expires first (`ending` > `spend-first` > `normal` > `preserve`) → lowest effort that meets the tier → reviewer from a different model family (UI/UX review prefers Anthropic).

Emergency models/efforts (e.g. `max`, `ultra`, top-priced tiers) and pay-per-use API providers are never used without your approval. A missing or stale assets file never blocks: the skill falls back to defaults and tells you.

## Install

Use the current [Node.js LTS](https://nodejs.org/en/about/previous-releases) for the installer, which itself needs Node.js 22.20 or later ([`skills`](https://github.com/vercel-labs/skills), covers Claude Code, Codex, Gemini CLI, Antigravity, Cursor and 70+ others).

```bash
# Project-level (current repo)
npx skills add osnoser1/agent-orchestration
# User-level for Claude Code and Codex (recommended; add -a gemini-cli / -a antigravity as needed)
npx skills add osnoser1/agent-orchestration --skill agent-orchestration -g -a claude-code -a codex -y
# Update / list / remove
npx skills update agent-orchestration
npx skills list
npx skills remove agent-orchestration
```

Claude Code plugin marketplace (alternative):

```
/plugin marketplace add osnoser1/agent-orchestration
/plugin install agent-orchestration@agent-orchestration
/plugin marketplace update agent-orchestration     # update
/plugin uninstall agent-orchestration@agent-orchestration
```

Where it lands (user level): Codex reads `~/.agents/skills/` ([Codex skills docs](https://developers.openai.com/codex/skills)); Claude Code gets a symlink in `~/.claude/skills/`. If you run Claude Code with a custom `CLAUDE_CONFIG_DIR`, link it there too: `ln -s ~/.agents/skills/agent-orchestration "$CLAUDE_CONFIG_DIR/skills/"`.

Manual: copy `skills/agent-orchestration/` into your harness's skills directory (e.g. `~/.claude/skills/`, `~/.agents/skills/`).

## Your assets file

```bash
mkdir -p ~/.config/agent-orchestration
curl -fsSL https://raw.githubusercontent.com/osnoser1/agent-orchestration/main/skills/agent-orchestration/assets/assets.example.yaml \
  -o ~/.config/agent-orchestration/assets.yaml
```

Then just tell the agent in plain language: "Claude reset", "I cancelled X", "Antigravity is almost out", "Codex quota is use-it-or-lose-it". When a child hits a quota error (`You've hit your weekly limit · resets Nov 23, 3am …`, `Resets in 4h29m1s`, `try again at Nov 26th, 2026 8:01 AM`) the skill records the reset time and switches to the next best model, resending the same brief. Override the path with `$AGENT_ORCHESTRATION_ASSETS`.

## Optional helper

```bash
python3 skills/agent-orchestration/scripts/orch.py select --complexity hard --risk high \
  --available antigravity:gemini-4-neon,claude:claude-opus-5-5
python3 skills/agent-orchestration/scripts/orch.py exhaust antigravity "Individual quota reached… Resets in 4h29m1s"
python3 skills/agent-orchestration/scripts/orch.py check
```

## Contributing

New or re-rated models: edit `catalog.yaml` (score per effort, `source`, retrieval date, `confidence` if uncertain; mark `rating: unrated` if you have no data) and open a PR. Run `python3 skills/agent-orchestration/scripts/orch.py check`. Never commit personal quota data, emails or private project names.

## Standards followed

- [Agent Skills specification](https://agentskills.io/specification) (validated with the reference `skills-ref`)
- [`skills` installer layout](https://github.com/vercel-labs/skills/blob/main/docs/creating-skills.md) (`skills/<name>/SKILL.md`)
- [Claude Code plugin marketplace format](https://code.claude.com/docs/en/plugin-marketplaces) (`.claude-plugin/marketplace.json`, validated with `claude plugin validate`)

MIT licensed.
