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


LS_REMOTE = ("git", "ls-remote", "--tags", "origin", "refs/tags/v1.0.0", "refs/tags/v1.0.0^{}")
LOCAL_TAG = ("git", "rev-parse", "--verify", "--quiet", "refs/tags/v1.0.0^{commit}")
RELEASE_VIEW = ("gh", "release", "view", "v1.0.0", "--repo", "acme/widgets")


class FakeRun:
    """Stand-in for repo.sh: answers git/gh calls from a table and records them."""

    def __init__(self, overrides=None):
        self.answers = {
            ("git", "status", "--porcelain"): (0, ""),
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
            ("git", "fetch", "--quiet", "origin", "main"): (0, ""),
            ("git", "rev-parse", "HEAD"): (0, "abc123\n"),
            ("git", "rev-parse", "origin/main"): (0, "abc123\n"),
            ("git", "remote", "get-url", "origin"): (0, "git@github.com:acme/widgets.git\n"),
            LOCAL_TAG: (1, ""),
            LS_REMOTE: (0, ""),
            RELEASE_VIEW: (1, "release not found"),
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
            rc = repo.main(["check", "--self-test", "--full"])
        self.assertEqual(rc, 0, buf.getvalue())
        for rule in ("version", "harness", "links", "budget", "leak", "model", "pinned", "skills"):
            self.assertIn("%s fired" % rule, buf.getvalue())

    def test_harness_registry_is_alphabetical(self):
        with open(os.path.join(REPO_ROOT, "tools", "harnesses.json"), encoding="utf-8") as fh:
            names = json.load(fh)
        self.assertEqual(names, sorted(names, key=str.casefold))


class TestCheckRules(unittest.TestCase):
    def test_fixture_is_clean(self):
        self.assertEqual(repo.run_checks(fixture_dir(self)).errors, [])

    def test_word_budget_counts_exactly_the_body(self):
        report = repo.run_checks(fixture_dir(self))
        notes = [t for k, t in report.lines if k == "note"]
        self.assertEqual(notes, ["skills/agent-orchestration/SKILL.md body: 13/%d words" % repo.SKILL_WORD_BUDGET])

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


class TestRelease(unittest.TestCase):
    TAG = ("git", "tag", "-a", "v1.0.0", "-m", "Release v1.0.0")
    PUSH = ("git", "push", "origin", "refs/tags/v1.0.0")
    GH = ("gh", "release", "create", "v1.0.0", "--repo", "acme/widgets", "--generate-notes", "--verify-tag")
    ALL = [TAG, PUSH, GH]

    def release(self, run, publish=False, check_fn=ok_check):
        root = fixture_dir(self)
        lines = []
        rc = repo.cmd_release(root, publish, run, check_fn, out=lines.append)
        return rc, lines

    def assertBlocked(self, run, message):
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any(message in l for l in lines), lines)
        self.assertEqual([c for c in run.calls if c in self.ALL], [], "nothing may run while blocked")

    # preconditions
    def test_dirty_tree(self):
        self.assertBlocked(FakeRun({("git", "status", "--porcelain"): (0, " M x\n")}), "working tree is not clean")

    def test_detached_head_at_origin_main_releases(self):
        rc, lines = self.release(FakeRun({("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "HEAD\n")}))
        self.assertEqual(rc, 0, lines)
        self.assertIn("note: releasing from detached HEAD at origin/main", lines)

    def test_branch_at_origin_main_releases_with_note(self):
        run = FakeRun({("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "feat/x\n")})
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 0, lines)
        self.assertIn("note: releasing from feat/x at origin/main", lines)
        self.assertEqual([c for c in run.calls if c in self.ALL], self.ALL)

    def test_on_main_adds_no_branch_note(self):
        rc, lines = self.release(FakeRun())
        self.assertEqual(rc, 0, lines)
        self.assertFalse(any("releasing from" in l for l in lines), lines)

    def test_branch_not_at_origin_main_is_blocked(self):
        self.assertBlocked(FakeRun({
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "feat/x\n"),
            ("git", "rev-parse", "origin/main"): (0, "def456\n"),
        }), "HEAD is not origin/main")

    def test_head_differs_from_origin(self):
        self.assertBlocked(FakeRun({("git", "rev-parse", "origin/main"): (0, "def456\n")}), "HEAD is not origin/main")

    def test_failing_check_blocks_release(self):
        rc, lines = self.release(FakeRun(), publish=True, check_fn=failing_check)
        self.assertEqual(rc, 1)
        self.assertTrue(any("check has 1 error" in l for l in lines))

    def test_version_mismatch_blocks_release(self):
        root = fixture_dir(self)
        with open(os.path.join(root, ".claude-plugin/plugin.json"), "w", encoding="utf-8") as fh:
            fh.write('{"version": "1.0.1"}\n')
        lines = []
        rc = repo.cmd_release(root, True, FakeRun(), ok_check, out=lines.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any("versions are not in sync" in l for l in lines))

    def test_all_problems_are_reported_together(self):
        rc, lines = self.release(FakeRun({
            ("git", "status", "--porcelain"): (0, " M x\n"),
            ("git", "rev-parse", "origin/main"): (0, "def456\n"),
        }))
        self.assertEqual(rc, 1)
        self.assertEqual(len([l for l in lines if l.startswith("error: release:")]), 2)

    def test_unreachable_origin_blocks_release(self):
        self.assertBlocked(FakeRun({("git", "fetch", "--quiet", "origin", "main"): (1, "")}), "git fetch origin main failed")
        self.assertBlocked(FakeRun({LS_REMOTE: (128, "")}), "cannot query origin tags")

    def test_origin_url_must_name_a_github_repo(self):
        self.assertBlocked(FakeRun({("git", "remote", "get-url", "origin"): (0, "/srv/git/widgets\n")}), "cannot derive owner/repo")

    def test_tag_at_another_commit_blocks_release(self):
        self.assertBlocked(FakeRun({LOCAL_TAG: (0, "def456\n")}), "exists locally at def456, not at HEAD")
        self.assertBlocked(FakeRun({LS_REMOTE: (0, "def456\trefs/tags/v1.0.0\n")}), "exists on origin at def456, not at HEAD")

    def test_existing_release_blocks_release(self):
        run = FakeRun({LS_REMOTE: (0, "abc123\trefs/tags/v1.0.0\n"), RELEASE_VIEW: (0, "{}")})
        self.assertBlocked(run, "release v1.0.0 already exists")

    def test_release_lookup_failure_is_not_read_as_absent(self):
        run = FakeRun({LS_REMOTE: (0, "abc123\trefs/tags/v1.0.0\n"), RELEASE_VIEW: (1, "HTTP 502")})
        self.assertBlocked(run, "cannot query GitHub releases")

    # fresh publish
    def test_dry_run_prints_commands_and_runs_none(self):
        run = FakeRun()
        rc, lines = self.release(run, publish=False)
        self.assertEqual(rc, 0)
        self.assertTrue(any("dry run" in l for l in lines))
        for cmd in self.ALL:
            self.assertIn("  " + " ".join(cmd), lines)
            self.assertNotIn(cmd, run.calls)

    def test_publish_runs_tag_push_release_in_order(self):
        run = FakeRun()
        self.assertEqual(self.release(run, publish=True)[0], 0)
        self.assertEqual([c for c in run.calls if c in self.ALL], self.ALL)

    def test_gh_gets_the_repo_and_push_names_the_full_ref(self):
        self.assertEqual(self.GH[4:6], ("--repo", "acme/widgets"))
        self.assertEqual(self.PUSH[-1], "refs/tags/v1.0.0")
        run = FakeRun({("git", "remote", "get-url", "origin"): (0, "https://github.com/acme/widgets\n")})
        self.assertEqual(self.release(run, publish=True)[0], 0)
        self.assertIn(self.GH, run.calls)

    def test_failed_step_stops_the_run_and_prints_recovery(self):
        run = FakeRun({self.PUSH: (1, "denied")})
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 1)
        self.assertNotIn(self.GH, run.calls)
        self.assertTrue(any("denied" in l for l in lines))
        self.assertTrue(any(l.startswith("recover:") and "release --publish" in l for l in lines))

    # resume after a partial publish
    def test_gh_failure_prints_the_exact_recovery_command(self):
        rc, lines = self.release(FakeRun({self.GH: (1, "HTTP 502")}), publish=True)
        self.assertEqual(rc, 1)
        self.assertIn("recover: tag v1.0.0 is on origin; finish with: " + " ".join(self.GH), lines)

    def test_rerun_after_push_succeeded_resumes_at_gh(self):
        # first run pushed the tag, then `gh release create` failed; origin and local both hold the tag at HEAD
        run = FakeRun({LOCAL_TAG: (0, "abc123\n"), LS_REMOTE: (0, "abc123\trefs/tags/v1.0.0\n")})
        rc, lines = self.release(run, publish=True)
        self.assertEqual(rc, 0, lines)
        self.assertEqual([c for c in run.calls if c in self.ALL], [self.GH])
        self.assertTrue(any(l.startswith("note:") and "resuming at gh" in l for l in lines))

    def test_rerun_resumes_at_push_when_only_the_local_tag_exists(self):
        run = FakeRun({LOCAL_TAG: (0, "abc123\n")})
        self.assertEqual(self.release(run, publish=True)[0], 0)
        self.assertEqual([c for c in run.calls if c in self.ALL], [self.PUSH, self.GH])

    def test_annotated_remote_tag_is_compared_by_its_peeled_commit(self):
        listing = "tagobj1\trefs/tags/v1.0.0\nabc123\trefs/tags/v1.0.0^{}\n"
        run = FakeRun({LS_REMOTE: (0, listing)})
        self.assertEqual(self.release(run, publish=True)[0], 0)
        self.assertEqual([c for c in run.calls if c in self.ALL], [self.TAG, self.GH])


def git_fixture(test, init_args=()):
    root = fixture_dir(test)
    repo._git_repo(root, init_args)
    return root


def git(root, *args):
    rc, out = repo.sh(["git"] + repo.GIT_FIXTURE_CFG + list(args), root)
    assert rc == 0, out
    return out


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class TestFileDiscovery(unittest.TestCase):
    def test_ignored_files_are_skipped_when_git_is_a_file(self):
        # a worktree or submodule keeps `.git` as a file, not a directory
        gitdir = tempfile.mkdtemp(prefix="test-gitdir-")
        self.addCleanup(shutil.rmtree, gitdir, True)
        root = fixture_dir(self)
        write(root, ".gitignore", "__pycache__/\n")
        repo._git_repo(root, ["--separate-git-dir", os.path.join(gitdir, "g")])
        self.assertTrue(os.path.isfile(os.path.join(root, ".git")))
        write(root, "__pycache__/n.md", "see /home/bob/x\n")
        write(root, "docs/new.md", "ok\n")
        self.assertEqual(repo.run_checks(root).errors, [])
        self.assertIn("docs/new.md", repo.list_md(root))  # untracked but not ignored
        self.assertNotIn("__pycache__/n.md", repo.list_md(root))

    def test_untracked_unignored_file_is_still_linted(self):
        root = git_fixture(self)
        write(root, "docs/new.md", "see /home/bob/x\n")
        self.assertTrue(any(e.startswith("leak:") for e in repo.run_checks(root).errors))

    def test_walk_fallback_when_git_fails(self):
        root = fixture_dir(self)  # not a repository
        write(root, "docs/new.md", "see /home/bob/x\n")
        self.assertTrue(any(e.startswith("leak:") for e in repo.run_checks(root).errors))


class TestBaseGuard(unittest.TestCase):
    def test_renaming_a_shipped_file_still_needs_a_bump(self):
        root = git_fixture(self)
        git(root, "mv", "skills/agent-orchestration/references/harnesses.md", "moved.md")
        errors = repo.run_checks(root, base="HEAD").errors
        self.assertTrue(any(e.startswith("version:") and "changed since HEAD" in e for e in errors), errors)

    def test_failed_git_diff_is_an_error_not_a_pass(self):
        root = git_fixture(self)

        def run(argv, cwd):
            return (128, "fatal: boom") if argv[:2] == ["git", "diff"] else repo.sh(argv, cwd)

        errors = repo.run_checks(root, base="HEAD", run=run).errors
        self.assertTrue(any(e.startswith("version-git:") and "git diff" in e and "boom" in e for e in errors), errors)

    def test_failed_ls_files_is_an_error_not_a_pass(self):
        root = git_fixture(self)

        def run(argv, cwd):
            return (128, "fatal: boom") if argv[:3] == ["git", "ls-files", "--others"] else repo.sh(argv, cwd)

        errors = repo.run_checks(root, base="HEAD", run=run).errors
        self.assertTrue(any(e.startswith("version-git:") and "ls-files" in e for e in errors), errors)

    def test_a_skipped_bump_check_shows_in_the_closing_line(self):
        root = git_fixture(self)
        write(root, "skills/agent-orchestration/references/harnesses.md", "# Harnesses\n\n" + repo.FIX_TABLE + "\nMore.\n")

        def run(argv, cwd):
            return (128, "") if argv[:2] == ["git", "show"] else repo.sh(argv, cwd)

        report = repo.run_checks(root, base="HEAD", run=run)
        self.assertEqual(report.errors, [])
        self.assertIn("skipping bump check", report.closing())

    def test_missing_merge_base_closing_line_points_at_git_not_bump(self):
        root = git_fixture(self)
        closing = repo.run_checks(root, base="no-such-ref").closing()
        self.assertIn("run: git fetch", closing)
        self.assertNotIn("bump", closing)

    def test_missing_merge_base_is_an_error(self):
        root = git_fixture(self)
        errors = repo.run_checks(root, base="no-such-ref").errors
        self.assertTrue(any(e.startswith("version-git:") and "merge-base" in e for e in errors), errors)


class TestMarkdownRules(unittest.TestCase):
    def errors_for(self, readme_tail, allowlist=None):
        root = fixture_dir(self)
        with open(os.path.join(root, "README.md"), "a", encoding="utf-8") as fh:
            fh.write(readme_tail)
        if allowlist is not None:
            write(root, "tools/model-id-allowlist.json", json.dumps(allowlist))
        return repo.run_checks(root).errors

    def test_fence_closes_only_on_a_bare_fence(self):
        text = "\n```\n```python\n[x](docs/missing.md)\n```\n\n[y](docs/also-missing.md)\n"
        errors = self.errors_for(text)
        self.assertEqual(len(errors), 1, errors)  # only the link after the real close is checked
        self.assertIn("also-missing", errors[0])

    def test_footnote_definitions_are_not_links(self):
        self.assertEqual(self.errors_for("\nText[^1].\n\n[^1]: A footnote about things.\n"), [])

    def test_reference_definitions_are_still_checked(self):
        self.assertTrue(any("nope.md" in e for e in self.errors_for("\n[ref]: docs/nope.md\n")))

    def test_emphasis_markers_do_not_reach_the_slug(self):
        text = "\n## The _leading_ **word**\n## snake_case\n\n[a](#the-leading-word) [b](#snake_case)\n"
        self.assertEqual(self.errors_for(text), [])
        self.assertEqual(repo.heading_slugs("## A_b and _c_\n"), {"a_b-and-c"})

    def test_html_anchor_tags_are_link_targets(self):
        text = '\n<a id="Custom-Anchor"></a>\n<a name=\'old\'></a>\n\n[a](#custom-anchor) [b](#old)\n'
        self.assertEqual(self.errors_for(text), [])

    def test_ssh_remote_is_not_an_email_leak(self):
        self.assertEqual(self.errors_for("\nclone git@github.com:acme/widgets.git\n"), [])
        self.assertTrue(any(e.startswith("leak:") for e in self.errors_for("\nmail alice@corp.com\n")))

    def test_wildcard_model_prefix_needs_an_exact_allowlist_token(self):
        # `claude-opus-*` reads as the token `claude-opus`; the allowlist has no globbing
        self.assertTrue(any(e.startswith("model:") and "claude-opus " in e for e in self.errors_for("\nUse claude-opus-* models.\n")))
        self.assertEqual(self.errors_for("\nUse claude-opus-* models.\n", allowlist=["claude-opus"]), [])
        self.assertTrue(self.errors_for("\nUse claude-opus-9-9.\n", allowlist=["claude-opus"]))


class TestCliRule(unittest.TestCase):
    def test_mentions_come_from_code_only_with_flags_per_mention(self):
        text = (
            "Prose orch.py nope is ignored. Run `orch.py check --full` then `repo.py bump 1.0.0 --root X`.\n"
            "```bash\n"
            "python3 scripts/orch.py select --a 1 \\\n  --b 2   # orch.py log --c\n"
            "```\n"
            "~~~\nfenced orch.py status ok\n~~~\n"
        )
        self.assertEqual(repo.cli_mentions(text), [
            (1, "orch", "check", ["--full"]),
            (1, "repo", "bump", ["--root"]),
            (3, "orch", "select", ["--a", "--b"]),
            (7, "orch", "status", []),
        ])

    def test_unfinished_continuation_does_not_leak_into_the_next_fence(self):
        text = "```\nfoo \\\n```\n\n```\norch.py check\n--bogus\n```\n"
        self.assertEqual(repo.cli_mentions(text), [(6, "orch", "check", [])])

    def test_flags_stop_at_a_shell_operator(self):
        for op in ("&&", "||", "|", ";"):
            text = "`orch.py check --full %s grep --pattern`\n" % op
            self.assertEqual(repo.cli_mentions(text), [(1, "orch", "check", ["--full"])], op)

    def test_one_span_with_two_mentions_splits_the_flags(self):
        self.assertEqual(repo.cli_mentions("`orch.py check --x; orch.py select --y`\n"),
                         [(1, "orch", "check", ["--x"]), (1, "orch", "select", ["--y"])])

    def test_real_parsers_load_and_know_history(self):
        report = repo.Report()
        parsers = repo.load_cli_parsers(repo.REPO_ROOT, report)
        self.assertEqual(report.errors, [])
        self.assertIn("--last", parsers["orch"]["history"])
        self.assertIn("--self-test", parsers["repo"]["check"])
        self.assertIn("--help", parsers["repo"]["bump"])

    def test_missing_script_is_an_error(self):
        root = fixture_dir(self)
        os.remove(os.path.join(root, "tools/repo.py"))
        self.assertTrue(any(e.startswith("cli: tools/repo.py is missing") for e in repo.run_checks(root).errors))

    def test_error_messages_name_the_file_line_and_fix(self):
        root = fixture_dir(self)
        write(root, "AGENTS.md", "# A\n\nRun `orch.py check --bogus`.\n")
        errors = repo.run_checks(root).errors
        self.assertEqual(errors, ["cli: AGENTS.md:3: orch.py check has no flag --bogus"])


class TestDoneWhenRule(unittest.TestCase):
    def errors_for(self, rel, text, exempt=None):
        root = fixture_dir(self)
        write(root, rel, text)
        if exempt is not None:
            write(root, "tools/done-when-exempt.json", json.dumps(exempt))
        return [e for e in repo.run_checks(root).errors if e.startswith("done-when")]  # the overwritten fixture may trip other rules

    SKILL = "---\nname: agent-orchestration\nmetadata:\n  version: \"1.0.0\"\n---\n\n# T\n\n## 1. Step\nDo.\n\n%s\n## References\n- x\n"
    REF = "skills/agent-orchestration/references/harnesses.md"

    def test_skill_step_needs_a_done_when_before_the_next_heading(self):
        skill = "skills/agent-orchestration/SKILL.md"
        self.assertEqual(self.errors_for(skill, self.SKILL % "*Done when*: done."), [])
        self.assertEqual(self.errors_for(skill, self.SKILL % ""), ["done-when: %s: section '1. Step' has no `*Done when*` line" % skill])
        # a Done when inside the next section does not count; unnumbered sections are not steps
        self.assertEqual(len(self.errors_for(skill, self.SKILL % "" + "*Done when*: later.\n")), 1)

    def test_reference_section_with_an_ordered_list_needs_one(self):
        self.assertEqual(len(self.errors_for(self.REF, "## S\n1. a\n2. b\n")), 1)
        self.assertEqual(self.errors_for(self.REF, "## S\n1. a\n2. b\n\n*Done when*: ok.\n"), [])

    def test_single_item_nested_and_bullet_lists_are_not_procedures(self):
        for body in ("1. only\n", "- a\n  1. x\n  2. y\n", "- a\n- b\n"):
            self.assertEqual(self.errors_for(self.REF, "## S\n" + body), [], body)

    def test_ordered_list_inside_a_fence_is_ignored(self):
        self.assertEqual(self.errors_for(self.REF, "## S\n```\n1. a\n2. b\n```\n"), [])

    def test_exemption_covers_a_lookup_and_a_stale_one_errors(self):
        self.assertEqual(self.errors_for(self.REF, "## Rank\n1. a\n2. b\n", exempt={self.REF: ["Rank"]}), [])
        stale = self.errors_for(self.REF, "## Rank\n- a\n", exempt={self.REF: ["Gone"]})
        self.assertEqual(stale, ["done-when: tools/done-when-exempt.json: %s has no section 'Gone'" % self.REF])

    def test_exemption_for_a_missing_file_or_a_non_list_is_an_error(self):
        gone = self.errors_for(self.REF, "## S\n- a\n", exempt={"skills/agent-orchestration/references/nope.md": ["S"]})
        self.assertEqual(gone, ["done-when: tools/done-when-exempt.json: skills/agent-orchestration/references/nope.md has no section 'S'"])
        bad = self.errors_for(self.REF, "## S\n- a\n", exempt={self.REF: "S"})
        self.assertEqual(bad, ["done-when: tools/done-when-exempt.json must map a file to a list of section titles"])

    def test_real_exemptions_name_real_sections(self):
        self.assertEqual([e for e in repo.run_checks(repo.REPO_ROOT).errors if e.startswith("done-when")], [])


if __name__ == "__main__":
    unittest.main()
