---
name: agent-orchestration
description: Orchestrate coding work across subagents and models by complexity, risk, and provider quota. Use when delegating multi-step work, choosing a model or effort, or when a provider hits a quota limit.
license: MIT
compatibility: Any harness with subagents. Optional helper script requires Python 3.9+ (stdlib only).
metadata:
  version: "0.2.0"
---

# Agent orchestration

Act as the **orchestrator**: select the right worker, write a tight brief, enforce quality gates, and keep orchestrator spend low. Handle cheap checks yourself (read a diff, run one key test) and delegate expensive work (implementation, deep research, review).

Read models only from discovery and two data files:
- `catalog.yaml` (next to this file): shared model facts (scores, cost class, strengths, quirks), merged with `catalog.local.yaml` next to the assets file when present.
- `~/.config/agent-orchestration/assets.yaml` (or `$AGENT_ORCHESTRATION_ASSETS`): per-user providers, quota status, and policy (`assets/assets.example.yaml`). When missing or stale, proceed with defaults (all discovered providers `ok`, policy `normal`), note that in one line, and offer to create it.

## 1. Discover (once per conversation)
List available `provider:model` pairs using your harness's discovery mechanism (`references/harnesses.md`) and intersect them with `catalog.yaml` (plus `catalog.local.yaml`). Run `python3 scripts/orch.py probe` when available to refresh `used_percent`, `window`, `resets_at`, and clear expired lockouts automatically. For any required model missing from the catalog or `rating: unrated`, run the one-time research in `references/research-unrated.md` and record it locally.

*Done when*: every available `provider:model` pair is mapped to the catalog, quota status is probed (or noted unverified), and any needed unrated model is recorded once.

## 2. Classify
Assign complexity (`trivial|standard|hard|frontier`), risk (`low|medium|high`), UI/UX flag (`--ui` when the task is UI/UX design or review), and tool requirements (browser, device, network) using `references/selection.md`.

*Done when*: complexity, risk, required tier, UI/UX flag, and tool needs are stated before model selection.

## 3. Choose model + effort
Apply the filter, tie-break, effort knee, and UI/UX rules in `references/selection.md` (or run `python3 scripts/orch.py select`). Apply the chosen model's `quirks` from `catalog.yaml` and `references/harnesses.md`. Ask once before using emergency models, `max`/`ultra` efforts, or pay-per-use APIs.

*Done when*: a primary `provider / model @ effort` and, for reviewed work, a cross-family reviewer are selected with applicable quirks noted.

## 4. Brief
Send one outcome per child using `references/brief-template.md` (`GOAL`, `CONTEXT / KNOWN FACTS`, `CONSTRAINTS`, `VERIFICATION`, `REPORT`, `IF BLOCKED`). For children writing agent-facing docs, instruct them to follow the target repo's writing guide or `writing-for-agents` when none exists. Require every child report to close with `## Obstacles / not verified`.

*Done when*: the brief carries all known facts, exact verification commands, doc-writing rules when applicable, and the mandatory closing section.

## 5. Quality gates
1. **Red→green evidence**: require failing repro/test output before the fix and passing output after.
2. **Cross-family review**: review with a different model family given the diff or a shared **evidence pack** (`references/selection.md`). Send fixes back to the implementing child via the harness's continue mechanism (`references/harnesses.md`).
3. **Final cheap check**: read the diff and run one key test yourself; send follow-up fixes to the implementing child.
4. **Land**: follow the target repo's documented process (`AGENTS.md`/`CLAUDE.md`).

*Done when*: red→green output is checked, review findings are resolved by the implementer, your own diff + key-test check passes, and the change lands through the repo's process.

## 6. Quota and failure handling
- **Quota error**: parse the reset time (`references/quota-errors.md`), persist `exhausted` to assets (`scripts/orch.py exhaust`), switch to the next candidate, resend the same brief with partial results, and inform the user in one line.
- **Harness quirks or stalls**: follow the harness row in `references/harnesses.md` and `quirks` in `catalog.yaml`.
- **Manual fallback**: when the user reports facts probes cannot see (plan cancelled, renewal date, manual reset), update assets per `references/assets-updates.md`.

*Done when*: exhausted providers carry reset times in assets, expired lockouts are cleared, and interrupted work resumes on the next candidate.

## 7. Decide vs ask
Execute routine orchestration choices and report them briefly. Ask the user once before destructive actions, publishing, spending emergency models/efforts or pay-per-use APIs, or resolving ambiguity that changes the outcome.

*Done when*: routine decisions are reported in-flight and every listed high-impact action waits for one explicit approval.

## References (load on demand)
- `references/selection.md` — tier floors, risk triggers, effort knee, tie-break order, UI/UX routing, review gate.
- `references/harnesses.md` — alphabetical harness matrix (discover, delegate, continue, cancel, quota probe, quirks).
- `references/brief-template.md` — delegation brief layout and mandatory report sections.
- `references/quota-errors.md` — automatic probe thresholds, exit codes, and error-message parsing.
- `references/assets-updates.md` — plain-language fallback updates for `assets.yaml`.
- `references/research-unrated.md` — one-time scoring workflow for unrated models.
