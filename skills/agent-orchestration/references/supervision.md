# Batching, durable state, and supervision loop

Use these rules when coordinating multiple work items, running several children concurrently, or deferring work until a provider renews.

## Conflict-aware batching

Before launching items in parallel, compare each item's expected target paths, modules, schemas, lockfiles, and shared entrypoints:

1. **Detect overlap**: when the target repo's `## Orchestration` or scripts provide overlap tooling (`references/repo-policy.md`), run that tool; otherwise compare target paths and coupled modules directly.
2. **Partition into batches**:
   - **Independent items** (disjoint target areas) run in parallel in the current batch.
   - **Overlapping items** (touching the same files or coupled module) run sequentially across ordered batches. Keep dependent items queued in durable state and launch each one only after the earlier item lands (or its branch becomes the dependent item's base).

## Deferred launch until a provider renews

When the user or batch plan holds work for a specific exhausted or renewing provider (rather than spending another provider now):

1. Record a `resume` follow-up in `assets.yaml` targeting that provider (or another selector from `references/selection.md`):
   `python3 scripts/orch.py followups --add --kind resume --ref "<batch-or-task-ref>" --needs provider:<provider-id> --after <reset_or_renews + 2m>`
2. When the harness supports delayed scheduling (`Scheduled resume` in `references/harnesses.md`), schedule one wakeup at `reset_or_renews + 2m` to launch the batch; otherwise report the renewal timestamp to the user.
3. Clear the entry with `python3 scripts/orch.py followups --done "<batch-or-task-ref>"` once the batch launches.

## Durable orchestration state

Store multi-item batch state in the recurring supervision prompt itself or in a user-local plan file it points to (such as `~/.config/agent-orchestration/plans/<slug>.md`; keep it in user-local storage outside the target repository and public trackers).

Carry everything required to resume cold after context compaction or a new session:

1. **Provider quota snapshot**: current status, `used_percent`, and `resets_at` from `scripts/orch.py probe`.
2. **Batch-relevant preferences**: active `assets.yaml` settings (`ui_fallback`, `new_conversations`, `design_checkpoints`, `evidence`), repo `## Orchestration` lines, and user instructions for the batch.
3. **Open children**: each active child's ID/link, role, `provider / model @ effort`, target paths/branch, and latest status.
4. **Approved checkpoints**: concrete design or scope decisions the user already approved (`references/verification.md`), so resumed sessions reuse the recorded approval directly.
5. **Remaining batches**: ordered pending batches with their conflict dependencies and briefs, or the user-local plan file path holding them.
6. **Self-healing rule**: embed the literal instruction: *"If the plan file is missing, rebuild it from this summary before continuing."*

Refresh durable state whenever a batch launches, a child switches models or lands, quota state changes, or a checkpoint is approved.

## Supervision loop

When several children run for long, schedule a recurring supervision check using the harness's scheduler (`Scheduled resume` in `references/harnesses.md`); in harnesses without a background scheduler, run the check on each user turn or child wakeup.

On each check:

1. **Read cheaply**: inspect each open child with a small read limit (recent messages/run state only).
2. **Classify and act**:
   - **Healthy**: active run in progress, or waiting on an awaited task/subagent → leave running.
   - **Stopped by quota**: follow the quota-recovery order in `references/harnesses.md` (`orch.py exhaust` → switch model in place → new child with partial results → scheduled resume at `earliest_reset + 2m`).
   - **Idle with pending work**: diagnose why it stopped and send one continuation message. When a Gemini child exhibits the `"ended turn while waiting"` quirk (`catalog.yaml`, `references/harnesses.md`), resend and instruct it to block in one foreground command.
   - **Completed**: run the Step 5 quality gates (`references/verification.md`), launch any dependent batch unblocked by its landing, and update durable state.
3. **Low-noise status**: when every open child is healthy and nothing needs attention, reply with one short `"all running"` line.
4. **Wind down**: shrink the check list or widen the interval as children finish; disable or delete the recurring schedule as soon as the batch completes, and state in one line that supervision is off.

## Idempotent child messages

When sending a launch, continuation, steer, or cancel request in a harness that supports idempotency keys (`references/harnesses.md`), attach a deterministic key encoding the target, action, and timestamp bucket (for example `continue-<childId>-<unix_ts>`) so the harness deduplicates retries and overlapping supervision ticks into a single turn.
