# Quota probes, thresholds, and error parsing

## Automatic probe (`scripts/orch.py probe`)
Run `python3 scripts/orch.py probe` during discovery to refresh `used_percent`, `window`, `resets_at`, and per-group `groups` in `assets.yaml`:
- **Probe sources**:
  - **Antigravity**: `agy -p "/usage" --output-format json < /dev/null` (zero-token cost; requires one-time sign-in in the interactive `agy` TUI). Parses `command.data.groups[]` (`used_percent = 100 - remaining_fraction * 100`), ignores `disabled` buckets and expired `reset_time` windows, takes each group's most constraining active bucket, maps display names to catalog ids (unmatched kept as-is and reported once), persists `groups`, and sets provider `status` to the best group (`ok` when any group is usable).
  - **Claude**: `GET https://api.anthropic.com/api/oauth/usage` using local OAuth tokens from `<config_dir>/.credentials.json` (sent only to that endpoint, omitted from stdout, logs, and `assets.yaml`). Missing credentials or HTTP 401 report `unverified` and continue.
  - **Codex**: latest `token_count` event in today + yesterday `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (`payload.rate_limits.{primary,secondary}`), skipping expired windows.
- **Thresholds**: `used_percent >= 85` marks `status: low` (override via `preferences.low_threshold`); `used_percent >= 100` marks `status: exhausted` with `exhausted_until: <resets_at>` (applied per group when a provider reports `groups`).
- **Auto-recovery**: any `exhausted` provider or group whose `exhausted_until` or `resets_at` has passed clears to `status: ok`, persists to `assets.yaml`, and prints a recovery line. `probe` also surfaces `followups` per `references/selection.md` (`due` only when `--available` includes a usable match, otherwise `pending`).
- **Exit codes (`orch.py`)**: `0` ok · `1` validation/usage error · `2` no tier match (`select`) · `3` blocked by pre-flight threshold or policy gate (`low` ≥85%, gated, or `ui_fallback: defer|ask`) · `4` quota hit (`exhausted` / 100%).

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
