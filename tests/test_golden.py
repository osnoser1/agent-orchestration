"""Golden-output snapshots of orch.py and the escalate never-step-down property.

Snapshots: tests/fixtures/golden/<group>.txt, one `=== <argv>` case per run (compact, then `--full`), with the exit code,
stdout+stderr, and the follow-ups `select` left in the assets copy or the payload `launch` wrote. Inputs live in
tests/fixtures/golden/input and are copied to a fresh temp dir per run (HOME points there too), so ~/.config is never
read; paths print as <TMP>/<REPO>.
Regenerate only when a change intends to alter output: GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden
"""
import concurrent.futures
import contextlib
import difflib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "scripts")
ORCH = os.path.join(SCRIPTS, "orch.py")
CATALOG = os.path.join(REPO_ROOT, "skills", "agent-orchestration", "catalog.yaml")
GOLDEN = os.path.join(HERE, "fixtures", "golden")
INPUT = os.path.join(GOLDEN, "input")
NOW = "2026-11-20T21:00:00Z"
DIFF_LINES = 40

sys.path.insert(0, SCRIPTS)
import orch  # noqa: E402

AVAIL = {  # --available sets: healthy, low/instance ids, emergency/coordinator, tier-meeting models exhausted
    "main": "claudeAgent:claude-opus-5-5,claudeAgent:claude-sonnet-5-5,codex:gpt-6.2-sol,codex:gpt-6-luna,"
            "antigravity:gemini-4-neon,antigravity:claude-opus-5-5",
    "low": "claudeAgent_claude_extra:claude-opus-5-5,claudeAgent_claude_extra:claude-haiku-5-5,spent:gpt-6-2-sol,"
           "antigravity:gemini-4-hydrogen,antigravity:claude-sonnet-5-5,openai-api:gpt-6-2-sol",
    "edge": "codex:gpt-6-2-astra,codex:gpt-6-1-terra,antigravity:gemini-pro-orchestrator-neon,claudeAgent:claude-fable-5-2",
    "spent": "spent:claude-opus-5-5,spent:gpt-6-2-sol,antigravity:claude-opus-5-5,codex:gpt-6-1-terra,codex:gpt-6-luna",
}


def common(assets="quota.yaml"):
    return ["--assets", "<TMP>/" + assets, "--catalog", "<REPO>/skills/agent-orchestration/catalog.yaml", "--now", NOW]


