# Batching, durable state, and supervision loop

Reach this file when coordinating multiple work items, acting on a child's completion, supervising non-notifying children, deferring work after a user hold or a `defer`/`select` hold, retrying a failed child, or replacing one.

## Conflict-aware batching

Before launching items in parallel, compare each item's expected target paths, modules, schemas, lockfiles, and shared entrypoints:

1. **Detect overlap**: when repo policy (`references/repo-policy.md`) or repo scripts provide overlap tooling, run that tool; otherwise compare target paths and coupled modules directly.
2. **Partition into batches**:
   - **Independent items** (disjoint target areas) run in parallel in the current batch, by default at most two children per quota pool at once (a user or repo instruction may change the cap) so one batch does not drain a pool; place the rest on other pools or in the next batch. Treat a full pool as a batch cap rather than a quota wait: run `scripts/orch.py defer --ref <item> --pool-busy <pool>=<n> --pool-cap <cap>` with the selection arguments — it queues when `select`'s pool is at cap and no free pool gives an equally good pick — and follow its `action:` line.
   - **Overlapping items** (touching the same files or coupled module) run sequentially across ordered batches. Keep dependent items queued in durable state and launch each one only after the earlier item lands (or its branch becomes the dependent item's base).

*Done when*: every item sits in a batch, overlapping items are ordered across batches, each batch's independent items stay within the pool cap, and items whose `select` pool is at cap with no equally good free pool are queued for the next batch via `defer --pool-busy`.

## Deferred launch (user hold or `defer`/`select` hold only)

Apply this flow only when the user explicitly holds work for a renewing provider, or when `scripts/orch.py defer` / `scripts/orch.py select` prints `hold until` (dispatch what `select` or `defer` names whenever a usable model exists):

1. **User hold**: record the `resume` follow-up through `defer`:
   `python3 scripts/orch.py defer --ref "<batch-or-task-ref>" --user-hold --needs "provider:<provider-id>/<tier-or-model>" --after <reset_or_renews + 2m>`
   (or `provider:<provider-id>/<group-name>/<tier>` on grouped providers; add `--on-recovery` when the provider or group is exhausted so it fires on early recovery). When `defer` or `select` already printed `hold until`, its `resume` follow-up is recorded.
2. When the harness supports delayed scheduling (`Scheduled resume` in `references/harnesses.md`), schedule one wakeup at `reset_or_renews + 2m` to launch the batch; otherwise report the renewal timestamp to the user.
3. Clear the entry with `python3 scripts/orch.py followups --done "<batch-or-task-ref>"` once the batch launches.

*Done when*: the `resume` follow-up exists (from `defer --user-hold` or a `defer`/`select` hold) and its wakeup is scheduled (or the renewal time is reported), and the follow-up is cleared once the batch launches.

## Durable orchestration state

Store multi-item batch and staged-run state in a user-local plan file (such as `~/.config/agent-orchestration/plans/<slug>.md`; keep it outside the target repository and public trackers), and keep a short state summary in the orchestrator's own context and, when a supervision loop runs, in its recurring prompt. This applies whether the children notify or not.

Carry everything required to resume cold after context compaction or a new session:

1. **Provider quota snapshot**: current status, `used_percent`, and `resets_at` from `scripts/orch.py probe`.
2. **Batch-relevant preferences**: active `assets.yaml` settings (`ui_fallback`, `below_tier`, `new_conversations`, `design_checkpoints`, `evidence`), repo policy lines (`references/repo-policy.md`), and user instructions for the batch.
3. **Open children**: each active child's ID/link, role, `provider / model @ effort`, target paths/branch, latest status, and for children staged under `below_tier: stage` the current phase, the Phase 2 target model, and the scheduled double-check time (`references/fallbacks.md`).
4. **Approved checkpoints**: concrete design or scope decisions the user already approved (`references/verification.md`), so resumed sessions reuse the recorded approval directly.
5. **Remaining batches**: ordered pending batches with their conflict dependencies and briefs, or the user-local plan file path holding them.
6. **Self-healing rule**: put the literal line *"If the plan file is missing, rebuild it from this summary before continuing."* at the top of the plan file and in every state summary (the orchestrator's own and any supervision prompt).

Refresh durable state whenever a batch launches, a child switches models, completes, or lands, quota state changes, or a checkpoint is approved.

*Done when*: the plan file and the state summary carry all six items, refreshed at the latest launch, switch, completion, landing, quota change, or checkpoint approval.

## Completion notices

When a child reports completion (a harness task notification, or its final or `finished` milestone message), act on it in the same turn: read its result, take the next step it unblocks (the Step 5 quality gates in `references/verification.md`, a dependent batch item, or a report to the user). The turn ends with that next step started or done.

## Supervision loop

Scope the supervision loop strictly to children that do **not** send automatic completion wakeups to the orchestrator (top-level conversations, detached CLI runs, or harnesses without completion notices). Brief such children to message the orchestrator at each milestone where the harness lets them (`references/brief-template.md`), so the loop is a fallback for silence, not the main signal. Children that notify the orchestrator on completion rely on their completion wakeup alone (`references/harnesses.md`).

When several non-notifying children run for long, schedule a recurring supervision check using the harness's scheduler (`Scheduled resume` in `references/harnesses.md`); in harnesses without a background scheduler, run the check on each user turn.

On each check:

1. **Read cheaply**: inspect each open non-notifying child with a small read limit (recent messages/run state only).
2. **Classify and act**:
   - **Healthy**: active run in progress, or waiting on an awaited task/subagent → leave running, and run the scope check on its working diff (`references/verification.md`).
   - **Staged Phase 1 done**: apply the phase-boundary switch in `references/fallbacks.md`.
   - **Stopped by quota**: run the quota-recovery decision order in `references/harnesses.md`.
   - **Idle with pending work**: diagnose why it stopped and send one continuation message. When a Gemini child exhibits the `"ended turn while waiting"` quirk (`catalog.yaml`, `references/harnesses.md`), resend and instruct it to block in one foreground command.
   - **Polling**: recent activity shows the same short command repeated (a loop of seconds, each a model turn) → steer it once to the harness wait (`WAITING` in `references/brief-template.md`) and, while an Antigravity Gemini child runs a long background command, check at least every 30 min (sized to the ~50 min drain reported in issue 14; *unverified* as a general figure).
   - **Completed**: act on it as a completion notice (above).
3. **Low-noise status**: when every open child is healthy and nothing needs attention, reply with one short `"all running"` line.
4. **Wind down**: shrink the check list or widen the interval as children finish; disable or delete the recurring schedule as soon as the batch completes, and state in one line that supervision is off.

*Done when*: each check has classified and acted on every open non-notifying child and refreshed durable state, and the recurring schedule is removed once no such child remains.

## Failure-mode retries

Run `python3 scripts/orch.py failure "<error text>" --ref <item>` (loads `Known failures:` from `--repo` automatically, or pass `--known 'REGEX=>ACTION'` to override) and follow its `action:` line, which already applies the ledger's retry count; log the outcome with `scripts/orch.py log --event complete` plus the `log:` flags it prints (`--ref` is a short item label, never a path). A stall has no error text: use its row.

Without Python, map how the child failed to the next action:

| How the child failed | Next action | Outcome |
|---|---|---|
| Quota error | The quota flow in `SKILL.md` Step 6 and `references/harnesses.md`. | `quota` |
| Context or turn cap hit | Start a replacement (confirm-stop below) with the partial results (branch, commit, last completed step) and a narrower scope covering only the remaining work. | `failed` |
| Tool or harness error | Retry once on the same model; if it recurs, rerun `select` with the failing model removed from `--available` and use its candidate. | `failed` |
| Transient infrastructure or CI failure | The single rerun in `references/verification.md`. | `ok` when the rerun passes, else `failed` |
| Stall | The harness row's stall handling in `references/harnesses.md`; for non-notifying children, the supervision loop above. | `stall` |
| Unknown | Retry once; a failed retry counts toward the two below. | `failed` |

After two failed retries of the same item, stop retrying it and report it to the user with the evidence (error text, last messages, ledger lines). Count per item across models, and only retries or replacements under the context-cap, tool or harness error, and unknown rows; quota switches, scheduled resumes, stall continuations, and infrastructure reruns do not count. `failure` counts these from the ledger by `--ref` and restarts at an `--outcome ok` completion without `--failure-class`, so give each item its own `--ref`.

*Done when*: each failed child's next action is taken from the `action:` line (or the table), its outcome is logged, and any item past two failed retries is reported to the user with evidence.

## Confirm stop before replacement

Before a replacement child writes to the same paths or branch as an old child, cancel the old child and confirm it stopped as the Cancel column in `references/harnesses.md` says. This covers every replacement, including the quota flow's new child (`references/harnesses.md` step 3); a quota-stopped child the harness resumes on its own counts as running until cancelled. Launch the replacement only after that confirmation: never run two writers on one checkout.

*Done when*: the Cancel column's confirming read shows the old child stopped before the replacement's first write.

## Idempotent child messages

When sending a launch, continuation, steer, or cancel request in a harness that supports idempotency keys (`references/harnesses.md`), derive the key deterministically from the target, action, and the child's last observed turn or message ID (for example `continue-<childId>-<lastTurnId>` or `launch-<batchRef>-<itemSlug>`) so retries for the same observed child state carry the identical key and deduplicate into a single turn.
