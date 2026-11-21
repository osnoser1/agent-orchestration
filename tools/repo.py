#!/usr/bin/env python3
"""Maintainer tooling for this repository (not shipped with the skill).

Stdlib only, Python 3.9+; run it as `python3 -I tools/repo.py <command>`.

Commands:
  check [--base REF] [--root DIR]   repo-wide rules; exit 0 ok, 1 on any `error:` line
  check --self-test                 build broken fixture trees and assert every rule fires
  bump X.Y.Z                        write a new, higher version to every file in tools/versions.json
  release [--publish]               verify release preconditions; dry run unless --publish

Output contract (tests/test_output_contract.py is the source of truth): compact by
default, every list capped at 10 per rule followed by `… N more (rerun with --full)`,
no color, no prompts, and the last line is the decision: `ok: …` or
`error: N problem(s); action: <fix or rerun hint>`. `--full` prints every line.

Data files (all under tools/): versions.json (version locators), harnesses.json
(single source for harness lists), pinned-rules.json (rules that must stay in
the docs), model-id-allowlist.json (intentional unknown model-id tokens).

Model-id allowlist: entries are exact, case-insensitive tokens; there is no
globbing. A family prefix written in docs as `claude-opus-*` is read as the
token `claude-opus` (trailing `-` and `.` are stripped), so allow `claude-opus`;
an effort suffix (`-high`, ...) on a catalog id needs no entry.

Output: one line per problem, `error: <rule>: <message>` (fails the run);
`note:` lines (information only) appear with --full. Rules: version, version-git
(git could not answer for --base), harness, links, budget, leak, model, pinned, skills.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

SKILL_WORD_BUDGET = 1500  # SKILL.md body words
AGENTS_MAX_LINES = 150
VENDORED_PREFIXES = (".agents/skills/writing-for-agents/",)  # `npx skills update` owns these
CAP = 10  # list items shown per rule unless --full
RULE_FIX = {  # one fix hint per rule, joined into the closing `error: N problem(s); action: …` line
    "version": "run python3 tools/repo.py bump <X.Y.Z> (the version files must agree first)",
    "version-git": "run: git fetch --unshallow (or git fetch origin), then rerun with the same --base",
    "harness": "edit the list or table to match tools/harnesses.json (alphabetical)",
    "links": "fix or remove the broken link or anchor",
    "budget": "shorten the file to its budget",
    "leak": "replace the path or email with a placeholder",
    "model": "use a catalog id or alias, or add the token to tools/model-id-allowlist.json",
    "pinned": "restore the pinned phrase, or change it together with its tools/pinned-rules.json entry",
    "skills": "restore skills/*/SKILL.md",
}
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
EFFORT_SUFFIX = re.compile(r"^(.+)-(none|low|medium|high|xhigh|max|ultra)$")

# Tokens that look like model ids: Anthropic family names, gpt-<digit|o>, gemini-<digit|pro|flash>.
MODEL_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])((?:claude-(?:opus|sonnet|haiku|fable)|gpt-[0-9o]|gemini-(?:[0-9]|pro|flash))[A-Za-z0-9.-]*)"
)
LEAK_PATH_RE = re.compile(r"(?:/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\)")
EMAIL_OK_LOCALS = ("noreply", "no-reply", "git")  # `git@github.com:owner/repo` is an ssh remote, not a person
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_LINK_RE = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
ANCHOR_TAG_RE = re.compile(r"<a\s[^>]*?\b(?:id|name)=[\"']([^\"']+)[\"']", re.I)
FENCE_OPEN_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
FENCE_CLOSE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*$")  # a closing fence carries no info string
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


# ---------- plumbing ----------
def more(n):
    return "… %d more (rerun with --full)" % n


def capped(items, full):
    """`items` limited to CAP unless `full`, plus a trailing `… N more` entry when some were cut."""
    items = list(items)
    if full or len(items) <= CAP:
        return items
    return items[:CAP] + [more(len(items) - CAP)]


class Report:
    def __init__(self):
        self.lines = []  # (kind, text)
        self.briefs = []  # short facts for the closing `ok:` line

    def error(self, rule, msg):
        self.lines.append(("error", "%s: %s" % (rule, msg)))

    def note(self, msg, brief=False):
        self.lines.append(("note", msg))
        if brief:
            self.briefs.append(msg)

    @property
    def errors(self):
        return [t for k, t in self.lines if k == "error"]

    def render(self):
        return ["%s: %s" % (k, t) for k, t in self.lines]

    def render_compact(self, full=False):
        """Error lines grouped by rule and capped per rule; notes only with --full."""
        if full:
            return self.render()
        groups = {}
        for t in self.errors:
            groups.setdefault(t.split(":", 1)[0], []).append("error: " + t)
        return [line for lines in groups.values() for line in capped(lines, False)]

    def closing(self):
        """The last line: `ok: …` or `error: N problem(s); action: …`."""
        errors = self.errors
        if not errors:
            return "ok: all checks passed" + (" (%s)" % "; ".join(self.briefs) if self.briefs else "")
        rules = []
        for t in errors:
            rule = t.split(":", 1)[0]
            if rule not in rules:
                rules.append(rule)
        hints = "; ".join("%s: %s" % (r, RULE_FIX[r]) if r in RULE_FIX else r for r in rules)
        return "error: %d problem(s); action: %s, then rerun python3 tools/repo.py check" % (len(errors), hints)


def sh(argv, cwd):
    """Run a command; return (returncode, output): stdout, plus stderr when it fails. Missing binaries return 127."""
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, p.stdout if p.returncode == 0 else p.stdout + p.stderr


def read_text(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def load_json(root, rel, report, rule, required=True):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        if required:
            report.error(rule, "%s is missing" % rel)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as exc:
        report.error(rule, "%s is not valid JSON (%s)" % (rel, exc))
        return None


def semver_tuple(v):
    return tuple(int(x) for x in v.split("."))


def list_md(root):
    """Tracked (or untracked, not ignored) *.md files, minus vendored skills.

    Asks git first (`.git` is a file in worktrees and submodules); walks the tree only when git fails.
    """
    rc, out = sh(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
    rels = {p for p in out.split("\0") if p} if rc == 0 else set()
    if rc != 0:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                rels.add(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
    return sorted(
        p for p in rels
        if p.endswith(".md")
        and not p.startswith(VENDORED_PREFIXES)
        and os.path.isfile(os.path.join(root, p))
    )


def split_frontmatter(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return (text[: m.end()], text[m.end():]) if m else ("", text)


def blank_code(text, inline=True):
    """Blank fenced blocks (and optionally inline code spans), keeping line numbers."""
    out, fence = [], None
    for line in text.split("\n"):
        if fence:
            m = FENCE_CLOSE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append("")
        elif FENCE_OPEN_RE.match(line):
            fence = FENCE_OPEN_RE.match(line).group(1)
            out.append("")
        else:
            out.append(re.sub(r"`[^`\n]*`", "", line) if inline else line)
    return "\n".join(out)


def heading_slugs(text):
    """GitHub-style anchors: ATX headings (emphasis markers dropped) and `<a id|name>` tags. Setext headings are not read."""
    slugs, seen = set(), {}
    body = blank_code(text, inline=False)
    slugs.update(a.lower() for a in ANCHOR_TAG_RE.findall(body))
    for line in body.split("\n"):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))
        title = re.sub(r"[`*]", "", title)
        title = re.sub(r"(?<![A-Za-z0-9])_+(\S.*?)_+(?![A-Za-z0-9])", r"\1", title)  # _emphasis_, not snake_case
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        slugs.add(slug if n == 0 else "%s-%d" % (slug, n))
    return slugs


# ---------- rule: version ----------
def version_cfg(root, report):
    cfg = load_json(root, "tools/versions.json", report, "version")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("files"), list) or not cfg["files"]:
        if cfg is not None:
            report.error("version", "tools/versions.json needs a non-empty \"files\" list")
        return None
    return cfg


def find_version(text, pattern):
    """Return (version, span) when `pattern` has exactly one match, else None."""
    ms = list(re.finditer(pattern, text, re.M))
    if len(ms) != 1 or not ms[0].groups():
        return None
    return ms[0].group(1), ms[0].span(1)


def read_versions(root, cfg, report):
    found = {}
    for entry in cfg["files"]:
        rel = entry.get("file", "")
        try:
            with open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
                text = fh.read()
        except OSError:
            report.error("version", "%s is missing" % rel)
            continue
        hit = find_version(text, entry.get("pattern", ""))
        if hit is None:
            report.error("version", "%s: pattern must match exactly one version" % rel)
            continue
        if not SEMVER.match(hit[0]):
            report.error("version", "%s: %r is not X.Y.Z" % (rel, hit[0]))
            continue
        found[rel] = hit[0]
    return found


def check_versions(root, report):
    cfg = version_cfg(root, report)
    if cfg is None:
        return None, None
    found = read_versions(root, cfg, report)
    if len(set(found.values())) > 1:
        report.error("version", "files disagree: " + ", ".join("%s=%s" % kv for kv in sorted(found.items())))
        return cfg, None
    if len(found) != len(cfg["files"]):
        return cfg, None
    return cfg, next(iter(found.values()))


def check_base(root, report, cfg, current, base, run):
    """PR guard: guarded paths changed since the merge-base => version must increase."""
    rc, mb = run(["git", "merge-base", base, "HEAD"], root)
    mb = mb.strip()
    if rc != 0 or not mb:
        report.error("version-git", "cannot find a merge-base with %s; run: git fetch --unshallow (or git fetch origin)" % base)
        return
    rc, diff = run(["git", "diff", "--name-only", "--no-renames", mb], root)  # a rename must list its old path too
    if rc != 0:
        report.error("version-git", "git diff against merge-base %s failed: %s" % (mb[:7], diff.strip() or "no output"))
        return
    rc, untracked = run(["git", "ls-files", "--others", "--exclude-standard"], root)
    if rc != 0:
        report.error("version-git", "git ls-files failed: %s" % (untracked.strip() or "no output"))
        return
    changed = {p for p in (diff + "\n" + untracked).split("\n") if p}
    guards = tuple(cfg.get("guarded_paths") or [])
    touched = sorted(p for p in changed if p.startswith(guards))
    if not touched:
        report.note("no files under %s changed since %s; no version bump needed" % (", ".join(guards), base))
        return
    first = cfg["files"][0]
    rc, old_text = run(["git", "show", "%s:%s" % (mb, first["file"])], root)
    hit = find_version(old_text, first["pattern"]) if rc == 0 else None
    if hit is None:
        report.note("%s has no version at merge-base %s; skipping bump check" % (first["file"], mb[:7]), brief=True)
        return
    old = hit[0]
    if not SEMVER.match(old):
        report.note("%s has a non-semver version %r at merge-base; skipping bump check" % (first["file"], old), brief=True)
        return
    if current is None:
        return  # sync error already reported
    if semver_tuple(current) <= semver_tuple(old):
        report.error(
            "version",
            "%d file(s) under %s changed since %s (e.g. %s) but version is %s (was %s); run: python3 tools/repo.py bump <X.Y.Z>"
            % (len(touched), ", ".join(guards), base, touched[0], current, old),
        )


# ---------- rule: harness ----------
def table_first_cells(text):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^\|\s*Harness\s*\|", line):
            names = []
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                names.append(row.split("|")[1].strip())
            return names
    return None


def check_harnesses(root, report):
    names = load_json(root, "tools/harnesses.json", report, "harness")
    if names is None:
        return
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        report.error("harness", "tools/harnesses.json must be a non-empty list of names")
        return
    if names != sorted(names, key=str.casefold) or len(set(names)) != len(names):
        report.error("harness", "tools/harnesses.json must be unique and alphabetical: %s" % names)
    for rel, trailing in (("README.md", True), ("skills/agent-orchestration/references/harnesses.md", False)):
        if not os.path.isfile(os.path.join(root, rel)):
            report.error("harness", "%s is missing" % rel)
            continue
        rows = table_first_cells(read_text(root, rel))
        if rows is None:
            report.error("harness", "%s has no table whose header starts with `| Harness |`" % rel)
            continue
        if trailing:
            rows = [r for r in rows if not r.startswith("Others")]
        if rows != names:
            report.error("harness", "%s lists %s; tools/harnesses.json expects %s" % (rel, rows, names))


# ---------- rule: links, budget, leak, model ----------
def load_orch():
    """Import orch.py (its YAML loader reads catalog.yaml)."""
    path = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "scripts", "orch.py")
    spec = importlib.util.spec_from_file_location("orch_for_repo_tools", path)
    orch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orch)
    return orch


def catalog_ids(root, report):
    """Known model ids and aliases (lowercase) from every skills/*/catalog.yaml."""
    ids = set()
    paths = sorted(glob.glob(os.path.join(root, "skills", "*", "catalog.yaml")))
    if not paths:
        report.error("model", "no skills/*/catalog.yaml found to resolve model ids against")
        return None
    try:
        orch = load_orch()
        for p in paths:
            for m in (orch.load_file(p) or {}).get("models") or []:
                for key in ("id", "aliases"):
                    val = m.get(key)
                    for v in (val if isinstance(val, list) else [val]):
                        if isinstance(v, str):
                            ids.add(v.lower())
    except Exception as exc:  # noqa: BLE001 - report any loader failure as a rule error
        report.error("model", "cannot read catalog.yaml (%s)" % exc)
        return None
    return ids


