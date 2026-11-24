---
name: agent-orchestration
description: Orchestrate coding work across subagents and models by complexity, risk, and provider quota. Use when delegating multi-step work, choosing a model or effort, or when a provider hits a quota limit.
license: MIT
compatibility: Any harness with subagents. Optional helper script requires Python 3.9+ (stdlib only).
metadata:
  version: "0.23.1"
---

# Agent orchestration

Act as the **orchestrator** and keep your own spend low: handle cheap checks yourself (a small diff, one key test) and delegate expensive work (implementation, deep research, large-diff summaries, review).

Read models only from discovery and two data files:
- `catalog.yaml` (next to this file): shared model facts, merged with `catalog.local.yaml` next to `assets.yaml` when present.
- `~/.config/agent-orchestration/assets.yaml` (or `$AGENT_ORCHESTRATION_ASSETS`): per-user providers, quota status, preferences, and policy (`assets/assets.example.yaml`). When missing, proceed with defaults (`references/selection.md`), note it in one line, and offer to create it (`scripts/orch.py probe --init`).

Every `scripts/orch.py` command ends with one decision line (`action:`, `ok:`, or `error:`): follow it.

## 1. Discover (once per conversation)
List available `provider:model` pairs with your harness's discovery (`references/harnesses.md`) and intersect them with the catalog. Run `scripts/orch.py policy` (on `error:`, use defaults and tell the user once; `references/repo-policy.md`), `scripts/orch.py probe` (quota, expired lockouts, `followups`: `references/fallbacks.md`), and `scripts/orch.py check --available <provider:model,...>` (notes what this harness does not offer). Research any required model missing from the catalog or `rating: unrated` once (`references/research-unrated.md`).

*Done when*: every available `provider:model` pair is mapped to the catalog, repo policy (if present) is loaded, quota status and follow-ups are checked (or noted unverified), and any needed unrated model is recorded once.

## 2. Classify
Score the rubric in `references/selection.md` and run `scripts/orch.py classify` (repo `High risk:` triggers set `consequences: 2`). Note the `--ui` or repo `Models:` preference, tool needs, delegation surface (`references/harnesses.md`), and batching (`references/supervision.md`).

*Done when*: rubric (plus coordination score when an orchestrator conversation is planned), complexity, risk, tier, strength/family, tools, surface, and batch order are stated before model selection.

## 3. Choose model + effort
Run `scripts/orch.py select` (`--role implementer|reviewer|orchestrator`) and follow its final `action:` line; without Python, apply `references/selection.md`. Models a plan names ahead are tentative: run this step for each role when its step starts, after a fresh `probe`. Branches: unavailable preferred or tier-meeting model → `references/fallbacks.md`; a user, `select`, or `defer` hold → `references/supervision.md` (Deferred launch); reviewer → `references/review.md`; orchestrator → `references/orchestrator.md`. Apply model `quirks` (`catalog.yaml`, `references/harnesses.md`) and clear any Step 7 gate before dispatch. Log each selection with its `--role` (`scripts/orch.py log --event select`, `references/improvement.md`).

*Done when*: a primary `provider / model @ effort` (or staged Phase 1 + Phase 2 target), delegation surface, and (for reviewed work) a reviewer from `references/review.md` are selected at step start (held only when `select` says hold or ask, or the user holds work) with quirks noted and logged.

## 4. Brief
Send one outcome per worker on the chosen surface (`references/harnesses.md`), filling every applicable field of `references/brief-template.md`; after dispatch, verify requested vs effective model/effort (`references/harnesses.md`). For staged work and multi-item batches, persist user-local durable state. Start the supervision loop for non-notifying children, and act on each completion notice in the same turn (`references/supervision.md`).

*Done when*: every applicable brief-template field is filled (known facts, staged `PHASE`, explicit repo phase skills/commands, proportional verification and checkpoint rules, doc review loop, `STATUS`, closing `## Obstacles / not verified`); requested vs effective model/effort is verified, staged runs and multi-item batches have user-local durable state, and every non-notifying child has an active supervision loop.

## 5. Quality gates
Each gate's rules live in `references/verification.md` unless named otherwise.
1. **Red→green evidence**: require failing repro/test output before the fix and passing output after, scaled to the user's and repo's evidence priorities.
2. **Design checkpoints**: for user-visible taste decisions, apply the checkpoint gate.
3. **Cross-family review**: run `scripts/orch.py scope` first, then review with a different model family given the diff or a shared **evidence pack** (reviewer fallback order: `references/review.md`); agent-facing docs, including your own edits, get the doc review loop. Send fixes to the implementing child via the harness's continue mechanism (`references/harnesses.md`). On `rework`, important findings at the escalation threshold, or a child's `STATUS: ESCALATE`, run `scripts/orch.py escalate` and follow its `action:` line (`references/escalation.md`).
4. **Final check before landing**: run the pre-landing checklist (including `scripts/orch.py land-check` on `high` risk).
5. **Land**: follow repo policy `Land:` (`references/repo-policy.md`) or documented process (`AGENTS.md`/`CLAUDE.md`), advance any dependent batch waiting on these paths, and wind down the supervision loop when the batch finishes (`references/supervision.md`).
6. **Log outcome**: append completion (`ok|failed|quota|stall|rework` + findings count) to the decision ledger (`scripts/orch.py log`, `references/improvement.md`).

