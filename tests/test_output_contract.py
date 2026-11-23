"""Output contract for every command of orch.py and tools/repo.py (source of truth; AGENTS.md points here).

The users of these commands are agents, so every output line costs tokens and long output gets
truncated by harnesses. Each command, run on a LARGE fixture, must:

1. be compact by default: at most BUDGET lines, no ANSI escapes;
2. end with exactly one decision line (`action: …`, `ok: …`, or `error: N problem(s); action: …`);
3. cap every list (10 items) with `… N more (rerun with --full)`, which `--full` removes.

A new subcommand without a case below fails `test_every_subcommand_has_a_contract_case`.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
ORCH = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "scripts", "orch.py")
REPO = os.path.join(REPO_ROOT, "tools", "repo.py")
AGY_SAMPLE = os.path.join(HERE, "fixtures", "agy-usage-sample.json")
NOW = "2026-11-20T21:00:00Z"
MORE = "more (rerun with --full)"
ESC = "\x1b"

ORCH_FINAL = re.compile(r"^action: \S")
CHECK_FINAL = re.compile(r"^(ok: \S.*|error: \d+ problem\(s\); action: \S.*)$")
LINE_CHARS = 350  # no compact line longer than this (a list crammed into one line is still a list)
HELP_LINES = 25  # per-subcommand --help
TOP_HELP_LINES = 60


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


class Case:
    """One command on a large fixture.

    final: regex the last line must match, and the only line that may match it (the decision line).
    last: text that `--full` shows but the compact output must not (proves the cap); None when nothing is capped.
    """

    def __init__(self, argv, budget, final, rc=0, last=None, cwd=None, env=None, shows=(), runner=None, hides=()):
        self.argv, self.budget, self.final, self.rc = argv, budget, final, rc
        self.last, self.cwd, self.env, self.shows, self.runner = last, cwd, env or {}, shows, runner
        self.hides = hides  # detail text that only --full may print


def run(script, case):
    """Run a case twice (compact, --full); return ((rc, lines), (rc, lines))."""
    results = []
    for extra in ([], ["--full"]):
        if case.runner:
            results.append(case.runner(extra))
            continue
        env = {**os.environ, **case.env}
        argv = case.argv() if callable(case.argv) else case.argv  # a callable builds a fresh fixture per run
        p = subprocess.run(
            [sys.executable, "-I", script, *argv, *extra],
            cwd=case.cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        )
        results.append((p.returncode, p.stdout.split("\n")[:-1] if p.stdout.endswith("\n") else p.stdout.split("\n")))
    return results[0], results[1]


# ---------- large fixtures for orch.py ----------
class OrchWorld:
    """A temp dir with a 50-provider assets.yaml, a fake `agy`, and helpers that build cases."""

    def __init__(self, tc):
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.assets = os.path.join(self.dir, "assets.yaml")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.env = {
            "HOME": self.home,
            "PATH": os.path.join(self.dir, "bin") + os.pathsep + os.environ.get("PATH", ""),
            "AGENT_ORCHESTRATION_ASSETS": self.assets,
            "NO_COLOR": "1",
            "CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude"),  # never probe the real accounts
            "CODEX_HOME": os.path.join(self.home, ".codex"),
        }
        os.makedirs(os.path.join(self.dir, "bin"))
        agy = os.path.join(self.dir, "bin", "agy")
        with open(agy, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\ncat '%s'\n" % AGY_SAMPLE)
        os.chmod(agy, 0o755)

    def write(self, text, name="assets.yaml"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def select_assets(self, exhausted=40, followups=30):
        """Write select-assets.yaml (one file per case: cases are built before any runs); return its path."""
        lines = ["version: 1", "providers:", "  - id: claude", "    status: ok"]
        for i in range(exhausted):
            lines += ["  - id: ex%02d" % i, "    status: exhausted", "    exhausted_until: 2026-12-01T00:00:00+00:00"]
        lines.append("followups:")
        for i in range(followups):
            lines += ["  - kind: ui-review", "    ref: item-%02d" % i, "    needs: any"]
        return self.write("\n".join(lines) + "\n", "select-assets.yaml")

    def probe_assets(self, exhausted=30, ok=20, name="probe-assets.yaml"):
        lines = ["version: 1", "providers:"]
        for i in range(exhausted):  # a group filter that is 100% used: the provider reads as exhausted
            lines += ["  - id: ex%02d" % i, "    probe: {kind: antigravity, group: \"Claude and GPT models\"}"]
        for i in range(ok):
            lines += ["  - id: ok%02d" % i, "    probe: {kind: antigravity}"]
        return self.write("\n".join(lines) + "\n", name)

    def common(self, assets=None):
        return ["--assets", assets or self.assets, "--now", NOW]


class OrchContract(unittest.TestCase):
    def cases(self):
        w = OrchWorld(self)
        pairs = ["claude:claude-opus-5-5"] + ["ex%02d:model-%d" % (i, i) for i in range(40)] + ["un%02d:mystery-%d" % (i, i) for i in range(9)]
        avail = ",".join(pairs)  # 50 models
        assets = w.select_assets()
        env = w.env
        sel = ["select", "--complexity", "standard", "--risk", "low", "--available", avail, *w.common(assets)]
        cases = {
            "classify": [
                Case(
                    ["classify", "--size", "2", "--uncertainty", "1", "--deps", "2",
                     "--consequences", "1", "--verification", "1", "--coordination", "2", "--assets", assets],
                    6, ORCH_FINAL, env=env, cwd=w.dir,
                    shows=("complexity: hard", "risk: medium", "orchestrator: T3", "rubric: size=2,"),
                ),
            ],
            "select": [
                Case(sel, 5, ORCH_FINAL, last="ex39", env=env, cwd=w.dir, shows=("note: provider exhausted: ex00, ex01",),
                     hides=("reviewer degraded",)),  # the reviewer is chosen later: its degradation belongs to that step
                Case(["select", "--role", "orchestrator", "--coordination", "2", "--available", avail, *w.common(assets)],
                     6, ORCH_FINAL, last="ex39", env=env, cwd=w.dir, shows=("orchestrator tier T3", "ladder:")),
                Case(["select", "--role", "reviewer", "--complexity", "standard", "--risk", "high", "--available", avail, "--implementer", "gpt-6-2-sol", *w.common(assets)],
                     5, ORCH_FINAL, env=env, cwd=w.dir, shows=("dispatch reviewer",)),
                Case(["select", "--complexity", "standard", "--risk", "high", "--available", avail, "--implementer", "no-such-model", *w.common(assets)],
                     5, ORCH_FINAL, rc=1, env=env, cwd=w.dir),
            ],
        }
        cases["select"] += self.select_scenarios(w)
        cases["launch"] = self.launch_cases(w, assets, avail)
        cases["followups"] = [Case(["followups", "--due", "--available", "claude:claude-opus-5-5", *w.common(assets)], 3, ORCH_FINAL,
                                   last="item-29", env=env, cwd=w.dir)]
        cases["log"] = [Case(["log", "--complexity", "standard", "--risk", "low", "--provider", "claude", "--model", "claude-opus-5-5",
                              "--effort", "high", "--ledger", os.path.join(w.dir, "ledger.jsonl"), *w.common()],
                             3, re.compile(r"^logged \S"), env=env, cwd=w.dir)]
        hist = os.path.join(w.dir, "history-ledger.jsonl")
        with open(hist, "w", encoding="utf-8") as fh:
            for i in range(40):
                fh.write(json.dumps({"event": "select", "ref": "item-%02d" % i, "provider": "claude", "model": "claude-opus-5-5", "effort": "high"}) + "\n")
        cases["history"] = [
            Case(["history", "--ledger", hist, *w.common(assets)], 14, re.compile(r"^ok: 40 ref\(s\); 30 pending follow-up\(s\)$"),
                 last="item-00", env=env, cwd=w.dir),
            Case(["history", "--ledger", hist, "--role", "implementer", *w.common(assets)], 14, re.compile(r"^ok: 40 ref\(s\); 30 pending follow-up\(s\)$"),
                 last="item-00", env=env, cwd=w.dir),
            Case(["history", "--ledger", hist, "--ref", "no-such-item", *w.common(assets)], 3, CHECK_FINAL, rc=1, env=env, cwd=w.dir),
        ]
        cases["status"] = [Case(["status", "claude", "low", *w.common()], 3, re.compile(r"^claude: low$"), env=env, cwd=w.dir)]
        cases["exhaust"] = [
            Case(["exhaust", "claude", "You've hit your weekly limit · resets Nov 23, 3am (America/New_York)", *w.common()],
                 3, re.compile(r"^claude: exhausted until \S"), env=env, cwd=w.dir),
        ]
        ledger = os.path.join(w.dir, "big-ledger.jsonl")
        with open(ledger, "w", encoding="utf-8") as fh:
            for i in range(500):
                fh.write(json.dumps({"ref": "item-1", "failure_class": "tool", "event": "complete", "outcome": "failed", "n": i}) + "\n")
        cases["failure"] = [
            Case(["failure", "tool error: harness error " + "x" * 2000, "--ref", "item-1", "--ledger", ledger, *w.common()],
                 3, ORCH_FINAL, env=env, cwd=w.dir),
            Case(["failure", "You've hit your weekly limit · resets Nov 23, 3am (America/New_York)", "--ref", "item-1", "--ledger", ledger, *w.common()],
                 3, ORCH_FINAL, env=env, cwd=w.dir),
        ]
        cases["scope"] = self.scope_cases(w)
        cases["check"] = self.check_cases(w)
        cases["probe"] = self.probe_cases(w)
        cases["policy"] = self.policy_cases(w)
        return cases

    def select_scenarios(self, w):
        """Every select outcome that records a follow-up or stops: compact keeps the recording line and ends on the action."""
        exhausted = w.write(
            "version: 1\nproviders:\n  - id: claude\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
            "  - id: codex\n    status: ok\n", "exhausted-assets.yaml")
        ui = {}
        for mode in ("defer", "ask", "proceed"):
            ui[mode] = w.write(
                "version: 1\npreferences:\n  pay_per_use: never\n  ui_fallback: %s\nproviders:\n"
                "  - id: claude\n    policy: preserve\n    status: exhausted\n    exhausted_until: 2026-11-23T08:00:00+00:00\n"
                "  - id: antigravity\n    policy: spend-first\n    status: ok\n    groups:\n"
                "      - name: Gemini\n        models: [gemini-4-neon]\n        status: ok\n        used_percent: 30\n"
                "        window: 7d\n        resets_at: 2026-11-26T15:58:04+00:00\n"
                "      - name: Claude\n        models: [claude-opus-5-5, claude-sonnet-5-5]\n        status: exhausted\n"
                "        used_percent: 100\n        window: 7d\n        resets_at: 2026-11-27T15:59:36+00:00\n"
                "  - id: codex\n    status: ok\n" % mode, "ui-%s-assets.yaml" % mode)
        both = "claude:claude-opus-5-5,codex:gpt-6-2-sol"
        ui_avail = "claude:claude-sonnet-5-5,antigravity:claude-sonnet-5-5,antigravity:gemini-4-neon,codex:gpt-6-2-sol"
        base = lambda assets, avail, *more: ["select", "--available", avail, *more, *w.common(assets)]  # noqa: E731

        def case(argv, rc, final, *shows):
            return Case(argv, 4, re.compile(final), rc=rc, shows=shows, env=w.env, cwd=w.dir)

        return [
            case(base(exhausted, both, "--complexity", "hard", "--risk", "high", "--ref", "a-stage"), 0,
                 r"^action: stage: dispatch Phase 1 \S+ @ \S+ now; switch to \S+ @ \S+ at the Phase 1 boundary \(earliest reset 2026-11-23T08:00:00\+00:00\)",
                 "followup recorded: [resume] a-stage"),
            case(base(exhausted, both, "--complexity", "hard", "--risk", "high", "--below-tier", "proceed", "--ref", "a-proceed"), 0,
                 r"^action: dispatch \S+ @ \S+ now \(below T4\);.*provisional", "followup recorded: [resume] a-proceed"),
            case(base(exhausted, both, "--complexity", "hard", "--risk", "high", "--below-tier", "wait", "--ref", "a-wait"), 4,
                 r"^action: hold until 2026-11-23T08:02:00\+00:00; follow-up (?:recorded|in place): schedule its wakeup$",
                 "followup recorded: [resume] a-wait"),
            case(base(ui["defer"], ui_avail, "--complexity", "standard", "--risk", "low", "--ui", "--ref", "a-ui"), 3,
                 r"^action: hold until \S+ \(preferred ui-ux-review exhausted\); record a resume follow-up$"),
            case(base(ui["ask"], ui_avail, "--complexity", "standard", "--risk", "low", "--ui", "--ref", "a-ui"), 3,
                 r"^action: ask the user once \(preferred ui-ux-review exhausted"),
            case(base(ui["proceed"], ui_avail, "--complexity", "standard", "--risk", "low", "--ui", "--ref", "a-ui"), 0,
                 r"^action: dispatch \S+ @ \S+ now; .*mark it provisional", "followup recorded: [ui-review] a-ui"),
            case(base(exhausted, both, "--complexity", "hard", "--risk", "medium", "--implementer", "gpt-6-2-sol"), 0,
                 r"^action: dispatch reviewer \S+ @ \S+ now; state the reviewer degradation in the report", "[note] reviewer degraded"),
            case(base(exhausted, both, "--complexity", "hard", "--risk", "high", "--implementer", "gpt-6-2-sol", "--ref", "a-rev-wait"), 4,
                 r"^action: hold the review until \S+; follow-up (?:recorded|in place): schedule its wakeup$",
                 "followup recorded: [resume] a-rev-wait"),
            case(base(exhausted, both, "--complexity", "hard", "--risk", "high", "--below-tier", "proceed", "--implementer", "gpt-6-2-sol", "--ref", "a-rev-proceed-wait"), 4,
                 r"^action: hold the review until \S+; follow-up (?:recorded|in place): schedule its wakeup$",
                 "followup recorded: [resume] a-rev-proceed-wait"),
        ]

    def launch_cases(self, w, assets, avail):
        w.write("GOAL: Contract test brief.\n", "brief.md")
        ok_spec = w.write(
            "version: 1\nrole: orchestrator\ntitle: contract launch\nbrief: brief.md\n"
            "rubric: {size: 2, uncertainty: 1, deps: 2, consequences: 1, verification: 1, coordination: 2}\n"
            "workspace: {strategy: worktree, base: origin/main, children: shared}\n",
            "ok.launch.yaml",
        )
        bad_lines = ["version: 2", "role: bad", "brief: no-such.md", "rubric: {}", "workspace: {strategy: bad}"]
        for i in range(15):
            bad_lines.append("extra%02d: 1" % i)
        bad_spec = w.write("\n".join(bad_lines) + "\n", "bad.launch.yaml")
        long_dir = os.path.join(w.dir, "deeply-nested-launch-plan-directory-for-contract-test")
        os.makedirs(long_dir, exist_ok=True)
        with open(os.path.join(long_dir, "brief.md"), "w", encoding="utf-8") as fh:
            fh.write("GOAL: Long path contract test.\n")
        long_spec = os.path.join(long_dir, "orch-role-pr2-review-with-a-long-name.launch.yaml")
        with open(long_spec, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\nrole: orchestrator\ntitle: long name contract launch\nrepo: osnoser1/agent-orchestration\nbrief: brief.md\n"
                "rubric: {size: 2, uncertainty: 1, deps: 2, consequences: 1, verification: 1, coordination: 2}\n"
                "workspace: {strategy: worktree, base: origin/main, children: shared}\n"
            )
        launch_avail = "claudeAgent:claude-opus-5-5," + avail.partition(",")[2]
        long_avail = "claudeAgent_claude_extra:claude-opus-5-5," + avail.partition(",")[2]
        return [
            Case(
                ["launch", ok_spec, "--no-probe", "--available", launch_avail, *w.common(assets)],
                9, ORCH_FINAL, last="ex39", env=w.env, cwd=w.dir,
                shows=("role: orchestrator", "requested: claudeAgent:claude-opus-5-5@high", "payload:", "log: orch.py log"),
            ),
            Case(
                ["launch", long_spec, "--no-probe", "--available", long_avail, *w.common(assets)],
                9, ORCH_FINAL, last="ex39", env=w.env, cwd=w.dir,
                shows=("requested: claudeAgent_claude_extra:claude-opus-5-5@high", "log: orch.py log"),
            ),
            Case(["launch", bad_spec, "--no-probe", *w.common(assets)], 13, CHECK_FINAL, rc=1, last="extra14", env=w.env, cwd=w.dir),
        ]

    def scope_cases(self, w):
        repo = os.path.join(w.dir, "repo")
        os.makedirs(repo)
        cfg = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]

        def git(*argv):
            subprocess.run(["git", *cfg, *argv], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        git("init", "-q", "-b", "main")
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("a\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "feature")
        os.makedirs(os.path.join(repo, "elsewhere"))
        for i in range(200):  # outside --expect, over budget; 40 are also strays or manifests
            name = "elsewhere/f%03d%s" % (i, ".log" if i % 5 == 0 else "")
            with open(os.path.join(repo, name), "w") as fh:
                fh.write("x\n" * 5)
        for i in range(20):
            os.makedirs(os.path.join(repo, "pkg%02d" % i))
            with open(os.path.join(repo, "pkg%02d" % i, "package.json"), "w") as fh:
                fh.write("{}\n")
        base = ["scope", "--base", "main", "--repo", repo, "--max-files", "50", "--max-lines", "100", "--expect", "src/**"]
        return [
            Case(base, 3, ORCH_FINAL, last="f199", env=w.env, cwd=w.dir),
            Case(["scope", "--base", "no-such-ref", "--repo", repo], 4, ORCH_FINAL, rc=1, env=w.env, cwd=w.dir),
        ]

    def check_cases(self, w):
        # 60 providers, only 3 offered: 57 notes in one group
        lines = ["version: 1", "providers:"] + ["  - id: p%02d" % i for i in range(60)]
        ok_assets = w.write("\n".join(lines) + "\n", "ok-assets.yaml")
        # 40 providers with a bad status: 40 errors
        lines = ["version: 1", "providers:"]
        for i in range(40):
            lines += ["  - id: bad%02d" % i, "    status: nonsense"]
        bad_assets = w.write("\n".join(lines) + "\n", "bad-assets.yaml")
        return [
            Case(["check", "--assets", ok_assets, "--now", NOW, "--lockfile", "", "--available", "p00:claude-opus-5-5"], 5,
                 CHECK_FINAL, last="p59", env=w.env, cwd=w.dir),
            Case(["check", "--assets", bad_assets, "--now", NOW, "--lockfile", ""], 13, CHECK_FINAL, rc=1, last="bad39", env=w.env, cwd=w.dir),
        ]

    def probe_cases(self, w):
        assets = w.probe_assets()
        grouped = w.probe_assets(exhausted=0, ok=3, name="grouped-assets.yaml")
        unsaved = os.path.join(w.dir, "never-written.yaml")  # no assets file: results are not saved
        return [
            Case(["probe", *w.common(assets)], 12, ORCH_FINAL, rc=4, last="ex29", env=w.env, cwd=w.dir),
            Case(["probe", *w.common(grouped)], 5, ORCH_FINAL, shows=("[not ok: Claude and GPT models]",), env=w.env, cwd=w.dir),
            Case(["probe", *w.common(unsaved)], 4, re.compile(r"^action: .*results not saved .*offer probe --init to the user"),
                 shows=("[note] no assets file",), env=w.env, cwd=w.dir),
        ]

    def policy_cases(self, w):
        ok_repo = os.path.join(w.dir, "policy-ok-repo")
        os.makedirs(ok_repo)
        globs = "; ".join("area%02d/**" % i for i in range(30))
        failures = "; ".join("/err%02d/ => rerun" % i for i in range(25))
        with open(os.path.join(ok_repo, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "# AGENTS\n\n## Orchestration\n"
                "- Implement: plan-exec\n"
                "- High risk: %s\n"
                "- Models: UI/UX work → family:anthropic\n"
                "- Known failures: %s\n" % (globs, failures)
            )
        bad_repo = os.path.join(w.dir, "policy-bad-repo")
        os.makedirs(bad_repo)
        bad_lines = "\n".join("- Unknown%02d: x" % i for i in range(25))
        with open(os.path.join(bad_repo, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write("# AGENTS\n\n## Orchestration\n" + bad_lines + "\n")
        return [
            Case(["policy", "--repo", ok_repo, "--assets", w.assets], 8, CHECK_FINAL, last="area29/**", env=w.env, cwd=w.dir),
            Case(["policy", "--repo", bad_repo, "--assets", w.assets], 15, CHECK_FINAL, rc=1, last="Unknown24", env=w.env, cwd=w.dir),
        ]

    def test_every_subcommand_has_a_contract_case(self):
        orch = load_module("orch_contract", ORCH)
        sub = next(a for a in orch.build_parser()._actions if hasattr(a, "choices") and a.choices and a.dest == "cmd")
        self.assertEqual(sorted(self.cases()), sorted(sub.choices), "add a contract case for every orch.py subcommand")

    def test_compact_output_is_bounded_and_ends_with_one_decision(self):
        for name, cases in self.cases().items():
            for i, case in enumerate(cases):
                with self.subTest(command=name, case=i):
                    (rc, lines), (rc_full, full) = run(ORCH, case)
                    assert_contract(self, case, rc, lines, rc_full, full)

    def test_help_is_short_and_has_an_example(self):
        orch = load_module("orch_contract_help", ORCH)
        sub = next(a for a in orch.build_parser()._actions if a.dest == "cmd")
        assert_help(self, ORCH, sub.choices, "orch.py")


def assert_contract(tc, case, rc, lines, rc_full, full):
    text = "\n".join(lines)
    tc.assertEqual(rc, case.rc, text)
    tc.assertEqual(rc_full, case.rc, "--full must not change the exit code")
    tc.assertLessEqual(len(lines), case.budget, "compact output over budget:\n" + text)
    tc.assertNotIn(ESC, text + "\n".join(full))
    tc.assertLessEqual(max(len(l) for l in lines), LINE_CHARS, "a compact line is too long:\n" + text)
    tc.assertTrue(case.final.match(lines[-1]), "last line is not the decision:\n" + text)
    tc.assertEqual(len([l for l in lines if case.final.match(l)]), 1, "exactly one decision line:\n" + text)
    tc.assertTrue(case.final.match(full[-1]), "--full must end with the same decision line:\n" + "\n".join(full))
    for item in case.shows:
        tc.assertIn(item, text)
    for item in case.hides:
        tc.assertNotIn(item, text)
        tc.assertIn(item, "\n".join(full), "--full must still show %r" % item)
    if case.last:
        tc.assertIn(MORE, text)
        tc.assertNotIn(case.last, text, "compact output must cap the list")
        tc.assertIn(case.last, "\n".join(full), "--full must show everything")
        tc.assertNotIn(MORE, "\n".join(full))
        tc.assertGreater(len(full), len(lines))


def assert_help(tc, script, subcommands, label):
    top = subprocess.run([sys.executable, "-I", script, "--help"], stdout=subprocess.PIPE, universal_newlines=True).stdout
    tc.assertLessEqual(len(top.splitlines()), TOP_HELP_LINES, label)
    tc.assertRegex(top, r"(?i)exit codes?", "%s --help must list the exit codes" % label)
    for name in subcommands:
        out = subprocess.run([sys.executable, "-I", script, name, "--help"], stdout=subprocess.PIPE, universal_newlines=True).stdout
        tc.assertLessEqual(len(out.splitlines()), HELP_LINES, "%s %s --help is too long" % (label, name))
        tc.assertRegex(out, r"(?m)^example: python3 \S+ %s\b" % re.escape(name), "%s %s --help needs one example" % (label, name))


# ---------- large fixtures for tools/repo.py ----------
class RepoContract(unittest.TestCase):
    def setUp(self):
        self.repo = load_module("repo_contract", REPO)

    def fixture(self, mutate=None):
        tmp = tempfile.mkdtemp(prefix="repo-contract-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.repo.write_fixture(tmp)
        if mutate:
            mutate(tmp)
        return tmp

    def broken(self, root):
        links = "".join("[x](docs/missing%03d.md)\n" % i for i in range(100))
        leaks = "".join("See /home/alice%02d/notes.\n" % i for i in range(30))
        self.repo._append(root, "README.md", "\n" + links + leaks)

    def cases(self):
        broken = self.fixture(self.broken)
        clean = self.fixture()
        no_base = self.fixture()
        return {
            "check": [
                Case(["check", "--root", broken], 25, CHECK_FINAL, rc=1, last="missing099"),
                Case(["check", "--root", clean], 2, CHECK_FINAL),
                Case(["check", "--root", no_base, "--base", "origin/no-such-ref"], 4, CHECK_FINAL, rc=1),
                Case(["check", "--self-test"], 1, CHECK_FINAL),
            ],
            "bump": [
                Case(lambda: ["bump", "1.1.0", "--root", self.fixture()], 1, CHECK_FINAL),
                Case(lambda: ["bump", "0.0.1", "--root", self.fixture()], 2, CHECK_FINAL, rc=1),
                Case(lambda: ["bump", "nope", "--root", self.fixture()], 2, CHECK_FINAL, rc=1),
            ],
            "release": [self.release_case()],
        }

    def release_case(self):
        repo = self.repo

        class Fake:
            def __call__(self, argv, cwd):
                if argv[:2] == ["git", "status"]:
                    return 0, " M x\n"
                if argv[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                    return 0, "feature\n"
                return 1, "no network"

        root = self.fixture()

        def runner(extra):
            lines = []
            rc = repo.cmd_release(root, False, Fake(), lambda r: repo.Report(), out=lines.append, full="--full" in extra)
            return rc, lines

        return Case(["release"], 12, CHECK_FINAL, rc=1, runner=runner)

    def test_every_subcommand_has_a_contract_case(self):
        sub = next(a for a in self.repo.build_parser()._actions if a.dest == "cmd")
        self.assertEqual(sorted(self.cases()), sorted(sub.choices), "add a contract case for every repo.py subcommand")

    def test_compact_output_is_bounded_and_ends_with_one_decision(self):
        for name, cases in self.cases().items():
            for i, case in enumerate(cases):
                with self.subTest(command=name, case=i):
                    (rc, lines), (rc_full, full) = run(REPO, case)
                    assert_contract(self, case, rc, lines, rc_full, full)

    def test_self_test_is_one_line_unless_full(self):
        (rc, lines), (_, full) = run(REPO, Case(["check", "--self-test"], 1, CHECK_FINAL))
        self.assertEqual((rc, len(lines)), (0, 1), lines)
        self.assertGreater(len(full), 20)
        self.assertTrue(all("fired" in l or "pass" in l for l in full[:-1]))

    def test_check_errors_say_what_to_do(self):
        root = self.fixture(lambda r: self.repo._append(r, "README.md", "\n[x](docs/missing.md)\n"))
        (_, lines), _ = run(REPO, Case(["check", "--root", root], 5, CHECK_FINAL, rc=1))
        self.assertIn("fix or remove the broken link", lines[-1])
        (_, lines), _ = run(REPO, Case(["check", "--root", root, "--base", "origin/no-such-ref"], 5, CHECK_FINAL, rc=1))
        self.assertIn("run: git fetch", lines[-1])  # the closing line carries the git hint, not "run bump"
        self.assertNotIn("bump", lines[-1])

    def test_help_is_short_and_has_an_example(self):
        sub = next(a for a in self.repo.build_parser()._actions if a.dest == "cmd")
        assert_help(self, REPO, sub.choices, "repo.py")


if __name__ == "__main__":
    unittest.main()
