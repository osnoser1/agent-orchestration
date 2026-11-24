---
name: agent-orchestration
description: Orchestrate coding work across subagents and models by complexity, risk, and provider quota. Use when delegating multi-step work, choosing a model or effort, or when a provider hits a quota limit.
license: MIT
compatibility: Any harness with subagents. Optional helper script requires Python 3.9+ (stdlib only).
metadata:
  version: "0.21.0"
---

# Agent orchestration

Act as the **orchestrator**: select the right worker, write a tight brief, supervise batches, enforce quality gates, and keep orchestrator spend low. Handle cheap checks yourself (a small diff, one key test) and delegate expensive work (implementation, deep research, large-diff summaries, review).

Read models only from discovery and two data files:
- `catalog.yaml` (next to this file): shared model facts, merged with `catalog.local.yaml` next to `assets.yaml` when present.
- `~/.config/agent-orchestration/assets.yaml` (or `$AGENT_ORCHESTRATION_ASSETS`): per-user providers, quota status, preferences, and policy (`assets/assets.example.yaml`). When missing, proceed with defaults (all discovered providers `ok`, policy `normal`), note it in one line, and offer to create it (`scripts/orch.py probe --init`).

## 1. Discover (once per conversation)
List available `provider:model` pairs using your harness's discovery mechanism (`references/harnesses.md`) and intersect them with `catalog.yaml` (plus `catalog.local.yaml`). Run `scripts/orch.py policy` for repo policy; on `error:`, use defaults and tell the user once (`references/repo-policy.md`). Run `scripts/orch.py probe` to refresh quota state, clear expired lockouts, and surface `followups` (`references/selection.md`); `scripts/orch.py check --available <provider:model,...>` notes assets, local models, and follow-ups this harness does not offer. For any required model missing from the catalog or `rating: unrated`, run the one-time research in `references/research-unrated.md` and record it locally.

*Done when*: every available `provider:model` pair is mapped to the catalog, repo policy (if present) is loaded, quota status and follow-ups are checked (or noted unverified), and any needed unrated model is recorded once.

## 2. Classify
Score the rubric in `references/selection.md` and run `scripts/orch.py classify` (repo `High risk:` triggers set `consequences: 2`). Note `--ui` or repo `Models:` preference, tool needs, delegation surface (`references/harnesses.md`), and batching (`references/supervision.md`).

*Done when*: rubric (plus coordination score when an orchestrator conversation is planned), complexity, risk, tier, strength/family, tools, surface, and batch order are stated before model selection.

## 3. Choose model + effort
Run `scripts/orch.py select` (`--role implementer|reviewer|orchestrator`, `references/selection.md`) and follow its final `action:` line. Without Python, apply `references/selection.md`. When the user holds work (`scripts/orch.py defer --ref <item> --user-hold --needs provider:<id>/<tier>`) or `select` stages or holds it, schedule the recorded `resume` follow-up (`references/selection.md`, `references/supervision.md`). Apply model `quirks` (`catalog.yaml`, `references/harnesses.md`). Follow Step 7 before spending gated models/efforts/APIs, opening a new top-level conversation, or proceeding under `preferences.ui_fallback: ask`. Log each selection with its `--role` (`scripts/orch.py log --event select`, `references/improvement.md`). Models a plan names ahead are tentative: repeat this step for each role when its step starts, after a fresh `probe`.

*Done when*: a primary `provider / model @ effort` (or staged Phase 1 + Phase 2 target), delegation surface, and (for reviewed work) a reviewer from `references/selection.md` are selected at step start (held only when `select` says hold or ask, or the user holds work) with quirks noted and logged.

## 4. Brief
Send one outcome per worker on the chosen surface (`references/harnesses.md`) using `references/brief-template.md`; after dispatch, verify requested vs effective model/effort (`references/orchestrator-launch.md`). Name the active phase's skills or commands from repo policy (`references/repo-policy.md`) explicitly and include proportional evidence and checkpoint triggers (`references/verification.md`). For staged work (`preferences.below_tier: stage`, `references/selection.md`) and multi-item batches, persist user-local durable state. Start the supervision loop for non-notifying children, and act on each completion notice in the same turn (`references/supervision.md`). Require every report to carry `STATUS:` and close with `## Obstacles / not verified`.

*Done when*: the brief carries all known facts, any staged `PHASE` boundary, explicit repo phase skills/commands, proportional verification and checkpoint rules, doc review-loop instructions when applicable, `STATUS`, and the closing section; requested vs effective model/effort is verified, staged runs and multi-item batches have user-local durable state, and every non-notifying child has an active supervision loop.

## 5. Quality gates
1. **Red→green evidence**: require failing repro/test output before the fix and passing output after, scaled to the user's and repo's evidence priorities (`references/verification.md`).
2. **Design checkpoints**: for user-visible taste decisions, apply the checkpoint gate in `references/verification.md` and record approved checkpoints in durable state (`references/supervision.md`).
3. **Cross-family review**: run `scripts/orch.py scope` first (`references/verification.md`); then review with a different model family given the diff or a shared **evidence pack**; when no such model is usable, take the next step of the reviewer fallback order and note it in the report (`references/selection.md`). Agent-facing docs, including your own edits, get the doc review loop in this review (`references/verification.md`). Send fixes to the implementing child via the harness's continue mechanism (`references/harnesses.md`). On `rework`, important findings at the escalation threshold (default 3), or a child's `STATUS: ESCALATE`, run `scripts/orch.py escalate` and follow its `action:` line (`references/supervision.md`).
4. **Final check before landing**: run the pre-landing verification checklist in `references/verification.md` and send any follow-up fixes to the implementing child.
5. **Land**: follow repo policy `Land:` (`references/repo-policy.md`) or documented process (`AGENTS.md`/`CLAUDE.md`), advance any dependent batch waiting on these paths, and shrink or disable the supervision loop when the batch finishes (`references/supervision.md`).
6. **Log outcome**: append completion (`ok|failed|quota|stall|rework` + findings count) to the decision ledger (`scripts/orch.py log`, `references/improvement.md`).