def check_links(root, report, md_files, texts):
    slug_cache = {}

    def slugs(rel):
        if rel not in slug_cache:
            try:
                slug_cache[rel] = heading_slugs(texts[rel] if rel in texts else read_text(root, rel))
            except OSError:
                slug_cache[rel] = set()
        return slug_cache[rel]

    for rel in md_files:
        stripped = blank_code(texts[rel])
        for lineno, line in enumerate(stripped.split("\n"), 1):
            targets = INLINE_LINK_RE.findall(line)
            m = REF_LINK_RE.match(line)
            if m:
                targets.append(m.group(1))
            for target in targets:
                if SCHEME_RE.match(target) or target.startswith("//"):
                    continue
                path, _, anchor = target.partition("#")
                if path:
                    rel_path = unquote(path)
                    base = root if rel_path.startswith("/") else os.path.dirname(os.path.join(root, rel))
                    dest = os.path.normpath(os.path.join(base, rel_path.lstrip("/")))
                    if not os.path.exists(dest):
                        report.error("links", "%s:%d: %s does not exist" % (rel, lineno, target))
                        continue
                    dest_rel = os.path.relpath(dest, root).replace(os.sep, "/")
                else:
                    dest, dest_rel = None, rel
                if anchor and (dest is None or (os.path.isfile(dest) and dest.endswith(".md"))):
                    if unquote(anchor).lower() not in slugs(dest_rel):
                        report.error("links", "%s:%d: anchor #%s not found in %s" % (rel, lineno, anchor, dest_rel))


