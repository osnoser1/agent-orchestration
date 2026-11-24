#!/usr/bin/env python3
"""Maintainer tooling for this repository (not shipped with the skill).

Stdlib only, Python 3.9+; run it as `python3 -I tools/repo.py <command>`.

Commands:
  check [--base REF] [--root DIR]   repo-wide rules; exit 0 ok, 1 on any `error:` line
  check --self-test                 build broken fixture trees and assert every rule fires
  bump X.Y.Z | --next PART          write a new, higher version to every file in tools/versions.json
  golden-diff [--base REF]          per-group count and decision changes of tests/fixtures/golden vs REF
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
(git could not answer for --base), harness, links, pointers, budget, leak, model, pinned, skills, cli, done-when.

Rule `pointers`: in skills/**/*.md, README.md and AGENTS.md, a section pointer ``(`references/x.md`, **Heading**``
(any `, step N` tail is ignored) must name a heading of that file, ignoring case, backticks, emphasis and a trailing
` (…)` or `: …` qualifier on the heading; `references/x.md#anchor` outside a markdown link must name a GitHub slug in
it. `references/` resolves in the citing skill, or in any skills/*/ for README.md and AGENTS.md. Same-file pointers
(`**Heading**, above`) are not checked.

Rule `cli` (docs <-> CLI): imports `build_parser()` from skills/agent-orchestration/scripts/orch.py and
tools/repo.py under the checked root, then reads every checked markdown file (the `links` set, README.md and
AGENTS.md included). Only code is scanned: inline code spans and fenced lines (a trailing `\\` joins the next
line; ` # comment` tails are dropped). Each `orch.py <sub>` / `repo.py <sub>` mention must name a real subcommand,
and the `--flags` after it, up to the next mention in the same span, must belong to that subcommand (or be --help).
Every subcommand of either script must also be mentioned in README.md.

Rule `done-when`: every `## N.` step of skills/*/SKILL.md, and every `##` section of skills/*/references/*.md that
holds an ordered list (two or more items starting at column 0), needs a line starting `*Done when*` before the next
`##` heading. Lookup lists (rankings, fallback orders) are exempt through tools/done-when-exempt.json, which maps a
file to its exempt section titles; an entry naming a missing section is an error.
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
    "cli": "fix the doc to match `--help`, or add the missing subcommand mention to README.md",
    "pointers": "point at an existing heading of that file (or fix the heading the pointer names)",
    "done-when": "add a `*Done when*: <checkable bound>` line to the section, or list a lookup section in tools/done-when-exempt.json",
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
CLI_SCRIPTS = (("orch", "skills/agent-orchestration/scripts/orch.py"), ("repo", "tools/repo.py"))
CLI_MENTION_RE = re.compile(r"\b(orch|repo)\.py[ \t]+([a-z][a-z-]*)\b")
CLI_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
DONE_WHEN_RE = re.compile(r"^\*Done when\*", re.M)
ORDERED_ITEM_RE = re.compile(r"^\d+[.)][ \t]", re.M)
STEP_HEADING_RE = re.compile(r"^##\s+\d+\.")
POINTER_PATH = r"((?:skills/[\w.-]+/)?references/[\w.-]+\.md)"
POINTER_BOLD_RE = re.compile(r"\(`?" + POINTER_PATH + r"`?,\s*\*\*([^*]+?)\*\*")  # (`references/x.md`, **Heading**
POINTER_ANCHOR_RE = re.compile(r"(?<![\w/(.\]<-])" + POINTER_PATH + r"#([\w-]+)")  # not a [link](target): `links` owns those


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
        return
    rc, tip_text = run(["git", "show", "%s:%s" % (base, first["file"])], root)
    tip_hit = find_version(tip_text, first["pattern"]) if rc == 0 else None
    if tip_hit is not None and SEMVER.match(tip_hit[0]):
        tip = tip_hit[0]
        if semver_tuple(current) <= semver_tuple(tip):
            report.error(
                "version",
                "version %s is not above %s on %s; rebase on %s, then run: python3 tools/repo.py bump --next <level>"
                % (current, tip, base, base),
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


def heading_titles(text):
    """Normalised ATX heading texts (code fences skipped): backticks, emphasis and link syntax dropped, lowercase."""
    titles = set()
    for line in blank_code(text, inline=False).split("\n"):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            titles.add(norm_title(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))))
    return titles


def norm_title(s):
    s = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", s)  # _emphasis_ like **strong**
    return " ".join(re.sub(r"[`*]", "", s).split()).lower()


def pointer_target(root, rel, target):
    """Repo-relative path a pointer names: a full skills/ path as is, else `references/…` in the citing skill (or any skill)."""
    if target.startswith("skills/"):
        return target if os.path.isfile(os.path.join(root, target)) else None
    m = re.match(r"^(skills/[^/]+)/", rel)
    skill_dirs = [m.group(1)] if m else sorted(
        os.path.relpath(p, root).replace(os.sep, "/") for p in glob.glob(os.path.join(root, "skills", "*")))
    for d in skill_dirs:
        if os.path.isfile(os.path.join(root, d, target)):
            return d + "/" + target
    return None


def check_pointers(root, report, md_files, texts):
    """Section pointers (`references/x.md`, **Heading**) and references/x.md#anchor must name a real heading."""
    cache = {}
    for rel in md_files:
        if not (rel.startswith("skills/") or rel in ("README.md", "AGENTS.md")):
            continue
        body = blank_code(texts[rel], inline=False)
        hits = [(m.start(), m.group(1), "**%s**" % m.group(2), norm_title(m.group(2)), None) for m in POINTER_BOLD_RE.finditer(body)]
        hits += [(m.start(), m.group(1), "#" + m.group(2), None, m.group(2).lower()) for m in POINTER_ANCHOR_RE.finditer(body)]
        for start, target, shown, title, anchor in sorted(hits, key=lambda h: h[0]):
            lineno = body.count("\n", 0, start) + 1
            dest = pointer_target(root, rel, target)
            if dest is None:
                report.error("pointers", "%s:%d: pointer to %s %s: no such file" % (rel, lineno, target, shown))
                continue
            if dest not in cache:
                text = texts[dest] if dest in texts else read_text(root, dest)
                cache[dest] = (heading_titles(text), heading_slugs(text))
            titles = cache[dest][0]
            named = title is None or any(t == title or t.startswith((title + " (", title + ":")) for t in titles)
            if not named or (anchor is not None and anchor not in cache[dest][1]):
                report.error("pointers", "%s:%d: pointer to %s %s has no such heading" % (rel, lineno, target, shown))


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


