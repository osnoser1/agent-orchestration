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
Destructive or irreversible operations, security/auth/secrets, production data or infra, publishing/releases, or billing/money. High risk bumps the reviewer tier by one (capped at T4) and requires one user approval before the irreversible step.

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

1. **Filter** (in this exact order):
   1. Exclude providers with `pay_per_use: true` or `policy: avoid`.
   2. Exclude providers with `status: exhausted`, and models whose quota group has `status: exhausted` (and `status: low` at provider or group level when running with `--fail-on-low`). Providers without `groups` use provider-level status alone.
   3. Exclude models missing from the catalog (note for one-time research).
   4. Exclude models with `cost: emergency`.
   5. Exclude models with `rating: unrated` (note for one-time research).
   6. Exclude models whose rated non-emergency efforts fall below the tier floor.
2. **Rank** remaining candidates by:
   1. **UI/UX tasks (`--ui`, primary worker only)**: models with `ui-ux-review` in `strengths` (Anthropic) sort first.
   2. **Quota health**: on T3+ work, `status: ok` sorts ahead of `status: low` (evaluated per model group when `groups` are present, otherwise per provider; `used_percent >= 85` by default).
   3. **Policy**: `ending` > `spend-first` > `normal` > `preserve`.
   4. **Sooner reset/renewal**: earliest future timestamp from `renews` (when `policy: ending`) or the model group's `resets_at` (when present) / provider `resets_at` / `resets` / `renews` (ISO time, date-only as end of day, or cadence `5h`/`daily`/`weekly`/`monthly`).
   5. **Lower cost class**: `cheap` < `normal` < `expensive`.
   6. **Lower effort** (`none` < `low` < `medium` < `high` < `xhigh`), then lower score.

## Reviewer and review gate
- **Tier**: equal to the implementer's tier; `high` risk bumps the reviewer one tier up (capped at T4).
- **Family**: exclude the primary worker's `family`. On a UI/UX task, the primary already carries `ui-ux-review`, so any second reviewer comes from another family without requiring `ui-ux-review`. When only one family is available, use a different model with fresh context and state that in the report.
- **Shared evidence pack**: for multi-angle or cross-family reviews on large diffs, have a cheap model assemble one `path:line` evidence pack first, then hand the same pack and raw diff alone to each reviewer so judgments compare identical facts independently.

## Tool needs
Check `references/harnesses.md` before choosing a provider when the task requires browser, device, or network tools.