def check_budgets(root, report, texts):
    skills = sorted(glob.glob(os.path.join(root, "skills", "*", "SKILL.md")))
    if not skills:
        report.error("skills", "no skills/*/SKILL.md found; refusing to pass an empty skill set")
    for path in skills:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            body = split_frontmatter(fh.read())[1]
        words = len(body.split())
        report.note("%s body: %d/%d words" % (rel, words, SKILL_WORD_BUDGET), brief=True)
        if words > SKILL_WORD_BUDGET:
            report.error("budget", "%s body has %d words; budget is %d" % (rel, words, SKILL_WORD_BUDGET))
    if "AGENTS.md" in texts:
        n = len(texts["AGENTS.md"].splitlines())
        if n > AGENTS_MAX_LINES:
            report.error("budget", "AGENTS.md has %d lines; limit is %d" % (n, AGENTS_MAX_LINES))


def check_leaks(report, md_files, texts):
    for rel in md_files:
        for lineno, line in enumerate(texts[rel].split("\n"), 1):
            m = LEAK_PATH_RE.search(line)
            if m:
                report.error("leak", "%s:%d: absolute home path %r" % (rel, lineno, m.group(0)))
            for local, domain in EMAIL_RE.findall(line):
                d = domain.lower()
                if local.lower() in EMAIL_OK_LOCALS or d.startswith("example.") or d.endswith(".example") or d.endswith("noreply.github.com"):
                    continue
                report.error("leak", "%s:%d: email address %s@%s" % (rel, lineno, local, domain))