*Done when*: proportional red→green evidence and any taste checkpoints are approved, review findings are resolved, the pre-landing checklist passes, the change lands, dependent batches or supervision wind-down run, and the outcome is logged.

## 6. Quota and failure handling
- **Quota error** (child or orchestrator): run `scripts/orch.py exhaust` with selection arguments (reset parsing in `references/quota-errors.md`; pass `--self --coordination <0|1|2>` in place of the worker arguments for this conversation per `references/harnesses.md` "Orchestrator's own quota"), log `--outcome quota` (`scripts/orch.py log`), and follow its `action:` line per `references/harnesses.md` (switch in place → new child with partial results → scheduled resume at `earliest_reset + 2m`).
- **Known infrastructure failures and unstarted PR checks**: apply the single-rerun and merge-state gates in `references/verification.md` before investigating or waiting.
- **Other child failures** (context or turn cap, tool or harness error, unknown): run `scripts/orch.py failure "<error text>" --ref <item>` and follow its `action:` line (`references/supervision.md`).
- **Harness quirks or stalls**: follow the harness row in `references/harnesses.md`, `quirks` in `catalog.yaml`, and the supervision loop in `references/supervision.md`; log `stall` outcomes to the ledger.
- **Manual fallback**: when the user reports facts probes cannot see (plan cancelled, renewal date, manual reset, preferences), update assets per `references/assets-updates.md`.

*Done when*: exhausted scopes carry reset times in assets, expired lockouts clear, failures follow retry/rerun and merge-state rules, any quota or reviewer hold passes `scripts/orch.py defer`, interrupted or staged work switches in place or schedules a `resume` wakeup, and outcomes are logged.

## 7. Decide, ask, report
Execute routine orchestration choices (including in-place model switches, staged Phase 1 launches and phase-boundary switches under `below_tier: stage`, same-conversation scheduled resumes, supervision checks over non-notifying children, single reruns of known infrastructure failures, delegated design checkpoints when `preferences.design_checkpoints: delegate`, and dispatching a `select` candidate from a `low` or `preserve` pool). Waiting for a preferred model to renew is not by itself a reason to hold or to ask: dispatch what its `action:` line names now and choose the reviewer when its step starts. Before deferring an item, asking whether to wait, or announcing a stop over quota or reviewer family, run `scripts/orch.py defer` and follow its `action:` line. Ask the user once before destructive or irreversible actions, publishing, spending emergency models/efforts, spending pay-per-use APIs under `preferences.pay_per_use: ask` (under `never`, keep them excluded), opening a new top-level conversation when `preferences.new_conversations: ask`, proceeding when `preferences.ui_fallback: ask`, approving a taste checkpoint when `preferences.design_checkpoints: ask` (default), or resolving ambiguity that changes the outcome.

**Reporting to the user**: one line per event: what finished, what is next, what the user must decide. Model, effort, quota, reviewer choice, and tables only when asked or when a decision depends on them; reviewer degradation and model details go in the PR body or report. Answer which-model, which-reviewer, and why questions from `orch.py history` and the plan file, not memory.

*Done when*: routine decisions are reported one line each and every listed high-impact or taste gate waits for one explicit approval.

## References (load on demand)
- `references/selection.md` — classification rubric, tier floors, risk triggers, effort knee, quota pools and tie-break order, orchestrator ladder, UI/UX and preferred-family fallback (`ui_fallback`), below-tier staging (`below_tier`), follow-up kinds and due rule, reviewer timing and fallback order, review gate.
- `references/orchestrator-launch.md` — launch spec (`*.launch.yaml`), `orch.py launch` payload/header, and post-launch requested-vs-effective check.
- `references/supervision.md` — conflict-aware batching, deferred launch (user or `defer`/`select` hold), failure-mode retries, quality escalation, confirm-stop before replacement, user-local durable state, completion notices, recurring supervision loop, and idempotent child messages.
- `references/verification.md` — proportional evidence (`evidence`), scope check, agent-facing doc review, design checkpoints (`design_checkpoints`), pre-landing check, single rerun for known infra failures, and merge-state check for unstarted PR checks.
- `references/repo-policy.md` — repo policy contract, precedence, and brief forwarding.
- `references/harnesses.md` — delegation surface (`subagent` vs `conversation`), in-place model switching and scheduled resume on quota exhaustion, orchestrator's own quota, and alphabetical harness matrix.
- `references/brief-template.md` — delegation brief layout, doc review-loop rule, and mandatory report sections.
- `references/quota-errors.md` — automatic probe thresholds, exit codes, and error-message parsing.
- `references/assets-updates.md` — plain-language fallback updates for `assets.yaml`.
- `references/research-unrated.md` — one-time scoring workflow for unrated models.
- `references/improvement.md` — decision ledger (`ledger.jsonl`), ladder calibration, upstream retro findings, and skill update routine.