*Done when*: proportional red→green evidence and any taste checkpoints are approved, review findings are resolved, the pre-landing checklist passes, the change lands, dependent batches or supervision wind-down run, and the outcome is logged.

## 6. Quota and failure handling
- **Quota error**: run `scripts/orch.py exhaust` with selection arguments (reset parsing and scope in `references/quota-errors.md`), log `--outcome quota`, and follow its `action:` line through the recovery order in `references/harnesses.md`. For this conversation's own quota, pass `--self --coordination <0|1|2>` instead of the worker arguments (`references/orchestrator.md`).
- **Known infrastructure failures and unstarted PR checks**: apply the single-rerun and merge-state gates in `references/verification.md` before investigating or waiting.
- **Other child failures** (context or turn cap, tool or harness error, unknown): run `scripts/orch.py failure "<error text>" --ref <item>` (`references/supervision.md`).
- **Harness quirks or stalls**: follow the harness row in `references/harnesses.md`, `quirks` in `catalog.yaml`, and the supervision loop; log `stall` outcomes to the ledger.
- **Manual fallback**: when the user reports facts probes cannot see (plan cancelled, renewal date, manual reset, preferences), update assets per `references/assets-updates.md`.

*Done when*: exhausted scopes carry reset times in assets, expired lockouts clear, failures follow retry/rerun and merge-state rules, any quota or reviewer hold passes `scripts/orch.py defer`, interrupted or staged work switches in place or schedules a `resume` wakeup, and outcomes are logged.

## 7. Decide, ask, report
Execute routine choices yourself: in-place model switches, staged Phase 1 launches and phase-boundary switches, same-conversation scheduled resumes, supervision checks, single reruns of known infrastructure failures, delegated design checkpoints (`design_checkpoints: delegate`), and a `select` candidate from a `low` or `preserve` pool. Waiting for a preferred model to renew is not by itself a reason to hold or to ask: dispatch what its `action:` line names now and choose the reviewer when its step starts. Before deferring an item, asking whether to wait, or announcing a stop over quota or reviewer family, run `scripts/orch.py defer` and follow its `action:` line. Ask the user once before destructive or irreversible actions, publishing, spending emergency models/efforts, spending pay-per-use APIs under `preferences.pay_per_use: ask` (under `never`, keep them excluded), opening a new top-level conversation when `preferences.new_conversations: ask`, proceeding when `preferences.ui_fallback: ask`, approving a taste checkpoint when `preferences.design_checkpoints: ask` (default), or resolving ambiguity that changes the outcome.

**Reporting to the user**: one line per event: what finished, what is next, what the user must decide. Model, effort, quota, reviewer choice, and tables only when asked or when a decision depends on them; reviewer degradation and model details go in the PR body or report. Answer which-model, which-reviewer, and why questions from `orch.py history` and the plan file, not memory.

*Done when*: routine decisions are reported one line each and every listed high-impact or taste gate waits for one explicit approval.

## References (load on demand)
- `references/selection.md` — every run: rubric, tier floors, risk triggers, effort knee, filter and ranking.
- `references/fallbacks.md` — unavailable model: `ui_fallback`, `below_tier` staging, follow-ups.
- `references/review.md` — reviewer step: tier, fallback order, re-reviews, evidence pack.
- `references/orchestrator.md` — orchestrator or launch: ladder, own quota, `orch.py launch`.
- `references/escalation.md` — escalation: `rework`, findings, `STATUS: ESCALATE`.
- `references/supervision.md` — batches and children: batching, deferred launch, durable state, completion notices, supervision loop, failure retries, replacements.
- `references/verification.md` — quality gates: evidence, scope, doc review, design checkpoints, pre-landing, infra reruns.
- `references/harnesses.md` — harness: delegation surface, quota recovery order, requested-vs-effective check, command matrix.
- `references/brief-template.md` — brief: layout and report sections.
- `references/repo-policy.md` — repo policy: contract, precedence, brief forwarding.
- `references/quota-errors.md` — quota error: probe thresholds, exit codes, reset parsing.
- `references/assets-updates.md` — user-reported facts: plain-language `assets.yaml` edits.
- `references/research-unrated.md` — unrated model: one-time scoring.
- `references/improvement.md` — ledger and retros: logging, ladder calibration, upstream findings, updates.