def check_models(root, report, md_files, texts):
    """Every model-id-looking token must be a catalog id or alias, or an exact allowlist token (see the module docstring)."""
    known = catalog_ids(root, report)
    if known is None:
        return
    allow = load_json(root, "tools/model-id-allowlist.json", report, "model", required=False) or []
    known |= {str(a).lower() for a in allow}
    for rel in md_files:
        reported = set()
        for lineno, line in enumerate(texts[rel].split("\n"), 1):
            for tok in MODEL_RE.findall(line):
                tok = tok.rstrip(".-")
                low = tok.lower()
                m = EFFORT_SUFFIX.match(low)  # `gemini-4-neon-high` = base id + effort suffix
                if low in known or (m and m.group(1) in known) or low in reported:
                    continue
                reported.add(low)
                report.error(
                    "model",
                    "%s:%d: %s is not a model id or alias in catalog.yaml (add it there, or to tools/model-id-allowlist.json)"
                    % (rel, lineno, tok),
                )


# ---------- rule: pinned ----------
def check_pinned(root, report):
    rules = load_json(root, "tools/pinned-rules.json", report, "pinned")
    if rules is None:
        return
    if not isinstance(rules, list):
        report.error("pinned", "tools/pinned-rules.json must be a list")
        return
    cache = {}
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r.get(k) for k in ("file", "phrase", "source")):
            report.error("pinned", "rule #%d needs non-empty file, phrase, source" % (i + 1))
            continue
        f = r["file"]
        if f not in cache:
            try:
                cache[f] = " ".join(read_text(root, f).split())
            except OSError:
                cache[f] = None
        if cache[f] is None:
            report.error("pinned", "%s (%s) is missing" % (f, r["source"]))
        elif " ".join(r["phrase"].split()) not in cache[f]:
            report.error("pinned", "%s lost a pinned rule from %s: %r" % (f, r["source"], r["phrase"][:70]))