# ---------- rule: cli ----------
def load_cli_parsers(root, report):
    """{name: {subcommand: set of option strings}} from each script's build_parser() under `root`."""
    parsers = {}
    for name, rel in CLI_SCRIPTS:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            report.error("cli", "%s is missing" % rel)
            continue
        try:
            spec = importlib.util.spec_from_file_location("cli_rule_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            choices = next(a for a in mod.build_parser()._actions if a.dest == "cmd").choices
            parsers[name] = {sub: set(p._option_string_actions) | {"-h", "--help"} for sub, p in choices.items()}
        except Exception as exc:  # noqa: BLE001 - any loader failure is a rule error
            report.error("cli", "cannot read the parser of %s (%s)" % (rel, exc))
    return parsers


def code_chunks(text):
    """(lineno, code) for inline code spans and fenced lines; a trailing backslash joins the next fenced line."""
    out, fence, pending = [], None, None
    for lineno, line in enumerate(text.split("\n"), 1):
        if fence:
            m = FENCE_CLOSE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
                if pending:  # an unfinished `\` continuation ends with its fence
                    out.append((pending[0], pending[1]))
                    pending = None
                continue
            code = line.split(" #", 1)[0].rstrip()
            if pending:
                code, lineno = pending[1] + " " + code, pending[0]
                pending = None
            if code.endswith("\\"):
                pending = (lineno, code[:-1])
            else:
                out.append((lineno, code))
        elif FENCE_OPEN_RE.match(line):
            fence = FENCE_OPEN_RE.match(line).group(1)
        else:
            out += [(lineno, m) for m in INLINE_CODE_RE.findall(line)]
    return out


def cli_mentions(text):
    """(lineno, script, subcommand, [flags]) for each `orch.py <sub>` / `repo.py <sub>` mention in code."""
    found = []
    for lineno, code in code_chunks(text):
        hits = list(CLI_MENTION_RE.finditer(code))
        for i, m in enumerate(hits):
            tail = code[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(code)]
            tail = re.split(r"&&|\|\||[|;]", tail)[0]  # flags after a shell operator belong to another command
            found.append((lineno, m.group(1), m.group(2), CLI_FLAG_RE.findall(tail)))
    return found


def check_cli(root, report, md_files, texts):
    parsers = load_cli_parsers(root, report)
    mentioned = set()
    for rel in md_files:
        for lineno, script, sub, flags in cli_mentions(texts[rel]):
            subs = parsers.get(script)
            if subs is None:
                continue
            if rel == "README.md":
                mentioned.add((script, sub))
            if sub not in subs:
                report.error("cli", "%s:%d: %s.py has no subcommand %r (has: %s)" % (rel, lineno, script, sub, ", ".join(sorted(subs))))
                continue
            for flag in dict.fromkeys(flags):
                if flag not in subs[sub]:
                    report.error("cli", "%s:%d: %s.py %s has no flag %s" % (rel, lineno, script, sub, flag))
    for script, subs in sorted(parsers.items()):
        for sub in sorted(subs):
            if (script, sub) not in mentioned:
                report.error("cli", "README.md never mentions `%s.py %s`" % (script, sub))


# ---------- rule: done-when ----------
def md_sections(text):
    """[(heading line, lineno, body)] for each `##` section, skipping headings inside code fences."""
    sections, cur = [], None
    for lineno, line in enumerate(blank_code(text, inline=False).split("\n"), 1):
        if re.match(r"^##\s", line):
            cur = [line.strip(), lineno, []]
            sections.append(cur)
        elif cur is not None:
            cur[2].append(line)
    return [(h, n, "\n".join(b)) for h, n, b in sections]


def check_done_when(root, report, texts):
    exempt = load_json(root, "tools/done-when-exempt.json", report, "done-when", required=False) or {}
    if not isinstance(exempt, dict) or not all(isinstance(v, list) for v in exempt.values()):
        report.error("done-when", "tools/done-when-exempt.json must map a file to a list of section titles")
        exempt = {}
    used = set()
    for rel in sorted(texts):
        is_skill = re.match(r"^skills/[^/]+/SKILL\.md$", rel)
        if not is_skill and not re.match(r"^skills/[^/]+/references/[^/]+\.md$", rel):
            continue
        for heading, lineno, body in md_sections(split_frontmatter(texts[rel])[1] if is_skill else texts[rel]):
            title = heading.lstrip("#").strip()
            if is_skill:
                needs = bool(STEP_HEADING_RE.match(heading))
            else:
                needs = len(ORDERED_ITEM_RE.findall(body)) >= 2
            if title in (exempt.get(rel) or []):
                used.add((rel, title))
                continue
            if needs and not DONE_WHEN_RE.search(body):
                report.error("done-when", "%s: section %r has no `*Done when*` line" % (rel, title))
    for rel, titles in sorted(exempt.items()):
        for title in titles:
            if (rel, title) not in used:
                report.error("done-when", "tools/done-when-exempt.json: %s has no section %r" % (rel, title))


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
    check_pointers(root, report, md_files, texts)
    check_budgets(root, report, texts)
    check_leaks(report, md_files, texts)
    check_models(root, report, md_files, texts)
    check_pinned(root, report)
    check_cli(root, report, md_files, texts)
    check_done_when(root, report, texts)
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
FIX_ORCH = """import argparse


def build_parser():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "select"):
        sub.add_parser(name).add_argument("--full")
    return ap
"""
FIX_REPO = FIX_ORCH.replace('("check", "select")', '("check",)')
FIX_FILES = {
    "README.md": "# Readme\n\nRun `orch.py check`, `orch.py select --full` and `repo.py check`.\n\n| Harness | Path |\n|---|---|\n| Alpha | a |\n| Beta | b |\n| Others via `skills` | x |\n",
    "AGENTS.md": "# AGENTS.md\n\n- Be brief.\n",
    ".claude-plugin/plugin.json": '{\n  "name": "demo",\n  "version": "1.0.0"\n}\n',
    "skills/agent-orchestration/SKILL.md": FIX_SKILL,
    "skills/agent-orchestration/references/harnesses.md": "# Harnesses\n\n" + FIX_TABLE,
    "skills/agent-orchestration/catalog.yaml": "version: 1\nmodels:\n  - id: claude-sonnet-9\n    name: Sonnet\n    family: anthropic\n",
    "skills/agent-orchestration/scripts/orch.py": FIX_ORCH,
    "tools/repo.py": FIX_REPO,
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


def _write(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


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
        ("shipped file changed, version stale vs base tip (--base)", "version",
         lambda r: (_git_repo(r),
                    _edit(r, ".claude-plugin/plugin.json", "1.0.0", "1.0.1"),
                    _edit(r, "skills/agent-orchestration/SKILL.md", '"1.0.0"', '"1.0.1"'),
                    sh(["git", "commit", "-am", "main bump"], r),
                    sh(["git", "branch", "main-tip"], r),
                    sh(["git", "checkout", "-q", "HEAD~1"], r),
                    _append(r, "skills/agent-orchestration/SKILL.md", "More text.\n"),
                    _edit(r, ".claude-plugin/plugin.json", "1.0.0", "1.0.1"),
                    _edit(r, "skills/agent-orchestration/SKILL.md", '"1.0.0"', '"1.0.1"')), "main-tip"),
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
        ("dangling section pointer", "pointers",
         lambda r: _append(r, "README.md", "\n(`references/harnesses.md`, **No Such Section**)\n"), None),
        ("dangling anchor pointer", "pointers", lambda r: _append(r, "AGENTS.md", "\nSee references/harnesses.md#nope.\n"), None),
        ("section pointers that resolve pass", None,
         lambda r: _append(r, "skills/agent-orchestration/SKILL.md",
                           "\n(`references/harnesses.md`, **`harnesses`**, step 1) and `references/harnesses.md#harnesses`.\n"), None),
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
        ("doc names an unknown subcommand", "cli", lambda r: _append(r, "AGENTS.md", "\nRun `orch.py nope`.\n"), None),
        ("doc names an unknown flag", "cli", lambda r: _append(r, "AGENTS.md", "\nRun `orch.py check --bogus`.\n"), None),
        ("fenced command with an unknown flag", "cli",
         lambda r: _append(r, "AGENTS.md", "\n```bash\npython3 tools/repo.py check \\\n  --bogus\n```\n"), None),
        ("flags are checked per mention", None,
         lambda r: _append(r, "AGENTS.md", "\nRun `orch.py check --full`, then `orch.py select --full`; `--bogus` outside a mention is ignored.\n"), None),
        ("subcommand missing from README", "cli",
         lambda r: _edit(r, "README.md", ", `orch.py select --full`", ""), None),
        ("step without a Done when", "done-when",
         lambda r: _append(r, "skills/agent-orchestration/SKILL.md", "\n## 1. Step\nDo it.\n"), None),
        ("step with a Done when", None,
         lambda r: _append(r, "skills/agent-orchestration/SKILL.md", "\n## 1. Step\nDo it.\n\n*Done when*: done.\n"), None),
        ("reference procedure without a Done when", "done-when",
         lambda r: _append(r, "skills/agent-orchestration/references/harnesses.md", "\n## Steps\n1. One.\n2. Two.\n"), None),
        ("reference procedure with a Done when", None,
         lambda r: _append(r, "skills/agent-orchestration/references/harnesses.md", "\n## Steps\n1. One.\n2. Two.\n\n*Done when*: done.\n"), None),
        ("exempt lookup list passes", None,
         lambda r: (_append(r, "skills/agent-orchestration/references/harnesses.md", "\n## Ranking\n1. One.\n2. Two.\n"),
                    _write(r, "tools/done-when-exempt.json", '{"skills/agent-orchestration/references/harnesses.md": ["Ranking"]}')), None),
        ("stale done-when exemption", "done-when",
         lambda r: _write(r, "tools/done-when-exempt.json", '{"skills/agent-orchestration/references/harnesses.md": ["Gone"]}'), None),
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
def next_version(root, part, run=sh):
    """(X.Y.Z, source) one `part` above the higher of the version files and the latest local vX.Y.Z tag, or (None, error)."""
    report = Report()
    _, current = check_versions(root, report)
    if current is None:
        return None, "versions must agree before bumping: " + "; ".join(report.errors or ["unreadable"])
    rc, tags = run(["git", "tag", "--list", "v*"], root)
    if rc != 0:
        return None, "cannot list git tags (%s); pass X.Y.Z instead" % (tags.strip() or "git failed")
    found = [t.strip()[1:] for t in tags.split("\n") if SEMVER.match(t.strip()[1:])]
    top = max([current] + found, key=semver_tuple)
    major, minor, patch = semver_tuple(top)
    bumped = {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}[part]
    return "%d.%d.%d" % bumped, ("tag v" + top if top != current else "version files") + " at " + top


def cmd_bump(root, new, out=print, full=False, part=None, run=sh):
    if (new is None) == (part is None):
        out("error: bump: pass either X.Y.Z or --next patch|minor|major, not both or neither")
        out("error: 1 problem(s); action: rerun bump with exactly one of them")
        return 1
    if part:
        new, source = next_version(root, part, run)
        if new is None:
            out("error: bump: " + source)
            out("error: 1 problem(s); action: fix the cause above, then rerun bump --next %s" % part)
            return 1
        if full:
            out("note: --next %s from %s" % (part, source))
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


# ---------- golden-diff ----------
GOLDEN_DIR = "tests/fixtures/golden"


def golden_cases(text):
    """{`=== <argv>` header: body}, as tests/test_golden.py `parse` splits a snapshot file."""
    cases, head = {}, None
    for line in (text or "").splitlines(keepends=True):
        if line.startswith("=== "):
            head = line.rstrip("\n")
            cases[head] = ""
        elif head is not None:
            cases[head] += line
    return cases


def golden_decision(body):
    """(rc, decision): the `rc:` value and the last `action:` line without its ` (…)` asides."""
    rc = re.search(r"^rc: (-?\d+)", body, re.M)
    acts = re.findall(r"^action: (.*)$", body, re.M)
    return (rc.group(1) if rc else "?"), (re.sub(r"\s*\([^()]*\)", "", acts[-1]).strip() if acts else "(none)")


def decision_change(old, new):
    """`old -> new`; `;`-segments both share at either end print once, around a bracketed `[old -> new]` middle."""
    a, b = old.split("; "), new.split("; ")
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    q = 0
    while q < min(len(a), len(b)) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    mid = "%s -> %s" % ("; ".join(a[p:len(a) - q]) or "(none)", "; ".join(b[p:len(b) - q]) or "(none)")
    return "; ".join(a[:p] + ["[%s]" % mid if p or q else mid] + (a[len(a) - q:] if q else []))


def golden_snapshots(root, base, run=sh):
    """({group: text} at the working tree, {group: text} at `base`, error or None)."""
    new = {}
    for path in sorted(glob.glob(os.path.join(root, GOLDEN_DIR, "*.txt"))):
        with open(path, encoding="utf-8") as fh:
            new[os.path.basename(path)[:-4]] = fh.read()
    rc, listing = run(["git", "ls-tree", "--name-only", base, GOLDEN_DIR + "/"], root)
    if rc != 0:
        return new, {}, "cannot read %s at %s: %s" % (GOLDEN_DIR, base, listing.strip() or "git failed")
    old = {}
    for rel in listing.split("\n"):
        if rel.endswith(".txt"):
            rc, text = run(["git", "show", "%s:%s" % (base, rel)], root)
            if rc != 0:
                return new, old, "git show %s:%s failed: %s" % (base, rel, text.strip())
            old[rel.rsplit("/", 1)[-1][:-4]] = text
    return new, old, None


def golden_diff_lines(new, old, full=False):
    """(lines, cases touched, groups touched): per group a count line, then its decision changes, most frequent first."""
    lines, total, groups = [], 0, 0
    for group in sorted(set(new) | set(old)):
        a, b = golden_cases(old.get(group)), golden_cases(new.get(group))
        changed = [h for h in b if h in a and a[h] != b[h]]
        added, removed = [h for h in b if h not in a], [h for h in a if h not in b]
        if not (changed or added or removed):
            continue
        groups += 1
        total += len(changed) + len(added) + len(removed)
        lines.append("%s: %d changed case(s), %d added, %d removed" % (group, len(changed), len(added), len(removed)))
        counts, same = {}, 0
        for h in changed:
            (rc0, d0), (rc1, d1) = golden_decision(a[h]), golden_decision(b[h])
            same += (rc0, d0) == (rc1, d1)
            for key in ([decision_change(d0, d1)] if d0 != d1 else []) + (["rc %s -> rc %s" % (rc0, rc1)] if rc0 != rc1 else []):
                counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines += capped([golden_line(group, k, n, full) for k, n in ranked], full)
        if same:
            lines.append("%s: same rc and action, other output changed: %d" % (group, same))
    return lines, total, groups


def golden_line(group, key, n, full, limit=350):
    """`<group>: <decision change>: <n>`, shortened in the middle when compact so it stays within the output contract."""
    line = "%s: %s: %d" % (group, key, n)
    if full or len(line) <= limit:
        return line
    room = limit - len("%s: : %d" % (group, n)) - 1
    return "%s: %s…%s: %d" % (group, key[: room // 2], key[len(key) - (room - room // 2):], n)


def cmd_golden_diff(root, base, run=sh, out=print, full=False):
    new, old, err = golden_snapshots(root, base, run)
    if err:
        out("error: golden-diff: " + err)
        out("error: 1 problem(s); action: run git fetch origin, or pass --base <ref> that has %s" % GOLDEN_DIR)
        return 1
    lines, total, groups = golden_diff_lines(new, old, full)
    for line in lines:
        out(line)
    out("ok: %d case(s) changed across %d group(s)" % (total, groups) if total else "ok: no golden changes")
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
    # Any clean worktree works: HEAD == origin/main pins the commit, not the branch name. The explicit refspec
    # updates refs/remotes/origin/main even when remote.origin.fetch does not cover main.
    rc, _ = run(["git", "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"], root)
    if rc != 0:
        problems.append("git fetch origin main failed")
    rc_h, head = run(["git", "rev-parse", "HEAD"], root)
    rc_o, origin = run(["git", "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main^{commit}"], root)
    head = head.strip()
    if rc_h != 0 or rc_o != 0 or head != origin.strip():
        problems.append("HEAD is not origin/main (push or pull first)")
    else:
        rc, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
        if rc == 0 and out.strip() != "main":
            notes.append("releasing from %s at origin/main" % (out.strip() if out.strip() not in ("", "HEAD") else "detached HEAD"))
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
        state["steps"].append(["git", "tag", "-a", tag, "-m", "Release " + tag, head])  # tag the checked commit
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
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=154),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_text, example):
        p = sub.add_parser(
            name, help=help_text, description=help_text, epilog="example: python3 tools/repo.py " + example,
            formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, width=154),
        )
        p.add_argument("--root", default=REPO_ROOT, help="repository root (default: this repository)")
        p.add_argument("--full", action="store_true", help="print every line and remove the list caps")
        return p

    c = add("check", "run repo-wide rules", "check --base origin/main")
    c.add_argument("--base", help="git ref (e.g. origin/main): shipped changes since the merge-base need a version bump")
    c.add_argument("--self-test", action="store_true", help="assert every rule fires on a broken fixture tree")
    b = add("bump", "write a higher X.Y.Z to every version file", "bump --next patch  # or: bump 1.2.0")
    b.add_argument("version", nargs="?", help="explicit X.Y.Z (or use --next)")
    b.add_argument("--next", choices=("patch", "minor", "major"),
                   help="one step above the higher of the version files and the latest local vX.Y.Z tag")
    g = add("golden-diff", "summarise golden snapshot changes against a git ref", "golden-diff --base v0.20.0")
    g.add_argument("--base", default="origin/main", help="git ref to compare tests/fixtures/golden against (default: origin/main)")
    r = add("release", "verify preconditions, then tag and publish a release", "release  # dry run; add --publish after approval")
    r.add_argument("--publish", action="store_true", help="run the tag, push and gh release commands (default: dry run)")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "check":
        return cmd_self_test(full=a.full) if a.self_test else cmd_check(os.path.abspath(a.root), a.base, full=a.full)
    if a.cmd == "bump":
        return cmd_bump(os.path.abspath(a.root), a.version, full=a.full, part=a.next)
    if a.cmd == "golden-diff":
        return cmd_golden_diff(os.path.abspath(a.root), a.base, full=a.full)
    return cmd_release(os.path.abspath(a.root), a.publish, full=a.full)


if __name__ == "__main__":
    sys.exit(main())
