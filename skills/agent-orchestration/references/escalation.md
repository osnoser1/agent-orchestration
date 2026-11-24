# Quality escalation (`orch.py escalate`)

Reach this file when a role's work falls short (any trigger below).

## Triggers and command

Escalate any role (implementer, reviewer, or orchestrator) on any one trigger: `outcome: rework`; important review findings at the escalation threshold (`preferences.escalation.findings`, default 3; for the orchestrator, `unflagged` findings at `preferences.orchestrator.unflagged_findings`, counting only gaps in its own brief or plan, such as a missing requirement, a wrong design decision, or an unstated constraint or acceptance criterion, never implementation defects); or a child's `STATUS: ESCALATE` (`references/brief-template.md`).

On `STATUS: ESCALATE`, first rerun `orch.py classify` with the child's `ASSESSMENT` (keep your `coordination` score), so its `--complexity`/`--risk` raise the tier floor. Then run `python3 scripts/orch.py escalate --role <role> --ref <item> --current <provider:model@effort> --cause rework|findings|status --available <provider:model,...> [--findings N]`, adding `--complexity`/`--risk` for workers, `--implementer` for a reviewer, or `--coordination` for the orchestrator, and follow its `action:` line; it logs the escalation with its cause. When the line asks the user (unknown current score, emergency-only rung, or the orchestrator's provider switch on `rework` or `status`), rerun it after approval with `--approved --to <provider:model@effort>` to log the approved switch.

After the switch, log the new rung with `log --event select` (`references/improvement.md`; an orchestrator switching itself verifies and logs per `references/orchestrator.md`, Orchestrator's own quota, step 3) and re-review the change as a delta (`references/review.md`, Re-reviews).

*Done when*: every trigger went through `orch.py escalate` (or the rules above), its `action:` line was followed, the escalation is logged with its cause, and the change got a delta re-review.

## Rules without Python

1. **Order**: the next higher rated effort of the same model that meets the role's tier floor first (skipped for a reviewer on the implementer's model; a reviewer goes below the floor only when no candidate meets the tier), then the first model in the worker ranking order (`references/selection.md`), same provider first, at its lowest effort that scores strictly higher; reviewers take the reviewer fallback order (`references/review.md`) instead. The target always scores strictly higher, so an escalation never steps down; when nothing scores higher, report to the user.
2. **Unscored rung**: a current rung without a rated score proves nothing; climb only to a higher rated effort of the same model, else ask the user.
3. **Cap**: at most two escalations per item and role (`preferences.escalation.max`; orchestrator `preferences.orchestrator.max_escalations`). At the cap, report the item to the user with the evidence.
4. **Providers**: workers cross providers as a routine decision. The orchestrator searches every tier-meeting candidate on its own provider, its own model's higher efforts first (effort capped at `high`, `references/orchestrator.md`), and asks the user before a provider switch, at a phase boundary only; on `findings` with no higher same-provider rung it continues on its current rung.
5. **Exclusions**: a reviewer escalates only to a model other than the implementer's; emergency efforts and models, pay-per-use, `policy: avoid`, and exhausted pools stay out. When only emergency models or efforts score higher, ask the user once (`SKILL.md` Step 7).