# ---------- check ----------
def run_checks(root, base=None, run=sh):
    report = Report()
    md_files = list_md(root)
    texts = {}
    for rel in md_files:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            texts[rel] = fh.read()
    cfg, current = check_versions(root, report)
    if base and cfg is not None:
        check_base(root, report, cfg, current, base, run)
    check_harnesses(root, report)
    check_links(root, report, md_files, texts)
    check_budgets(root, report, texts)
    check_leaks(report, md_files, texts)
    check_models(root, report, md_files, texts)
    check_pinned(root, report)
    return report


def cmd_check(root, base, out=print, full=False):
    report = run_checks(root, base)
    for line in report.render_compact(full):
        out(line)
    out(report.closing())
    return 1 if report.errors else 0


# ---------- self-test ----------
FIX_SKILL = """---
name: agent-orchestration
description: Demo skill.
metadata:
  version: "1.0.0"
---

# Demo

See [the matrix](references/harnesses.md#harnesses) and [self](#demo).
Uses claude-sonnet-9. Pinned phrase stays here.
"""
FIX_TABLE = "## Harnesses\n\n| Harness | Notes |\n|---|---|\n| Alpha | a |\n| Beta | b |\n"
FIX_FILES = {
    "README.md": "# Readme\n\n| Harness | Path |\n|---|---|\n| Alpha | a |\n| Beta | b |\n| Others via `skills` | x |\n",
    "AGENTS.md": "# AGENTS.md\n\n- Be brief.\n",
    ".claude-plugin/plugin.json": '{\n  "name": "demo",\n  "version": "1.0.0"\n}\n',
    "skills/agent-orchestration/SKILL.md": FIX_SKILL,
    "skills/agent-orchestration/references/harnesses.md": "# Harnesses\n\n" + FIX_TABLE,
    "skills/agent-orchestration/catalog.yaml": "version: 1\nmodels:\n  - id: claude-sonnet-9\n    name: Sonnet\n    family: anthropic\n",
    "tools/harnesses.json": '["Alpha", "Beta"]\n',
    "tools/model-id-allowlist.json": "[]\n",
    "tools/versions.json": json.dumps({
        "files": [
            {"file": ".claude-plugin/plugin.json", "pattern": '"version":\\s*"([^"]+)"'},
            {"file": "skills/agent-orchestration/SKILL.md", "pattern": '^  version:\\s*"([^"]+)"\\s*$'},
        ],
        "guarded_paths": ["skills/agent-orchestration/", ".claude-plugin/"],
    }),
    "tools/pinned-rules.json": json.dumps([{"file": "skills/agent-orchestration/SKILL.md", "phrase": "Pinned phrase   stays here.", "source": "#0 v0"}]),
}


def write_fixture(root):
    for rel, text in FIX_FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def _edit(root, rel, old, new):
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    assert old in text, "fixture edit %r not found in %s" % (old, rel)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(old, new, 1))


def _append(root, rel, extra):
    with open(os.path.join(root, rel), "a", encoding="utf-8", newline="") as fh:
        fh.write(extra)


GIT_FIXTURE_CFG = [  # keep the fixture repo independent of the caller's git config and hooks
    "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
]


def _git_repo(root, init_args=()):
    for argv in (["git", "init", "-q"] + list(init_args), ["git", "add", "-A"], ["git"] + GIT_FIXTURE_CFG + ["commit", "-q", "-m", "base"]):
        rc, out = sh(argv, root)
        assert rc == 0, "git fixture setup failed: %s" % out


