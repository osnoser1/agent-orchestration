# Quota probes, thresholds, and error parsing

## Automatic probe (`scripts/orch.py probe`)
Run `python3 scripts/orch.py probe` during discovery to refresh `used_percent`, `window`, and `resets_at` in `assets.yaml`:
- **Thresholds**: `used_percent >= 85` marks `status: low` (override via `preferences.low_threshold`); `used_percent >= 100` marks `status: exhausted` with `exhausted_until: <resets_at>`.
- **Auto-recovery**: any `exhausted` entry whose `exhausted_until` or `resets_at` has passed clears to `status: ok`, persists to `assets.yaml`, and prints a recovery line.
- **Credential handling**: Claude OAuth tokens are read locally from `<config_dir>/.credentials.json`, sent only to `https://api.anthropic.com/api/oauth/usage`, and omitted from stdout, logs, and `assets.yaml`. Missing credentials or HTTP 401 report `unverified` and continue.
- **Exit codes (`orch.py`)**: `0` ok · `1` validation/usage error · `2` no tier match (`select`) · `3` blocked by pre-flight threshold (`low` ≥85% or gated) · `4` quota hit (`exhausted` / 100%).

## Reactive error parsing (`scripts/orch.py exhaust`)
Trigger on messages containing `"hit your (weekly|usage) limit"`, `"quota reached"`, `"rate limit"`, `"try again at"`, or `"resets"`:

| Message | Parse | Write |
|---|---|---|
| `You've hit your weekly limit · resets Nov 23, 3am (America/New_York)` | absolute date + IANA zone | `exhausted_until: 2026-11-23T03:00:00-05:00` |
| `Quota limit reached · resets Nov 23, 3am (America/Argentina/Buenos_Aires)` | multi-segment IANA zone | `exhausted_until: 2026-11-23T03:00:00-03:00` |
| `Individual quota reached… Resets in 4h29m1s` / `Resets in 2 hours 30 minutes` | relative duration | `now + duration` |
| `You've hit your usage limit… try again at Nov 26th, 2026 8:01 AM` | absolute (local tz if omitted) | `2026-11-26T08:01:00<tz>` |
| `Weekly limit resets Dec 1` / `resets 2026-12-01` | date-only | end of that day (`23:59:59`) |

Rules:
- Missing year resolves to the next future occurrence; missing time resolves to `23:59:59` of that day; unparseable messages default to `now + 1h` with `unparsed: true`.
- Update only the affected provider entry (`status`, `exhausted_until`, `status_note`) and top-level `updated`, then switch candidate and resend the brief with partial results.
