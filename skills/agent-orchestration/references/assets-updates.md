# Plain-language fallback updates to assets

Automatic probes (`scripts/orch.py probe`) maintain usage percentages and reset times. Use these manual edits for facts no local tool exposes (plan cancellations, renewal dates, manual resets, new providers, or user workflow preferences):

| User says | Update in `assets.yaml` |
|---|---|
| "Claude reset" | `status: ok`; clear `exhausted_until`, `status_note`, `unparsed` (`orch.py status <provider> ok`) |
| "I cancelled X" / "X ends on `<date>`" | `policy: ending`, `renews: <date>` |
| "X is almost out" | `status: low` (`orch.py status <provider> low`) |
| "X is use-it-or-lose-it" | `policy: spend-first` |
| "I added provider Y" | add entry with discovery `id` and optional `probe:` config |
| "stop using Z" | `policy: avoid` (or remove entry) |
| "keep pay-per-use off" (default) | mark billed-per-token entries `pay_per_use: true`, `policy: avoid` |
| "delegate design/taste checkpoints" / "ask me on design checkpoints" | `preferences.design_checkpoints: delegate` (or `ask`, default `ask`) |
| "stage below-tier work" / "wait for tier reset" / "proceed below tier" | `preferences.below_tier: stage` (or `wait` / `proceed`, default `stage`) |
| "skip `<evidence>` unless the task is about it" / "always include `<evidence>`" | `preferences.evidence: "<prioritized and deprioritized evidence>"` |

Edit `assets.yaml` in place, preserve comments, update top-level `updated`, and confirm in one line. When the file is missing, seed it from `assets/assets.example.yaml` with only the providers the user named. Keep personal data outside git repositories.