def self_test_cases():
    """(name, expected rule or None for 'must pass', mutation, base-ref)."""
    words = "word " * (SKILL_WORD_BUDGET + 1)
    return [
        ("baseline fixture passes", None, lambda r: None, None),
        ("version files disagree", "version", lambda r: _edit(r, ".claude-plugin/plugin.json", "1.0.0", "1.0.1"), None),
        ("--base without git history", "version-git", lambda r: None, "origin/main"),
        ("version not X.Y.Z", "version", lambda r: _edit(r, ".claude-plugin/plugin.json", "1.0.0", "v1"), None),
        ("shipped file changed, version same (--base)", "version",
         lambda r: (_git_repo(r), _append(r, "skills/agent-orchestration/SKILL.md", "More text.\n")), "HEAD"),
        ("shipped file changed, version bumped (--base)", None,
         lambda r: (_git_repo(r), _append(r, "skills/agent-orchestration/SKILL.md", "More text.\n"),
                    _edit(r, ".claude-plugin/plugin.json", "1.0.0", "1.0.1"),
                    _edit(r, "skills/agent-orchestration/SKILL.md", '"1.0.0"', '"1.0.1"')), "HEAD"),
        ("shipped file renamed away (--base)", "version",
         lambda r: (_git_repo(r), sh(["git", "mv", "skills/agent-orchestration/references/harnesses.md", "moved.md"], r)), "HEAD"),
        ("unshipped file changed (--base)", None, lambda r: (_git_repo(r), _append(r, "README.md", "\nMore.\n")), "HEAD"),
        ("harness registry unsorted", "harness", lambda r: _edit(r, "tools/harnesses.json", '["Alpha", "Beta"]', '["Beta", "Alpha"]'), None),
        ("README table drops a harness", "harness", lambda r: _edit(r, "README.md", "| Beta | b |\n", ""), None),
        ("README table out of order", "harness",
         lambda r: _edit(r, "README.md", "| Alpha | a |\n| Beta | b |", "| Beta | b |\n| Alpha | a |"), None),
        ("matrix has an extra harness", "harness",
         lambda r: _edit(r, "skills/agent-orchestration/references/harnesses.md", "| Beta | b |\n", "| Beta | b |\n| Gamma | g |\n"), None),
        ("broken relative link", "links", lambda r: _append(r, "README.md", "\n[x](docs/missing.md)\n"), None),
        ("broken anchor", "links", lambda r: _append(r, "README.md", "\n[x](skills/agent-orchestration/SKILL.md#nope)\n"), None),
        ("link inside code is ignored", None, lambda r: _append(r, "README.md", "\n`[x](docs/missing.md)`\n"), None),
        ("SKILL.md over word budget", "budget", lambda r: _append(r, "skills/agent-orchestration/SKILL.md", words), None),
        ("AGENTS.md over line limit", "budget", lambda r: _append(r, "AGENTS.md", "- line\n" * AGENTS_MAX_LINES), None),
        ("absolute home path", "leak", lambda r: _append(r, "README.md", "\nSee /home/alice/notes.\n"), None),
        ("macOS home path", "leak", lambda r: _append(r, "README.md", "\nSee /Users/alice/notes.\n"), None),
        ("email address", "leak", lambda r: _append(r, "README.md", "\nMail alice@corp.com.\n"), None),
        ("noreply and example emails pass", None,
         lambda r: _append(r, "README.md", "\nnoreply@anthropic.com bob@example.com\n"), None),
        ("unknown model id", "model", lambda r: _append(r, "README.md", "\nUse claude-opus-9-9.\n"), None),
        ("allowlisted model id passes", None,
         lambda r: (_append(r, "README.md", "\nUse claude-opus-9-9.\n"),
                    _edit(r, "tools/model-id-allowlist.json", "[]", '["claude-opus-9-9"]')), None),
        ("pinned phrase removed", "pinned", lambda r: _edit(r, "skills/agent-orchestration/SKILL.md", "Pinned phrase stays here.", "Gone."), None),
        ("pinned file missing", "pinned", lambda r: os.remove(os.path.join(r, "skills/agent-orchestration/SKILL.md")), None),
        ("no skills at all", "skills", lambda r: os.remove(os.path.join(r, "skills/agent-orchestration/SKILL.md")), None),
    ]


