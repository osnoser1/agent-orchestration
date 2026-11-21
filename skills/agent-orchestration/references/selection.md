# Selection and review rules

Single source of truth for tier lookup, model ranking, effort selection, and the review gate. `scripts/orch.py select` implements these exact rules.

## Required tier (complexity × risk)

Tiers are Artificial Analysis Intelligence Index floors (higher = smarter) from `catalog.yaml` merged with `catalog.local.yaml` next to `assets.yaml`. Override defaults in `assets.yaml` under `preferences.tier_floors`.

| Complexity \ Risk | low | medium | high |
|---|---|---|---|
| trivial (mechanical edit, rename, format, lookup) | T1 (≥20) | T1 | T2 |
| standard (one feature or bugfix in known code) | T2 (≥40) | T2 | T3 |
| hard (cross-module, unclear cause, migrations) | T3 (≥50) | T3 | T4 |
| frontier (architecture, security, novel debugging) | T4 (≥57) | T4 | T4 + strongest review |

## Risk triggers (any one → high)
Destructive or irreversible operations, security/auth/secrets, production data or infra, publishing/releases, or billing/money, plus any extra paths/areas listed under `High risk:` in the target repo's `## Orchestration` (`references/repo-policy.md`). High risk bumps the reviewer tier by one (capped at T4) and requires one user approval before the irreversible step (`SKILL.md` Step 7).

## Complexity hints
- `trivial`: fully specified, mechanical.
- `standard`: acceptance test is clear up front.
- `hard`: change location or root cause is not yet known.
- `frontier`: a wrong answer is costly and hard to detect.

## Effort knee and missing efforts
Evaluate rated non-emergency efforts in order (`none < low < medium < high < xhigh`), excluding `emergency_efforts` (`max`, `ultra`).
1. Pick the lowest rated effort whose score meets the tier floor.
2. **Effort knee**: if stepping down to the immediately lower rated effort drops the score by `<3` points and the lower score still meets the tier floor, take that lower effort once.
3. **Missing efforts**: evaluate only rated efforts during selection. When no rated effort meets the floor and higher non-emergency efforts are missing, skip the model and note those efforts for one-time research (`references/research-unrated.md`).

## Filter and ranking order
When `assets.yaml` is missing, default every discovered provider to `status: ok` and `policy: normal`. Expired `exhausted_until` / `resets_at` timestamps (provider or group) clear to `ok` automatically.

