# Repository orchestration policy

A target repository may add an optional `## Orchestration` section to its `AGENTS.md` (which harnesses read directly) to override this skill's default workflow for that repository.

## Contract

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: <skill names or guide>
- Review: <skill names>
- PR: <skill name>
- Land: <command or process>
- High risk: <extra paths/areas that count as high risk>
- Models: <task-type preferences, e.g. UI/UX work → Anthropic>
- Conversations: <when new top-level conversations are preferred or restricted>
- Evidence: <verification evidence by change type, e.g. UI changes require browser screenshots>
- Checkpoints: <surfaces or taste decisions that pause for user approval vs delegate>
- Known failures: <known transient/infra failure signatures and their one-rerun or remedy command>
```

Every line is optional. When the section is absent, the skill's defaults apply unchanged.

| Field | Effect |
|---|---|
| `Implement` | Skills or guides the implementing worker must follow (named explicitly in implementation briefs; may include repo overlap tooling for batch partitioning in `references/supervision.md`). |
| `Review` | Review skills or checklists the reviewer must apply (named explicitly in review briefs). |
| `PR` | Skill or workflow used to open/update pull requests (named explicitly in PR briefs). |
| `Land` | Exact command or process used in Step 5 (Land). |
| `High risk` | Extra paths, modules, or change classes treated as `high` risk in Step 2 (Classify), additive to `references/selection.md`. |
| `Models` | Task-type model or family preferences (e.g. `UI/UX work → Anthropic`). Unmet preferences follow `references/selection.md`: `preferences.ui_fallback` (from `assets.yaml`, Precedence 2) governs switching to another family when the preferred family is `exhausted` (including mid-task quota switches in `references/harnesses.md`); `not offered here` proceeds with the best available model in this harness and one informational note. |
| `Conversations` | Repo guidance for opening new top-level conversations (`references/harnesses.md`). |
| `Evidence` | Verification evidence expected by change type (`references/verification.md`), scaled by user-wide priorities in `assets.yaml` (`preferences.evidence`, Precedence 2). |
| `Checkpoints` | User-visible surfaces or taste choices that trigger a design checkpoint (`references/verification.md`). `preferences.design_checkpoints` (`ask|delegate` in `assets.yaml`, Precedence 2, default `ask`) governs whether checkpoints pause for user approval or are delegated; this line sets the repo default when omitted in `assets.yaml`. |
| `Known failures` | Known transient or infrastructure failure signatures and their remedy or one-rerun command before code investigation (`references/verification.md`). |

## Precedence

1. **User's explicit instruction** — overrides everything for the current request.
2. **User's `assets.yaml` and harness rules** — keep authority over quota limits (`low_threshold`, `exhausted_threshold`, `exhausted_until`), `preferences.ui_fallback` when a preferred family is exhausted, `preferences.evidence` (user-wide evidence priorities and deprioritized checks), `pay_per_use`, `policy: avoid`, and the ask-once gates in `SKILL.md` Step 7 (`preferences.new_conversations`, `preferences.design_checkpoints`, emergency spending). Assets and harness rules always win on these.
3. **Repository `## Orchestration`** — overrides skill defaults for process (phase skills/commands), extra high-risk triggers, task-type model preferences (`Models:`), repo evidence expectations (`Evidence:`), checkpoint triggers and repo-default mode (`Checkpoints:`), and known infrastructure remedies (`Known failures:`).
4. **Skill defaults** — apply for any omitted line or when `## Orchestration` is absent.

## Applying policy in briefs

Read `## Orchestration` once during Step 1 (Discover). In Step 4 (Brief), name the active phase's skills, commands, evidence requirements, and checkpoint triggers explicitly in `CONSTRAINTS` and `VERIFICATION` (for example, `"follow the implement-plan skill"`, `"use the create-pr skill"`, or `"land with bun run land <pr>"`) in addition to the general `AGENTS.md` read.

## Example

```markdown
## Orchestration
Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: implement-plan, tdd
- Review: code-review
- PR: create-pr
- Land: bun run land <pr>
- High risk: db/migrations/**, auth/**, billing/**
- Models: UI/UX work → Anthropic; SQL migrations → OpenAI
- Conversations: cross-repo follow-ups may open a new thread when user permits
- Evidence: UI changes require before/after screenshots; API changes require red→green integration tests
- Checkpoints: new screens, layout changes, and public CLI flags pause with concrete evidence
- Known failures: forge 502/ETIMEDOUT or lost CI runner → rerun failed jobs once before investigating
```
