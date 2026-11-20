# Plain-language updates to assets

| User says | Do |
|---|---|
| "Claude reset" | provider `status: ok`, clear note |
| "I cancelled X" / "X ends on <date>" | `policy: ending`, `renews` = end date; spend it first |
| "X is almost out" | `status: low`; deprioritize for big work |
| "X is use-it-or-lose-it" | `policy: spend-first` |
| "I added provider Y" | add entry (id from discovery) |
| "stop using Z" | `policy: avoid` or remove |
| "don't pay for API" (default) | keep `pay_per_use: true` providers excluded |

Edit the file in place, keep comments, confirm in one line. If the file doesn't exist, create it from `assets/assets.example.yaml` with only providers the user mentioned. Never write personal data into the repo.