def cmd_self_test(out=print, full=False):
    failures, cases, failed = 0, 0, []
    for name, rule, mutate, base in self_test_cases():
        tmp = tempfile.mkdtemp(prefix="repo-selftest-")
        try:
            write_fixture(tmp)
            mutate(tmp)
            errors = run_checks(tmp, base).errors
            fired = sorted({e.split(":", 1)[0] for e in errors})
            if rule is None:
                good = not errors
                want = "no errors"
            else:
                good = rule in fired
                want = "error rule %r" % rule
            cases += 1
            if good:
                if full:
                    out("self-test ok: %s -> %s" % (name, "pass" if rule is None else "%s fired" % rule))
            else:
                failures += 1
                failed.append("error: self-test: %s expected %s, got %s" % (name, want, errors or "no errors"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for line in capped(failed, full):
        out(line)
    if failures:
        out("error: %d problem(s); action: fix the rule or its fixture in tools/repo.py, then rerun check --self-test" % failures)
        return 1
    out("ok: self-test: %d cases, every rule fires" % cases)
    return 0


# ---------- bump ----------
def cmd_bump(root, new, out=print, full=False):
    if not SEMVER.match(new):
        out("error: bump: %r is not X.Y.Z" % new)
        out("error: 1 problem(s); action: pass a version like 1.2.3")
        return 1
    report = Report()
    cfg, current = check_versions(root, report)
    if current is None:
        for line in report.render_compact(full):
            out(line)
        out("error: bump: versions must agree before bumping")
        out("error: %d problem(s); action: fix the version files listed above, then rerun bump" % (len(report.errors) + 1))
        return 1
    if semver_tuple(new) <= semver_tuple(current):
        out("error: bump: %s must be higher than the current %s" % (new, current))
        out("error: 1 problem(s); action: pass a higher X.Y.Z")
        return 1
    # Plan every write before touching any file.
    plans = []
    for entry in cfg["files"]:
        path = os.path.join(root, entry["file"])
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        start, end = find_version(text, entry["pattern"])[1]
        plans.append((path, entry["file"], text[:start] + new + text[end:]))
    for path, rel, text in plans:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if full:
            out("bumped %s: %s -> %s" % (rel, current, new))
    out("ok: bumped %d file(s) %s -> %s" % (len(plans), current, new))
    return 0


# ---------- release ----------
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def peeled_sha(ls_remote_out):
    """Commit sha a `git ls-remote --tags` listing points at (the peeled `^{}` line wins for annotated tags)."""
    sha = None
    for line in ls_remote_out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            if parts[1].endswith("^{}"):
                return parts[0]
            sha = parts[0]
    return sha


def release_state(root, run=sh, check_fn=run_checks):
    """Verify release preconditions and work out what is left to do.

    Returns a dict: version, repo (owner/name), tag, steps (argv lists still to run), notes, problems.
    A rerun after a partial publish resumes: a tag already at HEAD (local and/or origin) skips its steps.
    `run` and `check_fn` are injectable so tests need no git or network.
    """
    problems, notes = [], []
    state = {"version": None, "repo": None, "tag": None, "steps": [], "notes": notes, "problems": problems}
    report = Report()
    _, version = check_versions(root, report)
    state["version"] = version
    if version is None:
        problems.append("versions are not in sync: " + "; ".join(report.errors or ["unreadable"]))
    rc, out = run(["git", "status", "--porcelain"], root)
    if rc != 0:
        problems.append("git status failed")
    elif out.strip():
        problems.append("working tree is not clean")
    rc, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if rc != 0 or out.strip() != "main":
        problems.append("not on main (on %s)" % (out.strip() or "unknown"))
    rc, _ = run(["git", "fetch", "--quiet", "origin", "main"], root)
    if rc != 0:
        problems.append("git fetch origin main failed")
    rc_h, head = run(["git", "rev-parse", "HEAD"], root)
    rc_o, origin = run(["git", "rev-parse", "origin/main"], root)
    head = head.strip()
    if rc_h != 0 or rc_o != 0 or head != origin.strip():
        problems.append("HEAD is not origin/main (push or pull first)")
    checked = check_fn(root)
    if checked.errors:
        problems.append("check has %d error(s); run: python3 tools/repo.py check" % len(checked.errors))
    rc, url = run(["git", "remote", "get-url", "origin"], root)
    m = GITHUB_REMOTE_RE.search(url.strip()) if rc == 0 else None
    if m:
        state["repo"] = "%s/%s" % m.groups()
    else:
        problems.append("cannot derive owner/repo from the origin URL (gh needs --repo)")
    if not version:
        return state
    tag = state["tag"] = "v" + version
    rc, local = run(["git", "rev-parse", "--verify", "--quiet", "refs/tags/%s^{commit}" % tag], root)
    local = local.strip() if rc == 0 else None
    rc, listing = run(["git", "ls-remote", "--tags", "origin", "refs/tags/" + tag, "refs/tags/%s^{}" % tag], root)
    if rc != 0:
        problems.append("cannot query origin tags")
        return state
    remote = peeled_sha(listing)
    if local and local != head:
        problems.append("tag %s exists locally at %s, not at HEAD %s" % (tag, local[:7], head[:7]))
    if remote and remote != head:
        problems.append("tag %s exists on origin at %s, not at HEAD %s" % (tag, remote[:7], head[:7]))
    if remote and remote == head and state["repo"]:
        rc, text = run(["gh", "release", "view", tag, "--repo", state["repo"]], root)
        if rc == 0:
            problems.append("release %s already exists" % tag)
        elif "not found" not in text.lower():
            problems.append("cannot query GitHub releases: %s" % (text.strip() or "no output"))
    if problems:
        return state
    if local:
        notes.append("tag %s already exists locally at HEAD; not recreating it" % tag)
    else:
        state["steps"].append(["git", "tag", "-a", tag, "-m", "Release " + tag])
    if remote:
        notes.append("tag %s is already on origin at HEAD and has no release yet; resuming at gh" % tag)
    else:
        state["steps"].append(["git", "push", "origin", "refs/tags/" + tag])
    state["steps"].append(["gh", "release", "create", tag, "--repo", state["repo"], "--generate-notes", "--verify-tag"])
    return state


def cmd_release(root, publish, run=sh, check_fn=run_checks, out=print, full=False):
    state = release_state(root, run, check_fn)
    for line in capped(["error: release: " + p for p in state["problems"]], full):
        out(line)
    if state["problems"]:
        out("error: %d problem(s); action: fix the problems above, then rerun python3 tools/repo.py release" % len(state["problems"]))
        return 1
    for n in state["notes"]:
        out("note: " + n)
    version, steps = state["version"], state["steps"]
    out("%s release v%s:" % ("publishing" if publish else "dry run (pass --publish to run)", version))
    for argv in steps:  # at most three commands: the approval payload, so shown in full
        out("  " + " ".join(argv))
    if not publish:
        out("ok: dry run v%s (%d step(s)); action: show the steps above to the user, then rerun with --publish after approval" % (version, len(steps)))
        return 0
    for argv in steps:
        rc, text = run(argv, root)
        if rc != 0:
            out("error: release: %s failed: %s" % (" ".join(argv), text.strip()))
            if argv[0] == "gh":
                out("recover: tag %s is on origin; finish with: %s" % (state["tag"], " ".join(argv)))
            out("recover: or fix the cause and rerun `python3 tools/repo.py release --publish` (finished steps are skipped)")
            out("error: 1 problem(s); action: follow the recover: line above")
            return 1
    out("ok: released v%s" % version)
    return 0


# ---------- main ----------
def build_parser():
    exits = "exit codes: 0 ok; 1 any `error:` line (the last line says what to do)"
    ap = argparse.ArgumentParser(
        prog="repo.py",
        description="Maintainer tooling for this repository.",
        epilog=exits,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_text, example):
        p = sub.add_parser(
            name, help=help_text, description=help_text, epilog="example: python3 tools/repo.py " + example,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("--root", default=REPO_ROOT, help="repository root (default: this repository)")
        p.add_argument("--full", action="store_true", help="print every line and remove the list caps")
        return p

    c = add("check", "run repo-wide rules", "check --base origin/main")
    c.add_argument("--base", help="git ref (e.g. origin/main): shipped changes since the merge-base need a version bump")
    c.add_argument("--self-test", action="store_true", help="assert every rule fires on a broken fixture tree")
    b = add("bump", "write a higher X.Y.Z to every version file", "bump 1.2.0")
    b.add_argument("version")
    r = add("release", "verify preconditions, then tag and publish a release", "release  # dry run; add --publish after approval")
    r.add_argument("--publish", action="store_true", help="run the tag, push and gh release commands (default: dry run)")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "check":
        return cmd_self_test(full=a.full) if a.self_test else cmd_check(os.path.abspath(a.root), a.base, full=a.full)
    if a.cmd == "bump":
        return cmd_bump(os.path.abspath(a.root), a.version, full=a.full)
    return cmd_release(os.path.abspath(a.root), a.publish, full=a.full)


if __name__ == "__main__":
    sys.exit(main())
