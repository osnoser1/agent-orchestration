# Selection rules

Every run: classification, tier lookup, effort selection, and model ranking. `scripts/orch.py classify` and `scripts/orch.py select` implement these exact rules. Branches live beside this file: an unavailable preferred or tier-meeting model in `references/fallbacks.md`, the reviewer in `references/review.md`, the orchestrator's own model in `references/orchestrator.md`.

## Classification rubric (`orch.py classify`)

Score all six criteria from `0` to `2`, then run `scripts/orch.py classify --size N --uncertainty N --deps N --consequences N --verification N --coordination N` and follow its `action:` line (pass its `rubric:` value to `orch.py log --rubric`).

| Criterion | 0 | 1 | 2 |
|---|---|---|---|
| `size` | one file / ≤50 lines | a few files / ≤300 lines | many files or >300 lines |
| `uncertainty` | fully specified: location and fix known | location known, approach open | root cause or location unknown |
| `deps` | self-contained | one shared interface | cross-module / schema / migration |
| `consequences` | local and easily reverted | shared behaviour other users or repos rely on | any Risk trigger or repo `High risk:` trigger |
| `verification` | one test proves it (acceptance test clear up front) | several tests or a manual check | a wrong answer is costly and hard to detect |
| `coordination` | one phase and one child | two phases or 2–3 children | three or more phases, four or more children, a dependency chain (stacked PRs), or multi-repo |

Without Python, map the scores directly:
- **Complexity** from `C = size + uncertainty + deps + verification`: `0–1` `trivial`, `2–3` `standard`, `4–6` `hard`, `7–8` `frontier`.
- **Risk** from `consequences`: `0` `low`, `1` `medium`, `2` `high`.
- **Orchestrator tier** from `coordination` via `preferences.orchestrator.tiers` (default `0 → T1`, `1 → T2`, `2 → T3`).

## Required tier (complexity × risk)

Tiers are Artificial Analysis Intelligence Index floors (higher = smarter) from `catalog.yaml` merged with `catalog.local.yaml` next to `assets.yaml`. Override defaults in `assets.yaml` under `preferences.tier_floors`.

| Complexity \ Risk | low | medium | high |
|---|---|---|---|
| trivial (mechanical edit, rename, format, lookup) | T1 (≥20) | T1 | T2 |
| standard (one feature or bugfix in known code) | T2 (≥40) | T2 | T3 |
| hard (cross-module, unclear cause, migrations) | T3 (≥50) | T3 | T4 |
| frontier (architecture, security, novel debugging) | T4 (≥57) | T4 | T4 + strongest review |

## Risk triggers (any one → high)
Destructive or irreversible operations, security/auth/secrets, production data or infra, publishing/releases, or billing/money, plus any extra triggers listed under `High risk:` in repo policy (`references/repo-policy.md`). High risk raises the reviewer tier (`references/review.md`) and requires one user approval before the irreversible step (`SKILL.md` Step 7).

## Effort knee and missing efforts
Evaluate rated non-emergency efforts in order (`none < low < medium < high < xhigh`), excluding `emergency_efforts` (`max`, `ultra`).
1. Pick the lowest rated effort whose score meets the tier floor.
2. **Effort knee**: if stepping down to the immediately lower rated effort drops the score by `<3` points and the lower score still meets the tier floor, take that lower effort once.
3. **Missing efforts**: evaluate only rated efforts during selection. When no rated effort meets the floor and higher non-emergency efforts are missing, skip the model and note those efforts for one-time research (`references/research-unrated.md`).

## Filter and ranking order
When `assets.yaml` is missing, default every discovered provider to `status: ok` and `policy: normal`. Expired `exhausted_until` / `resets_at` timestamps (provider or group) clear to `ok` automatically. `--available` takes the discovery pairs inline or as `@<file>` (commas or newlines; `#` comments ignored).

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
   3. **Weak score**: on T3+ work, a model with `confidence: low` or a best rated score (non-emergency, within the role's max effort) less than 3 points above the tier floor sorts after the others; it stays a candidate.
   4. **Policy**: `ending` > `spend-first` > `normal` > `preserve`.
   5. **Sooner reset/renewal**: earliest future timestamp from `renews` (when `policy: ending`) or the model group's `resets_at` (when present) / provider `resets_at` / `resets` / `renews` (ISO time, date-only as end of day, or cadence `5h`/`daily`/`weekly`/`monthly`).
   6. **Lower cost class**: `cheap` < `normal` < `expensive`.
   7. **Lower effort** (`none` < `low` < `medium` < `high` < `xhigh`), then lower score.

`status: low` and `policy: preserve` only rank a pool lower; neither holds work. When `select` returns a candidate from such a pool, dispatch it: work never waits for a reset and never waits on the user while a usable tier-meeting candidate exists. Only exhaustion of every tier-meeting candidate (exit `4`, or a `STAGE` plan, `references/fallbacks.md`) or a Step 7 gate in `SKILL.md` (emergency, pay-per-use, `ui_fallback: defer|ask`) holds work. Do not add `--fail-on-low` to avoid a `low` pool; it is a pre-flight check that blocks with exit `3` (`references/harnesses.md`, step 5).

Manage quota by choosing, not pausing: the ranking, the effort knee, and the cheapest model meeting each phase's tier pick the least wasteful candidate; then spend less with delta re-reviews (`references/review.md`) and a per-pool cap on parallel children (`references/supervision.md`).

**`select` output**: ends with one `action:` line (`dispatch`, `stage:`, `hold until <time>`, `ask the user once`, `rerun select without --fail-on-low`, `fix <argument>`, `no usable model`, or `no tier match`) that maps the outcome to the next step. Skipped candidates print as `note:` lines grouped by reason; `--full` adds the detail lines.

## Tool needs
Check `references/harnesses.md` before choosing a provider when the task requires browser, device, or network tools.
