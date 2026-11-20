# One-time research for `unrated` models

1. Check `catalog.yaml` and `catalog.local.yaml` again; if the model is `rating: unrated` (or lacks scores for the efforts you need: e.g. only `max` is rated), continue.
2. Look up the model once: prefer the Artificial Analysis Intelligence Index (artificialanalysis.ai) per reasoning effort; else a vendor eval; else mark `rating: estimated` with a note.
3. Record per effort `{score, source, date, version}`, plus `cost` class and `family`, in `catalog.yaml` (if you can edit/PR it) or `catalog.local.yaml`. Uncertain → `confidence: low`.
4. Don't re-research on later tasks. Suggest a PR upstream with the data.
Cost class guide: cheap = high-volume/mini tiers; normal = default flagship; expensive = top tier, scarce quota; emergency = priciest tier/effort, only if all else exhausted or the user asks.