def matrix():
    """{group: [argv, ...]}; each argv runs compact and with --full."""
    sel, cx_all = [], ("trivial", "standard", "hard", "frontier")
    for name, avail in AVAIL.items():
        for cx in cx_all:
            for risk in ("low", "medium", "high"):
                ref = ["--ref", "item-%s-%s" % (cx, risk)] if name == "spent" else []
                sel.append(["select", "--complexity", cx, "--risk", risk, "--available", avail, *ref, *common()])
    for extra in (["--ui"], ["--ui", "--fallback", "defer", "--ref", "ui-1"], ["--prefer-family", "openai"],
                  ["--fail-on-low"], ["--below-tier", "proceed", "--ref", "bt-1"], ["--below-tier", "wait", "--ref", "bt-2"]):
        sel.append(["select", "--complexity", "hard", "--risk", "high", "--available", AVAIL["spent"], *extra, *common()])
    for impl in ("gpt-6-2-sol", "claude-opus-5-5", "gemini-4-neon"):
        for name in ("main", "low"):
            for cx, risk in (("standard", "low"), ("standard", "high"), ("hard", "medium"), ("frontier", "high")):
                sel.append(["select", "--role", "reviewer", "--complexity", cx, "--risk", risk, "--implementer", impl,
                            "--available", AVAIL[name], *common()])
    for assets in ("quota.yaml", "quota-orch.yaml"):
        for name, avail in AVAIL.items():
            for coord in "012":
                sel.append(["select", "--role", "orchestrator", "--coordination", coord, "--available", avail, *common(assets)])
        for phase in ("plan", "review", "supervise"):
            sel.append(["select", "--role", "orchestrator", "--coordination", "1", "--phase", phase, "--current",
                        "claudeAgent:claude-opus-5-5@high", "--available", AVAIL["main"], *common(assets)])
    rubric = ("--size", "--uncertainty", "--deps", "--consequences", "--verification", "--coordination")
    classify = [["classify", *[x for k, v in zip(rubric, r) for x in (k, v)], "--assets", "<TMP>/quota.yaml"]
                for r in ("000000", "100000", "111111", "212112", "222222", "020201")]
    led = ["--ledger", "<TMP>/ledger.jsonl", *common()]
    history = [["history", *x, *led] for x in ([], ["--last", "2"], ["--ref", "PR-1"], ["--ref", "nope"])]
    history += [["history", "--role", r, *by, *led] for r in ("implementer", "reviewer", "orchestrator")
                for by in ([], ["--by", "model,effort"])]
    launch = [["launch", "<TMP>/%s.launch.yaml" % s, "--no-probe", *av, "--out", "<TMP>/payload.json", *common()]
              for s in ("orchestrator", "reviewer") for av in ([], ["--available", AVAIL["low"]])]
    esc = lambda role, cur, avail, *more, assets="quota.yaml", cause="rework", ref="item-9": [  # noqa: E731
        "escalate", "--role", role, "--ref", ref, "--current", cur, "--cause", cause, "--available", avail,
        *more, "--dry-run", *led[:2], *common(assets)]
    hl, sl = ("--complexity", "hard", "--risk", "low"), ("--complexity", "standard", "--risk", "low")
    escalate = [
        esc("implementer", "codex:gpt-6-2-sol@medium", AVAIL["main"], *hl),
        esc("implementer", "claudeAgent:claude-haiku-5-5@xhigh", AVAIL["main"], *sl),
        esc("implementer", "codex:gpt-6-2-sol@xhigh", "codex:gpt-6-2-sol", *hl),
        esc("implementer", "claudeAgent:claude-fable-5-2@high", AVAIL["main"], *hl),
        esc("implementer", "codex:gpt-6-1-terra@none", AVAIL["edge"], *sl),
        esc("implementer", "antigravity:gemini-4-argon@low", AVAIL["low"], *sl),
        esc("reviewer", "antigravity:gemini-4-neon@high", AVAIL["main"], *hl, "--implementer", "gpt-6-2-sol"),
        esc("reviewer", "claudeAgent:claude-sonnet-5-5@xhigh", AVAIL["main"], *hl, "--implementer", "claude-opus-5-5"),
        esc("orchestrator", "claudeAgent:claude-opus-5-5@medium", AVAIL["main"], "--coordination", "1", assets="quota-orch.yaml"),
        esc("orchestrator", "codex:gpt-6-2-sol@high", AVAIL["main"], "--coordination", "2"),
        esc("orchestrator", "codex:gpt-6-2-sol@high", AVAIL["main"], "--coordination", "2", "--findings", "9", cause="findings"),
        esc("orchestrator", "antigravity:gemini-pro-orchestrator-neon@high", AVAIL["main"], "--coordination", "0"),
        esc("implementer", "codex:gpt-6-2-sol@xhigh", AVAIL["main"], *hl, ref="PR-1"),
        esc("implementer", "codex:gpt-6-2-sol@medium", AVAIL["main"], *hl, "--findings", "1", cause="findings"),
        esc("implementer", "codex:gpt-6-2-sol@medium", AVAIL["main"], *hl, "--findings", "4", cause="findings"),
        esc("implementer", "claudeAgent:claude-sonnet-5-5@high", AVAIL["low"], *hl, cause="status"),
        esc("implementer", "codex:no-such-model@high", AVAIL["main"], *hl),
    ]
    return {"select": sel, "classify": classify, "history": history, "launch": launch, "escalate": escalate}