**Quota pools**: a pool is one provider entry in `assets.yaml` (an account or subscription shared by all its models and by every conversation on that account), or one `groups[]` entry when the provider declares quota groups (e.g. Antigravity reports one group for its Gemini models including `Orchestrator (Neon)`, one for Claude/GPT, and one for Argon). A per-model cap that a subscription enforces apart from the shared pool (e.g. a top model's weekly limit) locks only that model (`orch.py exhaust` scope rules in `references/quota-errors.md`). On exhaustion, pick a fallback whose pool still has quota for that model; when the whole pool is exhausted, the fallback comes from another pool. Same-pool upgrades at a `stage` phase boundary are not fallbacks and stay valid.

1. **Filter** (in this exact order):
   1. Exclude providers with `pay_per_use: true` or `policy: avoid`.
   2. Exclude providers with `status: exhausted`, and models whose quota group has `status: exhausted` (and `status: low` at provider or group level when running with `--fail-on-low`). Providers without `groups` use provider-level status alone.
   3. Exclude models missing from the catalog (note for one-time research).
   4. Exclude models with `cost: emergency`.
   5. Exclude models with `rating: unrated` (note for one-time research).
   6. Exclude models whose rated non-emergency efforts fall below the tier floor.
2. **Rank** remaining candidates by:
   1. **Preferred strength/family (primary worker only)**: `--ui` sorts models with `ui-ux-review` in `strengths` (Anthropic) first; `--prefer-family <family>` (from repo `Models:` in `references/repo-policy.md`) sorts that family first.
   2. **Quota health**: on T3+ work, `status: ok` sorts ahead of `status: low` (evaluated per model group when `groups` are present, otherwise per provider; `used_percent >= 85` by default).
   3. **Policy**: `ending` > `spend-first` > `normal` > `preserve`.
   4. **Sooner reset/renewal**: earliest future timestamp from `renews` (when `policy: ending`) or the model group's `resets_at` (when present) / provider `resets_at` / `resets` / `renews` (ISO time, date-only as end of day, or cadence `5h`/`daily`/`weekly`/`monthly`).
   5. **Lower cost class**: `cheap` < `normal` < `expensive`.
   6. **Lower effort** (`none` < `low` < `medium` < `high` < `xhigh`), then lower score.

## Preferred-model fallback (`preferences.ui_fallback`) and follow-ups
When `--ui` (`ui-ux-review`) or a repo `Models:` family preference (`--prefer-family`) has no usable candidate meeting the tier while at least one other tier-meeting candidate exists, classify the unmet preference from this run's discovery (`--available`):

1. **`exhausted`** (a tier-meeting model of the preferred family is offered in this harness, and its provider or quota group is exhausted): apply `preferences.ui_fallback` (default `proceed`; override via `--fallback`) using the earliest reset among exhausted offered candidates:
   - **`proceed`** (default): pick the best available candidate meeting the tier, mark the work **provisional**, and append `followups: [{kind, ref, needs, after}]` to `assets.yaml` (`kind: ui-review` with `needs: ui-ux-review` for `--ui`; `kind: preferred-review` with `needs: <family>` for `--prefer-family`).
   - **`defer`**: stop before dispatch (exit `3`), report the earliest preferred reset, and recommend waiting for non-urgent work.
   - **`ask`**: stop before dispatch (exit `3`) and ask the user once (`SKILL.md` Step 7) whether to proceed provisionally (queuing a follow-up review) or wait for the reset.
2. **`not offered here`** (absent from this harness's discovery, regardless of accounts in `assets.yaml`): treat the preference as satisfied by the best available model in this harness (preferring a matching `strengths` entry, else best by tier), print one informational note, and proceed immediately (skip follow-ups, reset times, and `ui_fallback` gates).

## Below-tier handling (`preferences.below_tier`) and follow-ups
Applies when no usable candidate meets the required tier, at least one tier-meeting non-emergency, non-pay-per-use model in discovery is exhausted (the **target**: earliest reset, then the standard ranking), and a usable below-tier model exists (best score first, then the standard ranking). `ui_fallback` does not run here. When no tier-meeting model is offered at all, `select` reports `NO CANDIDATE` and exits `2` in every mode. Read the mode from `preferences.below_tier` (default `stage`; override with `--below-tier`):

1. **`stage`** (default; `select` exits `0` and prints the staged plan):
   - **Phase 1** runs now on the best usable below-tier model and covers low-risk preparation only: read the code, plan the approach, write the failing tests, confirm red. Put the boundary in the brief's `PHASE` line (`references/brief-template.md`) and in durable state (`references/supervision.md`).
   - **Follow-up**: `select` records one `kind: resume` entry per ref (`--ref`, else `current-task` when an assets file exists) with `needs` = the required tier, scoped as `provider:<id>/<group>/<tier>` when every exhausted target candidate sits in the same group of one provider, and `after` = `earliest_reset + 2m` (omitted when the reset is unknown). A later run that computes a new `needs` or `after` refreshes the entry (`followup updated`); reschedule its wakeup.
   - **Phase-boundary switch**: when Phase 1 ends, switch the **same** child in place to the target model at once, even while its pool is still exhausted, for **Phase 2**: the risky change, destructive steps, and review. Then:
     - Send one continue message now only for a (harness, provider) pair whose own-provider quota stops resume automatically, verified or observed (`references/harnesses.md`, Scheduled resume column); the run stops on quota and resumes at renewal.
     - For an unverified pair, send nothing now and let the scheduled resume start Phase 2.
     - A harness that cannot switch in place defers the new Phase 2 child (brief plus Phase 1 results, `references/harnesses.md` step 3) until `earliest_reset + 2m`.
     - Schedule one resume at `earliest_reset + 2m` as a double check. At wakeup, re-read the child, derive the idempotency key (`continue-<childId>-<lastTurnId>`) from the turn observed then, and send only while the child is idle. Without a scheduler, report the reset time to the user (`references/harnesses.md` step 4).
   - **Quota stop mid-task**: when quota recovery gets a `STAGE` plan, a child whose remaining work is Phase 1 preparation switches to the below-tier model; a child already in the risky change, destructive steps, or review (staged or not) keeps its tier-meeting model and waits for renewal (`references/harnesses.md` step 4). At the `earliest_reset + 2m` wakeup, re-run `select` and switch in place to whichever tier-meeting model renewed first.
   - **Reviewer**: on `high` risk, the review takes a reviewer meeting the reviewer tier (`rt`): fallback steps 1 and 2, or step 5 when the implementer's model meets `rt`. When none is usable but an `rt` model waits on quota renewal, `select` prints `NO REVIEWER`, records a `resume` follow-up for `rt`, and exits `4`. With no `rt` model waiting, and on low/medium risk, the full fallback order applies.
2. **`wait`**: start nothing; `select` prints `NO CANDIDATE` and `SCHEDULE_RESUME` at `earliest_reset + 2m`, records the `resume` entry as `stage` does, and exits `4`.
3. **`proceed`** (`select` exits `0`): run all phases on the best usable below-tier model, mark the work `[provisional]` in the report and ledger (`orch.py log --provisional`), take the reviewer from the fallback order, and record one `kind: resume` entry per ref (same `needs` and `after` as `stage`) for a tier-meeting re-review when one becomes usable.

With no usable model at any tier, every mode behaves like `wait` (exit `4` when a tier-meeting model is exhausted, else `3` or `2`; `references/harnesses.md`).

**Follow-up kinds and due rule**:
- Three distinct `kind`s share `followups` in `assets.yaml`: `ui-review` (`needs: ui-ux-review`), `preferred-review` (`needs: <family>`), and `resume` (`needs: <tier>`, model id, `provider:<id>/<tier|model>`, `provider:<id>/<group>/<tier>`, `<family>`, or `any` — used for `below_tier` Phase 2 continuations and provisional re-reviews, quota-stopped children in `references/harnesses.md`, and deferred batch launches in `references/supervision.md`).
- `orch.py select`, `orch.py followups --due`, and `orch.py probe` report a recorded follow-up as **due** only when the current run's discovery (`--available`) includes a usable (non-exhausted, non-emergency, non-pay-per-use) model matching `needs`; a `kind: resume` entry with `after` also waits until that time, except a quota wait (`on_recovery: true`, set by `select` and by `followups --add --on-recovery`), which is due as soon as its needed pool is usable again because pools can renew early. An entry recorded under `--fail-on-low` (`fail_on_low: true`) counts `low` pools as unusable for that check. When `--available` is omitted, `probe` and `followups --due` report ready follow-ups as `pending: needs <need>` rather than `due`. Clear completed or resumed entries with `orch.py followups --done <ref>`.

## Reviewer and review gate
- **Tier**: equal to the implementer's tier; `high` risk bumps the reviewer one tier up (capped at T4).
- **Chosen at step start**: models a plan names ahead of time are provisional. When each role's step starts (implementer, then reviewer), run `scripts/orch.py probe`, then `select` from the models usable at that moment. At the reviewer step pass `--implementer <model-id>` so the reviewer is ranked against the model that actually implemented.
- **Fallback order** (the first step with a usable model wins and steps 2-5 are noted in the report; it never blocks, except for the `high`-risk `stage` rule in **Below-tier handling**). Capability comes before independence: a below-tier reviewer misses defects, while a same-family tier-meeting reviewer with fresh context still catches most of them.
  1. Different family meeting the reviewer tier. On a UI/UX task the primary already carries `ui-ux-review`, so this reviewer needs no `ui-ux-review`.
  2. Same family, different model, meeting the reviewer tier, with fresh context.
  3. Different family below the tier: the best score available.
  4. Same family, different model, below the tier: the best score available, with fresh context.
  5. Same model in a fresh conversation.
- **Exclusions**: exhausted, emergency, pay-per-use, `policy: avoid`, and unrated models stay out at every step (`SKILL.md` Step 7 gates). `preferences.ui_fallback` and `--prefer-family` govern primaries only, so reviewer selection always proceeds down the order.
- **`select` output**: always prints a `reviewer` line with a `[note] reviewer degraded` for steps 2-5, and exits `0` when any model qualifies. With `--implementer` and no usable model (or a `high`-risk review waiting under `stage`) it prints `NO REVIEWER` and exits `4`, `3`, or `2` like the Filter rules, adding `SCHEDULE_RESUME` and a `resume` follow-up on exit `4`; quota recovery then applies (`references/harnesses.md`).
- **Mid-review quota exhaustion**: reviewer children follow the same switch-in-place, new-child, scheduled-resume order as implementers (`references/harnesses.md`).
- **Shared evidence pack**: for multi-angle or cross-family reviews on large diffs, have a cheap model assemble one `path:line` evidence pack first, then hand the same pack and raw diff alone to each reviewer so judgments compare identical facts independently.

## Tool needs
Check `references/harnesses.md` before choosing a provider when the task requires browser, device, or network tools.
