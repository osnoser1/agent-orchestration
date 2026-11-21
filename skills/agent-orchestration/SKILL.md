---
name: agent-orchestration
description: Orchestrate coding work across subagents and models by complexity, risk, and provider quota. Use when delegating multi-step work, choosing a model or effort, or when a provider hits a quota limit.
license: MIT
compatibility: Any harness with subagents. Optional helper script requires Python 3.9+ (stdlib only).
metadata:
  version: "0.4.0"
---

# Agent orchestration

Act as the **orchestrator**: select the right worker, write a tight brief, supervise batches, enforce quality gates, and keep orchestrator spend low. Handle cheap checks yourself (read a small diff, run one key test) and delegate expensive work (implementation, deep research, large-diff summaries, review).

Read models only from discovery and two data files:
- `catalog.yaml` (next to this file): shared model facts (scores, cost class, strengths, quirks), merged with `catalog.local.yaml` next to the assets file when present.
- `~/.config/agent-orchestration/assets.yaml` (or `$AGENT_ORCHESTRATION_ASSETS`): per-user providers, quota status, preferences, and policy (`assets/assets.example.yaml`). When missing or stale, proceed with defaults (all discovered providers `ok`, policy `normal`), note that in one line, and offer to create it.

## 1. Discover (once per conversation)
List available `provider:model` pairs using your harness's discovery mechanism (`references/harnesses.md`) and intersect them with `catalog.yaml` (plus `catalog.local.yaml`). Read the target repo's `AGENTS.md` `## Orchestration` section when present (`references/repo-policy.md`). Run `python3 scripts/orch.py probe` when available to refresh `used_percent`, `window`, `resets_at`, clear expired lockouts, and surface `followups` (`references/selection.md`). For any required model missing from the catalog or `rating: unrated`, run the one-time research in `references/research-unrated.md` and record it locally.

*Done when*: every available `provider:model` pair is mapped to the catalog, repo policy (if present) is loaded, quota status and follow-ups are checked (or noted unverified), and any needed unrated model is recorded once.

## 2. Classify
Assign complexity (`trivial|standard|hard|frontier`), risk (`low|medium|high`, adding any repo `High risk:` triggers from `references/repo-policy.md`), UI/UX flag (`--ui`) or repo `Models:` preference, tool requirements (browser, device, network), proportional evidence and design-checkpoint needs (`references/verification.md`), delegation surface (`subagent` vs `conversation`, `references/harnesses.md`), and (for multi-item work) conflict-aware batch order (`references/supervision.md`) using `references/selection.md`.

*Done when*: complexity, risk, required tier, preferred strength/family, tool and evidence needs, delegation surface, and any multi-item batch order are stated before model selection.

## 3. Choose model + effort
Apply the filter, tie-break, effort knee, and preferred-model fallback rules (`preferences.ui_fallback`) in `references/selection.md` (or run `python3 scripts/orch.py select`). When holding a batch until a specific provider renews, record a `resume` follow-up and schedule its wakeup (`references/supervision.md`). Apply the chosen model's `quirks` from `catalog.yaml` and `references/harnesses.md`. Follow Step 7 before spending gated models/efforts/APIs or opening a new top-level conversation. Log the selection to the local decision ledger (`scripts/orch.py log --event select`, `references/improvement.md`). Models a plan names ahead are provisional: repeat this step for each role when its step starts, after a fresh `probe`.

*Done when*: a primary `provider / model @ effort`, delegation surface, and (for reviewed work) a reviewer from the fallback order in `references/selection.md` are selected at their step starts (or deferred to a provider renewal) with quirks noted and logged.

## 4. Brief
Send one outcome per worker on the chosen surface (`references/harnesses.md`, passing an idempotency key when supported per `references/supervision.md`) using `references/brief-template.md` (`GOAL`, `CONTEXT / KNOWN FACTS`, `CONSTRAINTS`, `VERIFICATION`, `REPORT`, `IF BLOCKED`, plus the doc review-loop rule when editing agent-facing docs). Name the active phase's skills or commands from the repo's `## Orchestration` section explicitly (`references/repo-policy.md`) and scale verification to user and repo evidence preferences (`references/verification.md`). When several children run for long, persist user-local durable state and start the supervision loop (`references/supervision.md`). Require every report to close with `## Obstacles / not verified`.

*Done when*: the brief carries all known facts, explicit repo phase skills/commands, proportional verification and checkpoint rules, doc review-loop instructions when applicable, and the mandatory closing section; multi-child runs have user-local durable state and an active supervision loop.

