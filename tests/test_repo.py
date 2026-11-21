import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("repo_tool", os.path.join(REPO_ROOT, "tools", "repo.py"))
repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo)


def fixture_dir(test):
    tmp = tempfile.mkdtemp(prefix="test-repo-")
    test.addCleanup(shutil.rmtree, tmp, True)
    repo.write_fixture(tmp)
    return tmp


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


class FakeRun:
    """Stand-in for repo.sh: answers git/gh calls from a table and records them."""

    def __init__(self, overrides=None):
        self.answers = {
            ("git", "status", "--porcelain"): (0, ""),
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
            ("git", "fetch", "--quiet", "origin", "main"): (0, ""),
            ("git", "rev-parse", "HEAD"): (0, "abc123\n"),
            ("git", "rev-parse", "origin/main"): (0, "abc123\n"),
            ("git", "tag", "--list", "v1.0.0"): (0, ""),
            ("git", "ls-remote", "--tags", "origin", "refs/tags/v1.0.0"): (0, ""),
        }
        self.answers.update(overrides or {})
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(tuple(argv))
        return self.answers.get(tuple(argv), (0, ""))


def ok_check(root):
    return repo.Report()


def failing_check(root):
    r = repo.Report()
    r.error("links", "boom")
    return r


class TestRealRepo(unittest.TestCase):
    def test_check_passes_on_this_tree(self):
        self.assertEqual(repo.run_checks(REPO_ROOT).errors, [])

    def test_self_test_fires_every_rule(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = repo.main(["check", "--self-test"])
        self.assertEqual(rc, 0, buf.getvalue())
        for rule in ("version", "harness", "links", "budget", "leak", "model", "pinned", "skills"):
            self.assertIn("%s fired" % rule, buf.getvalue())

    def test_harness_registry_is_alphabetical(self):
        with open(os.path.join(REPO_ROOT, "tools", "harnesses.json"), encoding="utf-8") as fh:
            names = json.load(fh)
        self.assertEqual(names, sorted(names, key=str.casefold))

    def test_pinned_rules_cover_recent_releases(self):
        with open(os.path.join(REPO_ROOT, "tools", "pinned-rules.json"), encoding="utf-8") as fh:
            sources = " ".join(r["source"] for r in json.load(fh))
        for pr in ("#16", "#15", "#13"):
            self.assertIn(pr, sources)


class TestCheckRules(unittest.TestCase):
    def test_fixture_is_clean(self):
        self.assertEqual(repo.run_checks(fixture_dir(self)).errors, [])

    def test_word_budget_counts_body_not_frontmatter(self):
        root = fixture_dir(self)
        report = repo.run_checks(root)
        notes = [t for k, t in report.lines if k == "note"]
        self.assertTrue(any("words" in n for n in notes))

    def test_anchor_resolves_against_github_slugs(self):
        slugs = repo.heading_slugs("# A `b` c\n## Dup\n## Dup\n```\n# not a heading\n```\n")
        self.assertEqual(slugs, {"a-b-c", "dup", "dup-1"})

    def test_vendored_skill_is_not_linted(self):
        root = fixture_dir(self)
        path = os.path.join(root, ".agents/skills/writing-for-agents/SKILL.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[bad](missing.md) /home/alice/x claude-opus-9-9\n")
        self.assertEqual(repo.run_checks(root).errors, [])


class TestBump(unittest.TestCase):
    def run_bump(self, root, version):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = repo.cmd_bump(root, version)
        return rc, buf.getvalue()

    def test_bump_rewrites_every_file_and_keeps_formatting(self):
        root = fixture_dir(self)
        before = {f: read(root, f) for f in (".claude-plugin/plugin.json", "skills/agent-orchestration/SKILL.md")}
        rc, _ = self.run_bump(root, "1.2.0")
        self.assertEqual(rc, 0)
        for rel, text in before.items():
            self.assertEqual(read(root, rel), text.replace("1.0.0", "1.2.0"))
        self.assertEqual(repo.run_checks(root).errors, [])

    def test_bump_preserves_crlf(self):
        root = fixture_dir(self)
        path = os.path.join(root, ".claude-plugin/plugin.json")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write('{\r\n  "version": "1.0.0"\r\n}\r\n')
        self.assertEqual(self.run_bump(root, "1.0.1")[0], 0)
        self.assertEqual(read(root, ".claude-plugin/plugin.json"), '{\r\n  "version": "1.0.1"\r\n}\r\n')

    def test_bump_refuses_bad_or_non_increasing_versions(self):
        root = fixture_dir(self)
        snapshot = read(root, ".claude-plugin/plugin.json")
        for bad in ("1.0", "v1.0.1", "1.0.0", "0.9.9", "01.0.0", "1.0.1-rc1"):
            rc, out = self.run_bump(root, bad)
            self.assertEqual(rc, 1, bad)
            self.assertIn("error:", out)
        self.assertEqual(read(root, ".claude-plugin/plugin.json"), snapshot)

    def test_bump_compares_numerically(self):
        root = fixture_dir(self)
        self.assertEqual(self.run_bump(root, "1.0.9")[0], 0)
        self.assertEqual(self.run_bump(root, "1.0.10")[0], 0)
        self.assertEqual(self.run_bump(root, "1.0.9")[0], 1)

    def test_bump_refuses_when_files_disagree(self):
        root = fixture_dir(self)
        path = os.path.join(root, ".claude-plugin/plugin.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"version": "1.0.5"}\n')
        before = read(root, "skills/agent-orchestration/SKILL.md")
        self.assertEqual(self.run_bump(root, "2.0.0")[0], 1)
        self.assertEqual(read(root, "skills/agent-orchestration/SKILL.md"), before)


class TestReleasePreconditions(unittest.TestCase):
    def problems(self, run, check_fn=ok_check):
        root = fixture_dir(self)
        return repo.release_preconditions(root, run, check_fn)

    def test_all_good(self):
        version, problems = self.problems(FakeRun())
        self.assertEqual((version, problems), ("1.0.0", []))

    def test_dirty_tree(self):
        _, problems = self.problems(FakeRun({("git", "status", "--porcelain"): (0, " M x\n")}))
        self.assertEqual(problems, ["working tree is not clean"])

    def test_not_on_main(self):
        _, problems = self.problems(FakeRun({("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "feat/x\n")}))
        self.assertEqual(problems, ["not on main (on feat/x)"])

    def test_head_differs_from_origin(self):
        _, problems = self.problems(FakeRun({("git", "rev-parse", "origin/main"): (0, "def456\n")}))
        self.assertEqual(problems, ["HEAD is not origin/main (push or pull first)"])

    def test_local_tag_exists(self):
        _, problems = self.problems(FakeRun({("git", "tag", "--list", "v1.0.0"): (0, "v1.0.0\n")}))
        self.assertEqual(problems, ["tag v1.0.0 already exists locally"])

    def test_remote_tag_exists(self):
        run = FakeRun({("git", "ls-remote", "--tags", "origin", "refs/tags/v1.0.0"): (0, "abc\trefs/tags/v1.0.0\n")})
        _, problems = self.problems(run)
        self.assertEqual(problems, ["tag v1.0.0 already exists on origin"])

    def test_unreachable_origin_blocks_release(self):
        run = FakeRun({
            ("git", "fetch", "--quiet", "origin", "main"): (1, ""),
            ("git", "ls-remote", "--tags", "origin", "refs/tags/v1.0.0"): (128, ""),
        })
        _, problems = self.problems(run)
        self.assertEqual(problems, ["git fetch origin main failed", "cannot query origin tags"])

    def test_failing_check_blocks_release(self):
        _, problems = self.problems(FakeRun(), failing_check)
        self.assertEqual(len(problems), 1)
        self.assertIn("check has 1 error", problems[0])

    def test_version_mismatch_blocks_release(self):
        root = fixture_dir(self)
        with open(os.path.join(root, ".claude-plugin/plugin.json"), "w", encoding="utf-8") as fh:
            fh.write('{"version": "1.0.1"}\n')
        version, problems = repo.release_preconditions(root, FakeRun(), ok_check)
        self.assertIsNone(version)
        self.assertIn("versions are not in sync", problems[0])

    def test_problems_are_all_reported(self):
        run = FakeRun({
            ("git", "status", "--porcelain"): (0, " M x\n"),
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "dev\n"),
        })
        _, problems = self.problems(run)
        self.assertEqual(len(problems), 2)


class TestReleaseCommand(unittest.TestCase):
    def release(self, run, publish, check_fn=ok_check):
        root = fixture_dir(self)
        lines = []
        rc = repo.cmd_release(root, publish, run, check_fn, out=lines.append)
        return rc, lines

    PUBLISH_CMDS = [
        ("git", "tag", "-a", "v1.0.0", "-m", "Release v1.0.0"),
        ("git", "push", "origin", "v1.0.0"),
        ("gh", "release", "create", "v1.0.0", "--generate-notes", "--verify-tag"),
    ]

    def test_dry_run_prints_commands_and_runs_none(self):
        run = FakeRun()
        rc, lines = self.release(run, publish=False)
        self.assertEqual(rc, 0)
        self.assertTrue(any("dry run" in l for l in lines))
        for cmd in self.PUBLISH_CMDS:
            self.assertIn("  " + " ".join(cmd), lines)
            self.assertNotIn(cmd, run.calls)

    def test_publish_runs_tag_push_release_in_order(self):
        run = FakeRun()
        rc, _ = self.release(run, publish=True)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in run.calls if c in self.PUBLISH_CMDS], self.PUBLISH_CMDS)

    def test_publish_stops_at_first_failure(self):
        run = FakeRun({self.PUBLISH_CMDS[1]: (1, "denied")})
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 1)
        self.assertNotIn(self.PUBLISH_CMDS[2], run.calls)
        self.assertTrue(any("denied" in l for l in lines))

    def test_failed_precondition_runs_nothing(self):
        run = FakeRun({("git", "status", "--porcelain"): (0, " M x\n")})
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 1)
        self.assertIn("error: release: working tree is not clean", lines)
        for cmd in self.PUBLISH_CMDS:
            self.assertNotIn(cmd, run.calls)


if __name__ == "__main__":
    unittest.main()
