Repository policy for the agent-orchestration skill; it overrides the skill's defaults here.
- Implement: follow [AGENTS.md](../AGENTS.md) (deterministic logic in scripts with tests, output contract, and version bump via `python3 tools/repo.py bump`)
- Review: [agent-doc-review-loop](skills/agent-doc-review-loop/SKILL.md) for agent-facing docs; output contract tests ([tests/test_output_contract.py](../tests/test_output_contract.py)); cross-family reviewer
- PR: squash-merge PRs; PR body lists verification and "Not verified"
- Land: squash merge once checks are green
- High risk: .github/workflows/**; tools/pinned-rules.json; class:release publishing; class:changes to quota or selection rules
- Known failures: /Failed to check for deleted skills/ => rerun