## 5. Quality gates
1. **Red→green evidence**: require failing repro/test output before the fix and passing output after, scaled to the user's and repo's evidence priorities (`references/verification.md`).
2. **Design checkpoints**: for user-visible taste decisions, apply the checkpoint gate in `references/verification.md` and record approved checkpoints in durable state (`references/supervision.md`).
3. **Cross-family review**: review with a different model family given the diff or a shared **evidence pack**; when no such model is usable, take the next step of the reviewer fallback order and note it in the report (`references/selection.md`). Send fixes back to the implementing child via the harness's continue mechanism (`references/harnesses.md`).
4. **Final check before landing**: verify the diff (delegating a large diff to a `cheap` subagent and judging its summary), one key test, dependency/tooling proportionality, user-visible evidence, repo budgets/limits, and maintainability (`references/verification.md`). Send follow-up fixes to the implementing child.
5. **Land**: follow the repo's `Land:` policy (`references/repo-policy.md`) or documented process (`AGENTS.md`/`CLAUDE.md`), advance any dependent batch waiting on these paths, and shrink or disable the supervision loop when the batch finishes (`references/supervision.md`).
6. **Log outcome**: append completion (`ok|failed|quota|stall|rework` + findings count) to the decision ledger (`scripts/orch.py log`, `references/improvement.md`).

*Done when*: proportional red→green evidence and any taste checkpoints are approved, review findings are resolved, the final landing check passes, the change lands, dependent batches or supervision wind-down run, and the outcome is logged.

## 6. Quota and failure handling
- **Quota error** (any child, implementer or reviewer): parse the reset time (`references/quota-errors.md`), persist `exhausted` to assets (`scripts/orch.py exhaust`, passing `--group` or `--model` when the provider uses quota groups), log `--outcome quota` (`scripts/orch.py log`), and follow the single decision order in `references/harnesses.md` (switch model in place → start a new child with partial results → schedule a resume at `earliest_reset + 2m` when no usable candidate remains). Inform the user in one line.
- **Known infrastructure failures**: classify transient forge/network errors, lost runners, or repo `Known failures:` patterns and rerun failed jobs once before investigating (`references/verification.md`).
- **Harness quirks or stalls**: on each supervision check or wakeup (`references/supervision.md`), diagnose idle children with pending work and continue them using idempotency keys where supported (`references/harnesses.md`, `catalog.yaml` `quirks`); log `stall` outcomes to the ledger.
- **Manual fallback**: when the user reports facts probes cannot see (plan cancelled, renewal date, manual reset, evidence or checkpoint preferences), update assets per `references/assets-updates.md`.

*Done when*: exhausted providers or quota groups carry reset times in assets, expired lockouts clear, transient infra failures get one rerun before investigation, interrupted or idle work resumes or schedules a post-reset wakeup (`kind: resume` in `followups`), and outcomes are logged.

## 7. Decide vs ask
Execute routine orchestration choices (including in-place model switches, same-conversation scheduled resumes, recurring supervision checks, single reruns of known infrastructure failures, and delegated design checkpoints when `preferences.design_checkpoints: delegate`) and report them briefly. Ask the user once before destructive actions, publishing, spending emergency models/efforts or pay-per-use APIs, opening a new top-level conversation when `preferences.new_conversations: ask`, proceeding when `preferences.ui_fallback: ask`, approving a taste checkpoint when `preferences.design_checkpoints: ask` (default), or resolving ambiguity that changes the outcome.

*Done when*: routine decisions are reported in-flight and every listed high-impact or taste gate waits for one explicit approval.

## References (load on demand)
- `references/selection.md` — tier floors, risk triggers, effort knee, tie-break order, UI/UX and preferred-family fallback (`ui_fallback`), follow-up kinds and due rule, reviewer timing and fallback order, review gate.
- `references/supervision.md` — conflict-aware batching, deferred launch on provider renewal, user-local durable state, recurring supervision loop, and idempotent child messages.
- `references/verification.md` — proportional evidence (`evidence`), design checkpoints (`design_checkpoints`), final check before landing, and single rerun for known infrastructure failures.
- `references/repo-policy.md` — target-repo `## Orchestration` contract, precedence, and explicit brief forwarding.
- `references/harnesses.md` — delegation surface (`subagent` vs `conversation`), in-place model switching and scheduled resume on quota exhaustion, and alphabetical harness matrix.
- `references/brief-template.md` — delegation brief layout, doc review-loop rule, and mandatory report sections.
- `references/quota-errors.md` — automatic probe thresholds, exit codes, and error-message parsing.
- `references/assets-updates.md` — plain-language fallback updates for `assets.yaml`.
- `references/research-unrated.md` — one-time scoring workflow for unrated models.
- `references/improvement.md` — decision ledger (`ledger.jsonl`), upstream retro findings, and skill update routine.
