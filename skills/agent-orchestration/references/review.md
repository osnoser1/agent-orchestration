# Reviewer and review gate

Reach this file at the reviewer step and for every re-review. Filters and ranking stay in `references/selection.md`.

## Reviewer selection
- **Tier**: equal to the implementer's tier; `high` risk bumps the reviewer one tier up (capped at T4).
- **Chosen at step start** (`SKILL.md` Step 3): run `scripts/orch.py probe`, then `select --role reviewer --implementer <model-id>` (bare `--implementer` also selects a reviewer) so the reviewer is ranked against the model that actually implemented.
- **Fallback order** (the first step with a usable model wins and steps 2-5 are noted in the report; on `high` risk a pick at steps 2-5 also queues a cross-family re-review, a `preferred-review` follow-up with `needs: !<family>/<tier>`; it never blocks, except for **High-risk reviewer** below). Capability comes before independence: a below-tier reviewer misses defects, while a same-family tier-meeting reviewer with fresh context still catches most of them.
  1. Different family meeting the reviewer tier. On a UI/UX task the primary already carries `ui-ux-review`, so this reviewer needs no `ui-ux-review`.
  2. Same family, different model, meeting the reviewer tier, with fresh context.
  3. Different family below the tier: the best score available.
  4. Same family, different model, below the tier: the best score available, with fresh context.
  5. Same model in a fresh conversation.
- **High-risk reviewer (`stage` and `proceed`)**: on `high` risk, the review takes a reviewer meeting the reviewer tier (`rt`: fallback steps 1 and 2, or step 5 when the implementer's model meets `rt`) under `stage` and for provisional `proceed` work (implementer below the required tier, `references/fallbacks.md`). When none is usable but an `rt` model waits on quota renewal, `select` records a `resume` follow-up for `rt` and exits `4`. With no `rt` model waiting, the full fallback order applies.
- **From `select` only**: when Python is available, every reviewer, including a mid-review quota replacement, comes from a `select` `action:` line. When `select` answers `fix …`, correct that argument as the line says and rerun `select`. Pass `--role reviewer --implementer <model-id>` with the selection arguments to `scripts/orch.py exhaust` on a mid-review quota error so the script returns the next reviewer directly.
- **Mid-review quota exhaustion**: reviewer children follow the same switch-in-place, new-child, scheduled-resume order as implementers (`references/harnesses.md`).
- **Exclusions**: exhausted, emergency, pay-per-use, `policy: avoid`, and unrated models stay out at every step (`SKILL.md` Step 7 gates). `preferences.ui_fallback` and `--prefer-family` govern primaries only, so reviewer selection always proceeds down the order.
- **`select` output**: ends with one `action:` line (`dispatch`, `stage:`, `hold until <time>`, `ask the user once`, `rerun select without --fail-on-low`, `fix <argument>`, `no usable model`, or `no tier match`) that maps the outcome to the next step. With `--implementer` it adds a `[note] reviewer degraded` for steps 2-5, and with no usable model (or a `high`-risk review waiting under `stage` or `proceed`) it exits `4`, `3`, or `2` (`references/quota-errors.md`), recording a `resume` follow-up on exit `4`; quota recovery then applies (`references/harnesses.md`). Skipped candidates print as `note:` lines grouped by reason; `--full` adds the detail lines.

## Re-reviews and evidence packs
- **Re-reviews**: after fixes, re-review as a delta (the fix commits plus their context). Continue the same reviewer's context when the harness can and its pool is usable (it stays independent of the implementer, so the fresh-context rule applies to the first review only); otherwise give a fresh reviewer from the fallback order the delta and the earlier findings.
- **Shared evidence pack**: for multi-angle or cross-family reviews on large diffs, have a cheap model assemble one `path:line` evidence pack first, then hand the same pack and raw diff alone to each reviewer so judgments compare identical facts independently.