def run_case(argv):
    """Run one argv in a fresh temp copy of the inputs; return the normalised snapshot body."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(INPUT, tmp, dirs_exist_ok=True)
        os.makedirs(os.path.join(tmp, "home"))
        env = {"HOME": os.path.join(tmp, "home"), "PATH": os.environ.get("PATH", ""), "NO_COLOR": "1",
               "AGENT_ORCHESTRATION_ASSETS": os.path.join(tmp, "quota.yaml"), "LANG": "C.UTF-8",
               "CLAUDE_CONFIG_DIR": os.path.join(tmp, "home", ".claude"), "CODEX_HOME": os.path.join(tmp, "home", ".codex")}
        real = [x.replace("<TMP>", tmp).replace("<REPO>", REPO_ROOT) for x in argv]
        p = subprocess.run([sys.executable, "-I", ORCH, *real], cwd=tmp, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8", timeout=60)
        body = "rc: %d\n%s" % (p.returncode, p.stdout)
        for extra, path in (("select", "quota.yaml"), ("select", "quota-orch.yaml"), ("launch", "payload.json")):
            if argv[0] == extra and "<TMP>/" + path in argv and os.path.exists(os.path.join(tmp, path)):
                with open(os.path.join(tmp, path), encoding="utf-8") as fh:
                    text = fh.read()
                text = text[text.find("followups:"):] if path.endswith(".yaml") and "followups:" in text else text
                body += "--- %s\n%s\n" % (path, text.rstrip("\n"))
        for src, dst in ((os.path.realpath(tmp), "<TMP>"), (tmp, "<TMP>"), (REPO_ROOT, "<REPO>")):
            body = body.replace(src, dst)
        return body


def render(argvs):
    runs = [(a + extra) for a in argvs for extra in ([], ["--full"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 2) * 2)) as pool:
        bodies = list(pool.map(run_case, runs))
    return [("=== " + " ".join(a), b) for a, b in zip(runs, bodies)]


def parse(text):
    cases, head = [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("=== "):
            head = line.rstrip("\n")
            cases.append([head, ""])
        elif head is not None:
            cases[-1][1] += line
    return [tuple(c) for c in cases]


class GoldenOutput(unittest.TestCase):
    def test_snapshots(self):
        update = os.environ.get("GOLDEN_UPDATE") == "1"
        for group, argvs in matrix().items():
            path = os.path.join(GOLDEN, group + ".txt")
            got = render(argvs)
            if update:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("".join(h + "\n" + b for h, b in got))
                continue
            with open(path, encoding="utf-8") as fh:
                want = parse(fh.read())
            with self.subTest(group=group):
                for (wh, wb), (gh, gb) in zip(want, got):
                    if (wh, wb) != (gh, gb):
                        diff = list(difflib.unified_diff((wh + "\n" + wb).splitlines(), (gh + "\n" + gb).splitlines(),
                                                         "golden", "actual", lineterm=""))
                        self.fail("%s.txt case differs (GOLDEN_UPDATE=1 only if intended): %s\n%s"
                                  % (group, gh[4:], "\n".join(diff[:DIFF_LINES])))
                self.assertEqual(len(want), len(got), "%s.txt case count differs: regenerate with GOLDEN_UPDATE=1" % group)


class EscalateNeverStepsDown(unittest.TestCase):
    """Every catalog rung as --current, every role: a target scores strictly higher (or, from an unscored rung, is the same
    model at a higher rated effort), a reviewer never lands on the implementer's model, the orchestrator stays on its provider
    without approval, a target logs exactly one escalate row, and an ask (exit 2/3) logs nothing. On `--cause findings` a
    scored orchestrator rung either climbs its own provider or continues (exit 0, nothing logged)."""

    def test_every_catalog_rung(self):
        catalog = orch.load_file(CATALOG)
        models = {m["id"]: m for m in catalog["models"]}
        ids = list(models)
        efforts = list(orch.EFFORT_ORDER) + list(catalog.get("emergency_efforts", []))
        emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
        pairs = lambda keep: ",".join("%s:%s" % (p, m) for p in ("codex", "claudeAgent") for m in ids if keep(m))  # noqa: E731
        best = lambda m: max([v for v in (models[m].get("scores") or {}).values()] or [0])  # noqa: E731
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        ledger, parser, problems, runs = os.path.join(tmp, "ledger.jsonl"), orch.build_parser(), [], 0
        targets = {"implementer": 0, "reviewer": 0, "orchestrator": 0}
        pat = re.compile(r"^escalate: \w+ \S+ @ \S+ -> (\S+)/(\S+) @ (\S+) \(cause", re.M)
        for i, mid in enumerate(ids):
            fam = [m for m in ids if m != mid and models[m]["family"] == models[mid]["family"]]
            hl, every = ["--complexity", "hard", "--risk", "low"], pairs(lambda m: True)
            roles = [("implementer", hl, every), ("implementer", ["--complexity", "trivial", "--risk", "low"], every),
                     ("reviewer", hl + ["--implementer", ids[(i + 1) % len(ids)]], every),
                     ("orchestrator", ["--coordination", "0"], every),
                     ("orchestrator", ["--coordination", "1"], every),
                     ("orchestrator", ["--coordination", "1", "--findings", "9"], every)]
            if fam:  # only same-family models offered and the strongest of them implemented: the reviewer must skip it
                roles.append(("reviewer", hl + ["--implementer", max(fam, key=best)], pairs(lambda m: m == mid or m in fam)))
            for eff in efforts:
                for role, more, avail in roles:
                    findings = "--findings" in more
                    argv = ["escalate", "--role", role, "--ref", "p", "--current", "codex:%s@%s" % (mid, eff), "--cause",
                            "findings" if findings else "rework", "--available", avail, *more, "--ledger", ledger,  # real run: logging is under test
                            "--assets", os.path.join(tmp, "quota.yaml"), "--catalog", CATALOG, "--now", NOW]
                    impl = more[-1] if role == "reviewer" else None
                    floor = (orch.FLOORS[orch.ORCH_TIERS_DEFAULT[int(more[1])]] if role == "orchestrator"
                             else orch.FLOORS[orch.TIER[more[1]][more[3]]])
                    cur_sc = orch._rung_score(models[mid], eff)
                    case = "%s %s@%s %s" % (role, mid, eff, " ".join(more))
                    out = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(out):
                            rc = orch.escalate_cmd(parser.parse_args(argv))
                    except AssertionError as e:
                        problems.append("%s: orch assertion %s" % (case, e))
                        continue
                    runs += 1
                    text = out.getvalue()
                    rows = []
                    if os.path.exists(ledger):
                        with open(ledger, encoding="utf-8") as fh:
                            rows = [l for l in fh if l.strip()]
                        os.remove(ledger)  # a fresh ledger per case, so the cap never interferes
                    cont = "\naction: no same-provider rung above " in "\n" + text
                    if len(rows) != (1 if rc == 0 and not cont else 0):
                        problems.append("%s: rc %s logged %d row(s)" % (case, rc, len(rows)))
                    # Finding 2 invariant: an auto orchestrator with a higher same-provider tier-meeting rung (<= high) must climb it
                    if role == "orchestrator" and cur_sc is not None:
                        same_up = any(
                            v >= floor and v > cur_sc and not (m == mid and orch.EFFORT_ORDER.get(e, 99) <= orch.EFFORT_ORDER.get(eff, 99))
                            for m in ids if models[m].get("cost") != "emergency" and not models[m].get("coordinator")
                            for e, v in orch._rated_efforts(models[m], emerg, max_effort="high")
                        )
                        if same_up and (rc != 0 or cont):
                            problems.append("%s: dropped higher same-provider orchestrator rung (rc %s)" % (case, rc))
                    if rc != 0:
                        if rc not in (2, 3) or (findings and cur_sc is not None):
                            problems.append("%s: rc %s\n%s" % (case, rc, text))
                        continue
                    if cont:
                        if not findings or rc != 0:
                            problems.append("%s: continue outside the findings trigger\n%s" % (case, text))
                        continue
                    hit = pat.search(text)
                    if not hit:
                        problems.append("%s: rc 0 without a target line\n%s" % (case, text))
                        continue
                    targets[role] += 1
                    if role == "orchestrator" and hit.group(1) != "codex":
                        problems.append("%s -> %s: orchestrator left its provider without approval" % (case, hit.group(1)))
                    to_m, to_e = orch._rung_model(catalog, hit.group(2)), hit.group(3)
                    to_sc = orch._rung_score(to_m, to_e)
                    if to_sc is None:
                        problems.append("%s -> %s@%s: target unrated" % (case, to_m["id"], to_e))
                    elif cur_sc is not None and not to_sc > cur_sc:
                        problems.append("%s -> %s@%s: %s <= %s" % (case, to_m["id"], to_e, to_sc, cur_sc))
                    elif cur_sc is None and not (to_m["id"] == mid and orch.EFFORT_ORDER.get(to_e, 99) > orch.EFFORT_ORDER.get(eff, 99)):
                        problems.append("%s (unscored) -> %s@%s: not the same model at a higher effort" % (case, to_m["id"], to_e))
                    # Finding 1 invariant: target meets the role's tier floor (reviewers may fall below only when no tier-meeting candidate exists)
                    avail_mids = {p.partition(":")[2] for p in avail.split(",")}
                    must_meet = role != "reviewer" or any(
                        m != impl and models[m].get("cost") != "emergency" and orch.best_effort(models[m], floor, emerg) is not None
                        for m in avail_mids if m in models
                    )
                    if to_sc is not None and must_meet and to_sc < floor:
                        problems.append("%s -> %s@%s: %s < tier floor %s" % (case, to_m["id"], to_e, to_sc, floor))
                    if role == "reviewer" and to_m["id"] == impl:
                        problems.append("%s -> %s: the implementer's model" % (case, to_m["id"]))
        self.assertGreater(runs, 300)
        for role, floor in (("implementer", 50), ("reviewer", 50), ("orchestrator", 20)):  # a vacuous all-ask run fails here
            self.assertGreaterEqual(targets[role], floor, "%s: only %d escalation target(s)" % (role, targets[role]))
        if problems:
            self.fail("%d of %d escalate run(s) broke the property:\n%s" % (len(problems), runs, "\n".join(problems[:10])))


if __name__ == "__main__":
    unittest.main()
