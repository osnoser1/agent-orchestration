# One-time research for unrated models

Run this once when discovery returns a model missing from `catalog.yaml` + `catalog.local.yaml`, marked `rating: unrated`, or lacking rated scores for needed non-emergency efforts.

1. Re-check `catalog.yaml` and `catalog.local.yaml` (next to `assets.yaml`) to confirm the score is still absent.
2. Look up intelligence scores per reasoning effort once: prefer Artificial Analysis Intelligence Index (`artificialanalysis.ai/models/<id>`), then vendor evaluations; when no benchmark exists, record `rating: estimated` with numeric estimated `scores` per effort and a brief note (`orch.py check` rejects an estimated entry without them).
3. Record `scores`, `source` (with version + retrieval date in `index_versions`), `cost`, `family`, and `confidence` (`high` or `low`) in `catalog.yaml` (via PR) or `catalog.local.yaml`.
4. Reuse the recorded entry on subsequent tasks and suggest an upstream PR with the new data.

Cost classes: `cheap` (high-volume/mini tiers) · `normal` (default flagship) · `expensive` (scarce/verbose top tier) · `emergency` (highest-cost tier, ask once before use).
