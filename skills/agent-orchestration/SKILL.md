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
List available `provider:model` pairs using your harness's discovery mechanism (`references/harnesses.md`) and intersect them with `catalog.yaml` (plus `catalog.local.yaml`). Read the target repo's `AGENTS.md` `## Orchestration` section when present (`references/repo-policy.md`). Run `python3 scripts/orch.py probe` when available to refresh `used_percent`, `window`, `resets_at`, clear expired lockouts, and surface `followups` (`references/selection.md`). For any required model missing from the catalog or `rating: unrated`, run the one-time research in `references/research-unrated.md` and record it locally.

*Done when*: every available `provider:model` pair is mapped to the catalog, repo policy (if present) is loaded, quota status and follow-ups are checked (or noted unverified), and any needed unrated model is recorded once.

## 2. Classify
Assign complexity (`trivial|standard|hard|frontier`), risk (`low|medium|high`, adding any repo `High risk:` triggers from `references/repo-policy.md`), UI/UX flag (`--ui`) or repo `Models:` preference, tool requirements (browser, device, network), and delegation surface (`subagent` vs `conversation`, `references/harnesses.md`) using `references/selection.md`.

*Done when*: complexity, risk, required tier, preferred strength/family, tool needs, and delegation surface are stated before model selection.

## 3. Choose model + effort
Apply the filter, tie-break, effort knee, and preferred-model fallback rules (`preferences.ui_fallback`) in `references/selection.md` (or run `python3 scripts/orch.py select`). Apply the chosen model's `quirks` from `catalog.yaml` and `references/harnesses.md`. Follow Step 7 before spending gated models/efforts/APIs or opening a new top-level conversation. Log the selection to the local decision ledger (`scripts/orch.py log --event select`, `references/improvement.md`). Models a plan names ahead are provisional: repeat this step for each role when its step starts, after a fresh `probe`.

*Done when*: a primary `provider / model @ effort`, delegation surface, and (for reviewed work) a reviewer from the fallback order in `references/selection.md` are selected at their step starts with quirks noted and logged.

## 4. Brief
Send one outcome per worker on the chosen surface (`references/harnesses.md`) using `references/brief-template.md` (`GOAL`, `CONTEXT / KNOWN FACTS`, `CONSTRAINTS`, `VERIFICATION`, `REPORT`, `IF BLOCKED`). Name the active phase's skills or commands from the repo's `## Orchestration` section explicitly in the brief (`references/repo-policy.md`). For workers writing agent-facing docs, instruct them to follow the target repo's writing guide or `writing-for-agents` when none exists. Require every report to close with `## Obstacles / not verified`.

*Done when*: the brief carries all known facts, explicit repo phase skills/commands, exact verification commands, doc-writing rules when applicable, and the mandatory closing section.

## 5. Quality gates
1. **Red→green evidence**: require failing repro/test output before the fix and passing output after.
2. **Cross-family review**: review with a different model family given the diff or a shared **evidence pack**; when no such model is usable, take the next step of the reviewer fallback order and note it in the report (`references/selection.md`). Send fixes back to the implementing child via the harness's continue mechanism (`references/harnesses.md`).
3. **Final cheap check**: read the diff and run one key test yourself; send follow-up fixes to the implementing child.
4. **Land**: follow the repo's `Land:` policy (`references/repo-policy.md`) or documented process (`AGENTS.md`/`CLAUDE.md`).
5. **Log outcome**: append completion (`ok|failed|quota|stall|rework` + findings count) to the decision ledger (`scripts/orch.py log`, `references/improvement.md`).

*Done when*: red→green output is checked, review findings are resolved by the implementer, your own diff + key-test check passes, the change lands through the repo's process, and the outcome is logged.

## 6. Quota and failure handling
- **Quota error** (any child, implementer or reviewer): parse the reset time (`references/quota-errors.md`), persist `exhausted` to assets (`scripts/orch.py exhaust`, passing `--group` or `--model` when the provider uses quota groups), log `--outcome quota` (`scripts/orch.py log`), and follow the single decision order in `references/harnesses.md` (switch model in place → start a new child with partial results → schedule a resume at `earliest_reset + 2m` when no usable candidate remains). Inform the user in one line.
- **Harness quirks or stalls**: follow the harness row in `references/harnesses.md` and `quirks` in `catalog.yaml`; log `stall` outcomes to the ledger.
- **Manual fallback**: when the user reports facts probes cannot see (plan cancelled, renewal date, manual reset), update assets per `references/assets-updates.md`.

*Done when*: exhausted providers or quota groups carry reset times in assets, expired lockouts are cleared, interrupted work either resumes on the next candidate (in place when supported, else in a new child with partial results) or is recorded and scheduled to resume after the earliest reset (`kind: resume` in `followups`), and `quota` plus continuation outcomes are logged.

## 7. Decide vs ask
Execute routine orchestration choices (including in-place model switches and same-conversation scheduled resumes) and report them briefly. Ask the user once before destructive actions, publishing, spending emergency models/efforts or pay-per-use APIs, opening a new top-level conversation when `preferences.new_conversations: ask`, proceeding when `preferences.ui_fallback: ask`, or resolving ambiguity that changes the outcome.

*Done when*: routine decisions are reported in-flight and every listed high-impact action waits for one explicit approval.

## References (load on demand)
- `references/selection.md` — tier floors, risk triggers, effort knee, tie-break order, UI/UX and preferred-family fallback (`ui_fallback`), follow-up kinds and due rule, reviewer timing and fallback order, review gate.
- `references/repo-policy.md` — target-repo `## Orchestration` contract, precedence, and explicit brief forwarding.
- `references/harnesses.md` — delegation surface (`subagent` vs `conversation`), in-place model switching and scheduled resume on quota exhaustion, and alphabetical harness matrix.
- `references/brief-template.md` — delegation brief layout and mandatory report sections.
- `references/quota-errors.md` — automatic probe thresholds, exit codes, and error-message parsing.
- `references/assets-updates.md` — plain-language fallback updates for `assets.yaml`.
- `references/research-unrated.md` — one-time scoring workflow for unrated models.
- `references/improvement.md` — decision ledger (`ledger.jsonl`), upstream retro findings, and skill update routine.
