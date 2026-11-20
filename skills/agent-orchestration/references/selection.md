# Selection tables

## Required tier (complexity × risk)

Tiers are intelligence-score floors from `catalog.yaml` (Artificial Analysis Intelligence Index; higher = smarter). Floors are defaults; override in assets `preferences.tier_floors`.

| Complexity \ Risk | low | medium | high |
|---|---|---|---|
| trivial (mechanical edit, rename, format, lookup) | T1 (≥25) | T1 | T2 |
| standard (one feature/bugfix in known code) | T2 (≥40) | T2 | T3 |
| hard (cross-module, unclear cause, migrations) | T3 (≥50) | T3 | T4 |
| frontier (architecture, security, novel debugging) | T4 (≥57) | T4 | T4 + strongest review |

## Risk triggers (any one → high)
Destructive or irreversible ops, security/auth/secrets, production data or infra, publishing/releases, money. High risk also means: stronger independent review (one tier up) and ask the user before the irreversible step.

## Complexity hints
trivial: no judgment, fully specified. standard: you can state the acceptance test. hard: you cannot yet say where the change goes. frontier: wrong answer is expensive and hard to notice.

## Effort knee
Pick the lowest effort whose score clears the tier. If scores jump <3 points between two efforts, take the lower. Missing effort data → use the nearest lower effort that is rated.

## Worked ordering
1. Filter (available, not exhausted, not emergency, not pay-per-use, tier met).
2. Sort by policy: `ending` > `spend-first` > `normal`/default > `preserve`; ties → sooner reset → lower cost class → lower effort.
3. Reviewer: same filter, different `family`, tier ≥ implementer's tier (UI/UX: `strengths` contains `ui-ux-review` first).

## Tool needs
Browser/device/network tasks require a harness that offers those tools; check the harness notes in `references/harnesses.md` before choosing.
