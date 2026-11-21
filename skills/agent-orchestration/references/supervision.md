# Batching, durable state, and supervision loop

Use these rules when coordinating multiple work items, acting on a child's completion, supervising non-notifying children, or deferring work until a provider renews.

## Conflict-aware batching

Before launching items in parallel, compare each item's expected target paths, modules, schemas, lockfiles, and shared entrypoints:

1. **Detect overlap**: when the target repo's `## Orchestration` or scripts provide overlap tooling (`references/repo-policy.md`), run that tool; otherwise compare target paths and coupled modules directly.
2. **Partition into batches**:
   - **Independent items** (disjoint target areas) run in parallel in the current batch.
   - **Overlapping items** (touching the same files or coupled module) run sequentially across ordered batches. Keep dependent items queued in durable state and launch each one only after the earlier item lands (or its branch becomes the dependent item's base).

## Deferred launch until a provider renews

When the user or batch plan holds work for a specific exhausted or renewing provider (rather than spending another provider now):

1. Record a `resume` follow-up in `assets.yaml` pairing the provider (and optional quota group) with a required tier or model (`references/selection.md`):
   `python3 scripts/orch.py followups --add --kind resume --ref "<batch-or-task-ref>" --needs "provider:<provider-id>/<tier-or-model>" --after <reset_or_renews + 2m>`
   (or `provider:<provider-id>/<group-name>/<tier>` on grouped providers). Add `--on-recovery` when the provider or group is exhausted, so the launch fires as soon as it renews early; omit it for a provider that renews on a schedule.
2. When the harness supports delayed scheduling (`Scheduled resume` in `references/harnesses.md`), schedule one wakeup at `reset_or_renews + 2m` to launch the batch; otherwise report the renewal timestamp to the user.
3. Clear the entry with `python3 scripts/orch.py followups --done "<batch-or-task-ref>"` once the batch launches.

## Durable orchestration state

Store multi-item batch and staged-run state in a user-local plan file (such as `~/.config/agent-orchestration/plans/<slug>.md`; keep it outside the target repository and public trackers), and keep a short state summary in the orchestrator's own context and, when a supervision loop runs, in its recurring prompt. This applies whether the children notify or not.

Carry everything required to resume cold after context compaction or a new session:

1. **Provider quota snapshot**: current status, `used_percent`, and `resets_at` from `scripts/orch.py probe`.
2. **Batch-relevant preferences**: active `assets.yaml` settings (`ui_fallback`, `below_tier`, `new_conversations`, `design_checkpoints`, `evidence`), repo `## Orchestration` lines, and user instructions for the batch.
3. **Open children**: each active child's ID/link, role, `provider / model @ effort`, target paths/branch, latest status, and for children staged under `below_tier: stage` the current phase, the Phase 2 target model, and the scheduled double-check time (`references/selection.md`).
4. **Approved checkpoints**: concrete design or scope decisions the user already approved (`references/verification.md`), so resumed sessions reuse the recorded approval directly.
5. **Remaining batches**: ordered pending batches with their conflict dependencies and briefs, or the user-local plan file path holding them.
6. **Self-healing rule**: put the literal line *"If the plan file is missing, rebuild it from this summary before continuing."* at the top of the plan file and in every state summary (the orchestrator's own and any supervision prompt).

Refresh durable state whenever a batch launches, a child switches models, completes, or lands, quota state changes, or a checkpoint is approved.

## Completion notices

When a child reports completion (a harness task notification, or its final or `finished` milestone message), act on it in the same turn: read its result, take the next step it unblocks (the Step 5 quality gates in `references/verification.md`, a dependent batch item, or a report to the user). The turn ends with that next step started or done.

## Supervision loop

Scope the supervision loop strictly to children that do **not** send automatic completion wakeups to the orchestrator (top-level conversations, detached CLI runs, or harnesses without completion notices). Brief such children to message the orchestrator at each milestone where the harness lets them (`references/brief-template.md`), so the loop is a fallback for silence, not the main signal. Children that notify the orchestrator on completion rely on their completion wakeup alone (`references/harnesses.md`; harness rules keep authority under Precedence 2 in `references/repo-policy.md`).

When several non-notifying children run for long, schedule a recurring supervision check using the harness's scheduler (`Scheduled resume` in `references/harnesses.md`); in harnesses without a background scheduler, run the check on each user turn.

On each check:

1. **Read cheaply**: inspect each open non-notifying child with a small read limit (recent messages/run state only).
2. **Classify and act**:
   - **Healthy**: active run in progress, or waiting on an awaited task/subagent → leave running, and run the scope check on its working diff (`references/verification.md`).
   - **Staged Phase 1 done**: apply the phase-boundary switch in `references/selection.md`.
   - **Stopped by quota**: run the quota-recovery decision order in `references/harnesses.md`.
   - **Idle with pending work**: diagnose why it stopped and send one continuation message. When a Gemini child exhibits the `"ended turn while waiting"` quirk (`catalog.yaml`, `references/harnesses.md`), resend and instruct it to block in one foreground command.
   - **Completed**: act on it as a completion notice (above).
3. **Low-noise status**: when every open child is healthy and nothing needs attention, reply with one short `"all running"` line.
4. **Wind down**: shrink the check list or widen the interval as children finish; disable or delete the recurring schedule as soon as the batch completes, and state in one line that supervision is off.

*Done when*: each check has classified and acted on every open non-notifying child and refreshed durable state, and the recurring schedule is removed once no such child remains.

## Idempotent child messages

When sending a launch, continuation, steer, or cancel request in a harness that supports idempotency keys (`references/harnesses.md`), derive the key deterministically from the target, action, and the child's last observed turn or message ID (for example `continue-<childId>-<lastTurnId>` or `launch-<batchRef>-<itemSlug>`) so retries for the same observed child state carry the identical key and deduplicate into a single turn.
