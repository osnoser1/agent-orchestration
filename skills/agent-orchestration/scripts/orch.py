#!/usr/bin/env python3
"""Optional, dependency-free helper for the agent-orchestration skill (Python 3.9+).

  orch.py check     [--available provider:model[,...]] [--catalog F] [--assets F] [--now ISO] [--lockfile F]
  orch.py probe     [--fresh] [--provider P] [--low-threshold N] [--exhausted-threshold N] [--available ...] [--assets F] [--now ISO]
  orch.py classify  --size N --uncertainty N --deps N --consequences N --verification N --coordination N [--assets F]
  orch.py select    [--role implementer|reviewer|orchestrator] [--complexity C --risk R | --coordination 0|1|2]
                    --available provider:model[,provider:model...] [--ui] [--prefer-family F]
                    [--fallback proceed|defer|ask] [--below-tier stage|wait|proceed]
                    [--ref TEXT] [--fail-on-low] [--implementer M] [--catalog F] [--assets F] [--now ISO]
                    [--phase plan|review|supervise --current P:M@E]   (orchestrator per-phase effort)
  orch.py launch    <spec> [--available provider:model[,...]] [--no-probe] [--out FILE] [--assets F] [--catalog F] [--now ISO]
  orch.py defer     --ref ITEM [--role implementer|reviewer|orchestrator] [--complexity C --risk R | --coordination 0|1|2]
                    --available ... [--implementer M] [--ui] [--prefer-family F] [--below-tier M]
                    [--pool-busy POOL=N[,...]] [--pool-cap N]
                    [--user-hold --needs N [--after ISO] [--on-recovery]] [--catalog F] [--assets F] [--now ISO]
  orch.py followups [--due] [--add --kind K --ref R --needs N [--after ISO]] [--done R]
                    [--available ...] [--catalog F] [--assets F] [--now ISO]
  orch.py log       --complexity C --risk R --provider P --model M --effort E
                    [--role implementer|reviewer|orchestrator] [--rubric K=N,...] [--requested P:M@E] [--effective P:M@E]
                    [--ui] [--reviewer R] [--surface subagent|conversation] [--event select|complete]
                    [--outcome ok|failed|quota|stall|rework] [--findings N] [--unflagged N] [--repo NAME] [--provisional]
                    [--ref ITEM] [--failure-class C] [--assets F] [--ledger F] [--now ISO]
  orch.py history   [--ref ITEM] [--last N] [--role implementer|reviewer|orchestrator [--by model,effort]] [--ledger F] [--assets F] [--catalog F]
  orch.py escalate  --role R --ref ITEM --current P:M@E --cause rework|findings|status --available ... [--findings N]
                    [--complexity C --risk R | --coordination 0|1|2] [--implementer P:M] [--approved --to P:M@E] [--dry-run]
                    [--ledger F] [--assets F] [--catalog F] [--now ISO]
  orch.py exhaust   <provider> "<quota error message>" [--group G | --model M]
                    [--self --coordination 0|1|2 --available ... | --role orchestrator --coordination 0|1|2 --available ...
                     | --complexity C --risk R --available ... [--role implementer|reviewer] [--implementer M]]
                    [--catalog F] [--assets F] [--now ISO]
  orch.py status    <provider> ok|low [--assets F] [--now ISO]
  orch.py failure   "<error text>" --ref ITEM [--known REGEX=>ACTION ...] [--repo DIR] [--ledger F] [--assets F] [--now ISO]
  orch.py scope     --base REF [--max-files N] [--max-lines N] [--expect GLOB ...] [--repo DIR]
  orch.py land-check --ref R --risk low|medium|high [--available ...] [--assets F] [--catalog F] [--now ISO]
  orch.py policy    [--repo DIR] [--check] [--catalog F] [--assets F]

Exit codes (only the final `action:` line carries the decision):
  0  ok;  1  validation or invocation error
  2  no candidate meets the required tier (select / exhaust with selection args), no reviewer is usable, or no higher rung (escalate)
  3  blocked by pre-flight quota threshold (probe >= low_threshold, or select / exhaust blocked on low/gated/fallback policy),
     escalate stops at its cap or asks the user, or land-check holds landing
  4  quota hit (probe >= exhausted_threshold, or select / exhaust found only exhausted providers)
  5  defer refused: a usable model exists (dispatch or stage now)

`probe`, `classify`, `select`, `launch`, `defer`, `escalate`, `exhaust`, `followups`, `failure`, `scope` and `land-check` end with one `action:` line naming the next step: follow it instead of reading exit codes.
`check`, `policy` and `history` end with `ok: …` or `error: N problem(s); action: …`.
Every command is compact by default: lists stop at 10 items (`… N more (rerun with --full)`), repeated notes group by
reason, and `--full` prints every detail line. `<command> --help` shows one example. (tests/test_output_contract.py)

YAML support is a small subset: block maps, '- ' lists of maps, one-line {..} / [..], # comments.
"""
import argparse, fnmatch, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone, date
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CATALOG = os.path.join(HERE, "..", "catalog.yaml")
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_LOW_THRESHOLD = 85
DEFAULT_EXHAUSTED_THRESHOLD = 98
DEFAULT_LAND_HOLD_HOURS = 4
VALID_UI_FALLBACK = ("proceed", "defer", "ask")
VALID_BELOW_TIER = ("stage", "wait", "proceed")
VALID_PAY_PER_USE = ("never", "ask")
VALID_NEW_CONVERSATIONS = ("ask", "allow")
VALID_DESIGN_CHECKPOINTS = ("ask", "delegate")
VALID_OUTCOMES = ("ok", "failed", "quota", "stall", "rework")
VALID_SURFACES = ("subagent", "conversation")
VALID_ROLES = ("implementer", "reviewer", "orchestrator")
VALID_CAUSES = ("rework", "findings", "status")  # quality-escalation triggers (references/escalation.md)
VALID_PHASES = ("plan", "review", "supervise")  # orchestrator per-phase effort (references/orchestrator.md)
ORCH_TIERS_DEFAULT = {0: "T1", 1: "T2", 2: "T3"}
ORCH_EFFORTS = ("none", "low", "medium", "high")
RUBRIC_KEYS = ("size", "uncertainty", "deps", "consequences", "verification", "coordination")
LAUNCH_KEYS = ("version", "role", "title", "repo", "brief", "rubric", "ladder", "implementer", "available", "workspace", "caps")
EFFORT_KEY_PREFIXES = (("claudeAgent", "effort"), ("codex", "reasoningEffort"), ("antigravity", "thought_level"))
VALID_FAILURE_CLASSES = ("quota", "infra", "context-cap", "tool", "unknown")
RETRY_CLASSES = ("context-cap", "tool", "unknown")  # the only classes that count toward the retry limit
MAX_RETRIES = 2
VALID_FOLLOWUP_KINDS = ("ui-review", "preferred-review", "resume")
RESUME_MARGIN = timedelta(minutes=2)
STALE_INSTALL_DAYS = 7


def def_assets():
    return os.environ.get("AGENT_ORCHESTRATION_ASSETS") or os.path.expanduser(
        "~/.config/agent-orchestration/assets.yaml"
    )


def def_ledger(assets_path=None):
    ap = assets_path or def_assets()
    return os.path.join(os.path.dirname(os.path.abspath(ap)), "ledger.jsonl")


def _opt_str(a, name, default=None):
    v = getattr(a, name, None)
    return v if isinstance(v, str) else default


def _opt_bool(a, name, default=False):
    v = getattr(a, name, default)
    return v if isinstance(v, bool) else default


def _opt_int(a, name, default=None):
    v = getattr(a, name, None)
    return v if (isinstance(v, int) and not isinstance(v, bool)) else default


def _opt_num(a, name, default=None):
    v = getattr(a, name, None)
    return v if (isinstance(v, (int, float)) and not isinstance(v, bool)) else default


# ---------- output contract (tests/test_output_contract.py is the source of truth) ----------
# Compact by default: one `action:` or `ok:` line, repeated notes grouped by reason, lists capped at CAP.
# `--full` prints every detail line and removes the caps.
CAP = 10
_FULL = False
PROVIDER_LEVEL_NOTES = (  # notes about a whole provider: group by provider; any other note names its model
    "provider exhausted",
    "pay-per-use/avoid, skipped",
    "previous exhaustion expired -> treating provider as ok",
    "provider low (blocked by pre-flight threshold)",
)


def _begin(a):
    """Read `--full` once per command; helpers below read the module flag."""
    global _FULL
    _FULL = _opt_bool(a, "full")
    return _FULL


def _more(n):
    return f"… {n} more (rerun with --full)"


def _capped(items, limit=CAP):
    """`items` limited to `limit` (default CAP) unless --full, plus a trailing `… N more` entry when some were cut."""
    items = list(items)
    if _FULL or len(items) <= limit:
        return items
    return items[:limit] + [_more(len(items) - limit)]


def _detail(text):
    """Print a line only under --full."""
    if _FULL:
        print(text)


def _note_lines(notes):
    """`note:` lines for select's candidate notes: grouped by reason (compact) or one per candidate (--full)."""
    if _FULL:
        return ["  note: " + n for n in notes]
    groups = {}
    for n in notes:
        pair, sep, reason = n.partition(": ")
        if not sep:
            pair, reason = "", n
        label = pair.partition(":")[0] if reason in PROVIDER_LEVEL_NOTES else pair
        counts = groups.setdefault(reason, {})
        counts[label] = counts.get(label, 0) + 1
    lines = []
    for reason, counts in groups.items():
        items = [f"{k} ({v})" if v > 1 else k for k, v in counts.items() if k]
        lines.append(f"note: {reason}" + (": " + ", ".join(_capped(items)) if items else ""))
    return _capped(lines)


def _print_notes(notes):
    for line in _note_lines(notes):
        print(line)


def _due_line(fu, usable):
    return (
        f"followup due: [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
        f"(needs {fu.get('needs')}; usable via {', '.join(usable)})"
    )


# ---------- mini YAML ----------
def _strip(line):
    out, q = [], None
    for i, c in enumerate(line):
        if q:
            if c == q:
                q = None
        elif c in "\"'" and (i == 0 or line[i - 1] in " [{,:"):
            q = c
        elif c == "#" and (i == 0 or line[i - 1] == " "):
            break
        out.append(c)
    return "".join(out).rstrip()


def _scalar(s):
    s = s.strip()
    if s == "" or s in ("null", "~"):
        return None
    if s[0] in "\"'" and s[-1] == s[0] and len(s) > 1:
        return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split(s):
    parts, depth, cur, q = [], 0, "", None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
        elif c in "\"'":
            q = c
            cur += c
        elif c in "[{":
            depth += 1
            cur += c
        elif c in "]}":
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    return parts


def _split_kv(p):
    q = None
    for i, c in enumerate(p):
        if q:
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c == ":":
            return p[:i], p[i + 1 :]
    return p, ""


def _unquote_key(k):
    k = k.strip()
    return k[1:-1] if len(k) > 1 and k[0] in "\"'" and k[-1] == k[0] else k


def _value(s):
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        d = {}
        for p in _split(s[1:-1]):
            k, v = _split_kv(p)
            d[_unquote_key(k)] = _value(v)
        return d
    if s.startswith("[") and s.endswith("]"):
        return [_value(p) for p in _split(s[1:-1])]
    return _scalar(s)


def load_yaml(text):
    lines = []
    for raw in text.splitlines():
        l = _strip(raw)
        if l.strip():
            lines.append((len(l) - len(l.lstrip(" ")), l.strip()))

    def block(i, ind):
        if lines[i][1].startswith("- "):
            res = []
            while i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                body = lines[i][1][2:]
                lines[i] = (ind + 2, body)
                if re.match(r"^[^\s\"'{\[][^:]*:(\s|$)", body):
                    v, i = block(i, ind + 2)
                else:
                    v, i = _value(body), i + 1
                res.append(v)
            return res, i
        d = {}
        while i < len(lines) and lines[i][0] == ind and not lines[i][1].startswith("- "):
            k, v = _split_kv(lines[i][1])
            k, v = _unquote_key(k), v.strip()
            i += 1
            if v == "":
                if i < len(lines) and lines[i][0] > ind:
                    d[k], i = block(i, lines[i][0])
                elif i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                    d[k], i = block(i, ind)
                else:
                    d[k] = None
            else:
                d[k] = _value(v)
        return d, i

    return block(0, lines[0][0])[0] if lines else {}


def load_file(path):
    with open(path, encoding="utf-8") as f:
        return load_yaml(f.read())


# ---------- model data ----------
COST = {"cheap": 0, "normal": 1, "expensive": 2, "emergency": 3}
POLICY = {"ending": 0, "spend-first": 1, "normal": 2, "preserve": 3}
STATUS_RANK = {"ok": 0, "low": 1, "exhausted": 2}
FLOORS = {"T1": 20, "T2": 40, "T3": 50, "T4": 57}
EFFORT_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4}
_WINDOW_ALIAS = {"weekly": "7d", "seven_day": "7d", "five_hour": "5h", "5h": "5h", "7d": "7d"}
TIER = {
    "trivial": {"low": "T1", "medium": "T1", "high": "T2"},
    "standard": {"low": "T2", "medium": "T2", "high": "T3"},
    "hard": {"low": "T3", "medium": "T3", "high": "T4"},
    "frontier": {"low": "T4", "medium": "T4", "high": "T4"},
}
norm = lambda s: re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def get_now(iso_arg=None):
    base = datetime.now(timezone.utc).astimezone()
    if not iso_arg:
        return base
    parsed = parse_time(iso_arg, base)
    return parsed if parsed else base


def parse_time(s, now):
    if s is None:
        return None
    if isinstance(s, (date, datetime)):
        s = str(s)
    text = str(s).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=now.tzinfo)
        except ValueError:
            return None
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=now.tzinfo)


def parse_cadence_or_time(val, now):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    dt = parse_time(s, now)
    if dt:
        return dt
    low = s.lower()
    cadences = {
        "session": timedelta(hours=5),
        "5h": timedelta(hours=5),
        "daily": timedelta(days=1),
        "24h": timedelta(days=1),
        "weekly": timedelta(days=7),
        "7d": timedelta(days=7),
        "monthly": timedelta(days=30),
        "30d": timedelta(days=30),
    }
    if low in cadences:
        return now + cadences[low]
    return None


def provider_next_reset(p, now):
    if not p:
        return None
    if p.get("policy") == "ending" and p.get("renews"):
        dt = parse_cadence_or_time(p.get("renews"), now)
        if dt and dt > now:
            return dt
    times = []
    for k in ("resets_at", "resets", "renews"):
        dt = parse_cadence_or_time(p.get(k), now)
        if dt and dt > now:
            times.append(dt)
    return min(times) if times else None


def load_catalog_with_local(catalog_path, assets_path):
    cat = load_file(catalog_path)
    local_path = os.path.join(os.path.dirname(os.path.abspath(assets_path)), "catalog.local.yaml")
    if not os.path.exists(local_path):
        return cat
    loc = load_file(local_path)
    if not isinstance(loc, dict):
        return cat
    merged = dict(cat)
    merged["index_versions"] = {
        **(cat.get("index_versions") or {}),
        **(loc.get("index_versions") or {}),
    }
    if loc.get("emergency_efforts"):
        merged["emergency_efforts"] = list(
            dict.fromkeys((cat.get("emergency_efforts") or []) + list(loc.get("emergency_efforts") or []))
        )
    by_id = {}
    order = []
    for m in (cat.get("models") or []):
        k = norm(m["id"])
        by_id[k] = m
        order.append(k)
    for m in (loc.get("models") or []):
        k = norm(m["id"])
        if k not in by_id:
            order.append(k)
        else:
            prev = by_id[k]
            if "coordinator" in prev and "coordinator" not in m:
                m = {**m, "coordinator": prev["coordinator"]}
        by_id[k] = m
    merged["models"] = [by_id[k] for k in order]
    return merged


def provider_state(assets, pid, now):
    p = next((x for x in (assets or {}).get("providers", []) or [] if x.get("id") == pid), {})
    st = p.get("status", "ok")
    expired = False
    until = parse_time(p.get("exhausted_until") or p.get("resets_at"), now)
    if st == "exhausted" and until and until <= now:
        st = "ok"
        expired = True
    return p, st, expired


def partitions_quota(p):
    """True when the provider's groups split one account into separate pools (Antigravity probe groups).

    Other providers' groups are per-model caps layered on the account-level status
    (seeded by `exhaust --model`), so provider status stays independent of them.
    """
    p = p or {}
    if ((_infer_probe(p) or {}).get("kind")) == "antigravity":
        return True
    return any(isinstance(g, dict) and g.get("window") for g in (p.get("groups") or []))


def _model_keys(mid, model_obj=None):
    """Normalised spellings of a model: the given id plus the catalog id, name, and aliases."""
    keys = {norm(mid)}
    if isinstance(model_obj, dict):
        for k in [model_obj.get("id"), model_obj.get("name")] + list(model_obj.get("aliases") or []):
            if k:
                keys.add(norm(k))
    return keys


def model_group(p, mid, model_obj=None):
    groups = (p or {}).get("groups")
    if not isinstance(groups, list) or not groups:
        return None
    keys = _model_keys(mid, model_obj)
    for g in groups:
        if not isinstance(g, dict):
            continue
        models = g.get("models")
        if isinstance(models, list) and models:
            if any(norm(gm) in keys for gm in models):
                return g
        elif g.get("name") and norm(g["name"]) in keys:
            return g
    return None


def group_state(g, now):
    st = g.get("status", "ok")
    expired = False
    until = parse_time(g.get("exhausted_until") or g.get("resets_at"), now)
    if st == "exhausted" and until and until <= now:
        st = "ok"
        expired = True
    return st, expired


def model_lock(g, mid, model_obj, now):
    """`(until, expired)` of the model's own lock in group `g` (`exhaust` on an "individual quota" message), else None."""
    locks = (g or {}).get("exhausted_models")
    if not isinstance(locks, dict):
        return None
    keys = _model_keys(mid, model_obj)
    matches = [parse_time(v, now) for k, v in locks.items() if norm(k) in keys]
    if not matches:
        return None
    valid = [dt for dt in matches if dt is not None]
    until = max(valid) if valid else None
    return until, until is None or until <= now


def _is_lock_carrier(g):
    """A group entry holding only individual-quota locks (no pool data): seeded by `exhaust`, kept by `probe`, never a pool."""
    return isinstance(g, dict) and bool(g.get("exhausted_models")) and not any(
        k in g for k in ("status", "used_percent", "window", "exhausted_until", "resets_at"))


def _locked(g, mid, model_obj, now):
    """The unexpired individual lock's end time, else None."""
    lock = model_lock(g, mid, model_obj, now) if g is not None else None
    return lock[0] if lock and not lock[1] else None


def candidate_next_reset(p, g, now):
    if not p:
        return None
    if p.get("policy") == "ending" and p.get("renews"):
        dt = parse_cadence_or_time(p.get("renews"), now)
        if dt and dt > now:
            return dt
    if g is None:
        return provider_next_reset(p, now)
    times = []
    for val in (g.get("resets_at"), p.get("resets"), p.get("renews")):
        dt = parse_cadence_or_time(val, now)
        if dt and dt > now:
            times.append(dt)
    return min(times) if times else None


def _rated_efforts(m, emerg, max_effort=None):
    scores = m.get("scores") or {}
    cap = EFFORT_ORDER.get(max_effort, 99) if max_effort is not None else 99
    rated = [
        (e, v)
        for e, v in scores.items()
        if isinstance(v, (int, float))
        and not isinstance(v, bool)
        and e not in emerg
        and EFFORT_ORDER.get(e, 99) <= cap
    ]
    rated.sort(key=lambda kv: (EFFORT_ORDER.get(kv[0], 99), kv[1]))
    return rated


def best_effort(m, floor, emerg, max_effort=None):
    rated = _rated_efforts(m, emerg, max_effort=max_effort)
    for idx, (e, v) in enumerate(rated):
        if v >= floor:
            # Effort knee: if stepping down one rated effort drops <3 points and still meets the floor, take the lower effort.
            if idx > 0 and (v - rated[idx - 1][1]) < 3 and rated[idx - 1][1] >= floor:
                return rated[idx - 1]
            return e, v
    return None


def top_effort(m, emerg, max_effort=None):
    rated = _rated_efforts(m, emerg, max_effort=max_effort)
    return max(rated, key=lambda kv: (kv[1], -EFFORT_ORDER.get(kv[0], 99))) if rated else None


def unrated_higher_efforts(m, emerg, max_effort=None):
    scores = m.get("scores") or {}
    cap = EFFORT_ORDER.get(max_effort, 99) if max_effort is not None else 99
    rated_ranks = [
        EFFORT_ORDER[e]
        for e, v in scores.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and e in EFFORT_ORDER and e not in emerg and EFFORT_ORDER[e] <= cap
    ]
    max_rated = max(rated_ranks) if rated_ranks else -1
    return [
        e
        for e, rank in sorted(EFFORT_ORDER.items(), key=lambda kv: kv[1])
        if max_rated < rank <= cap and e not in emerg and e not in scores
    ]


def _model_matches_need(m, need, floors=None, emerg=None, provider_id=None, group_name=None):
    if not isinstance(m, dict) or not need:
        return False
    n = str(need).strip().lower()
    if n in ("any", "*"):
        return True
    if n.startswith("provider:"):
        parts = [p.strip() for p in n[len("provider:"):].split("/") if p.strip()]
        if len(parts) < 2 or not provider_id or norm(provider_id) != norm(parts[0]):
            return False
        if len(parts) == 2:
            return _model_matches_need(m, parts[1], floors=floors, emerg=emerg)
        if len(parts) == 3:
            # v0.21.0/0.21.1 named a lock carrier after its model (`provider:<p>/<model>/<tier>`): match that model too.
            own = norm(parts[1]) in {norm(x) for x in [m.get("id"), m.get("name"), *(m.get("aliases") or [])] if x}
            if not own and (not group_name or norm(group_name) != norm(parts[1])):
                return False
            return _model_matches_need(m, parts[2], floors=floors, emerg=emerg)
        return False
    if n.startswith("!"):  # `!<family>/<tier>`: a model of any other family, meeting the tier
        fam, sep, tier = n[1:].partition("/")
        if not sep or not fam or tier.upper() not in FLOORS or str(m.get("family") or "").lower() == fam:
            return False
        return _model_matches_need(m, tier, floors=floors, emerg=emerg)
    t_key = n[len("tier:"):].upper() if n.startswith("tier:") else n.upper()
    if t_key in FLOORS:
        fl = (floors or FLOORS).get(t_key, FLOORS[t_key])
        em = emerg if emerg is not None else {"max", "ultra"}
        return best_effort(m, fl, em) is not None
    if norm(m.get("id") or "") == norm(n):
        return True
    if n.startswith("family:"):
        return str(m.get("family") or "").lower() == n[len("family:"):]
    strengths = [str(s).lower() for s in (m.get("strengths") or [])]
    if n in strengths:
        return True
    return str(m.get("family") or "").lower() == n


def _provider_inferred_family(p):
    if p.get("family"):
        return str(p["family"]).strip().lower()
    probe_cfg = p.get("probe") if isinstance(p.get("probe"), dict) else {}
    kind = str(probe_cfg.get("kind") or "").lower()
    pid = str(p.get("id") or "").lower()
    if kind == "claude" or "claude" in pid or "anthropic" in pid:
        return "anthropic"
    if kind == "codex" or "codex" in pid or "openai" in pid:
        return "openai"
    if kind == "antigravity" or "gemini" in pid or "google" in pid:
        return "google"
    return None


def find_usable_in_available(catalog, assets, now, need, available, fail_on_low=False):
    if not need or not available:
        return []
    cat = {norm(m["id"]): m for m in (catalog or {}).get("models") or []}
    emerg = set((catalog or {}).get("emergency_efforts", ["max", "ultra"]))
    prefs = ((assets or {}).get("preferences") or {}) if isinstance(assets, dict) else {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    usable = []
    seen = set()

    def _add(label):
        if label not in seen:
            seen.add(label)
            usable.append(label)

    for pair in available:
        prov, _, mid = pair.partition(":")
        m = cat.get(norm(mid))
        if not m or m.get("cost") == "emergency" or m.get("rating") == "unrated":
            continue
        p, prov_st, _ = provider_state(assets, prov, now)
        blocked = ("exhausted", "low") if fail_on_low else ("exhausted",)
        if p.get("pay_per_use") or p.get("policy") == "avoid" or prov_st in blocked:
            continue
        g = model_group(p, mid, m)
        real_g = g if isinstance(g, dict) and not _is_lock_carrier(g) else None
        gname = real_g.get("name") if real_g is not None else None
        if not _model_matches_need(m, need, floors=floors, emerg=emerg, provider_id=prov, group_name=gname):
            continue
        if g is not None:
            gst, _ = group_state(g, now)
            if gst in blocked or _locked(g, mid, m, now):
                continue
            _add(f"{prov} ({real_g.get('name') or 'group'})" if real_g is not None else prov)
        else:
            _add(prov)

    return usable


def find_usable_in_assets(catalog, assets, now, need, fail_on_low=False):
    if not need:
        return []
    cat = {norm(m["id"]): m for m in (catalog or {}).get("models") or []}
    emerg = set((catalog or {}).get("emergency_efforts", ["max", "ultra"]))
    prefs = ((assets or {}).get("preferences") or {}) if isinstance(assets, dict) else {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    usable = []
    seen = set()

    def _add(label):
        if label not in seen:
            seen.add(label)
            usable.append(label)

    for p in (assets or {}).get("providers") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            continue
        pid = p["id"]
        _, prov_st, _ = provider_state(assets, pid, now)
        blocked = ("exhausted", "low") if fail_on_low else ("exhausted",)
        if prov_st in blocked:
            continue
        groups = p.get("groups")
        real_groups = [g for g in groups if isinstance(g, dict) and not _is_lock_carrier(g)] if isinstance(groups, list) else []
        if real_groups and partitions_quota(p):
            for g in real_groups:
                gst, _ = group_state(g, now)
                if gst in blocked:
                    continue
                for mid in g.get("models") or []:
                    m = cat.get(norm(mid))
                    if (
                        m
                        and m.get("cost") != "emergency"
                        and m.get("rating") != "unrated"
                        and not _locked(g, mid, m, now)
                        and _model_matches_need(
                            m, need, floors=floors, emerg=emerg, provider_id=pid, group_name=g.get("name")
                        )
                    ):
                        _add(f"{pid} ({g.get('name') or 'group'})")
                        break
        else:
            fam = _provider_inferred_family(p)
            if fam:
                for m in (catalog or {}).get("models") or []:
                    cap = model_group(p, m["id"], m)
                    if cap is not None and (group_state(cap, now)[0] in blocked or _locked(cap, m["id"], m, now)):
                        continue
                    if (
                        str(m.get("family") or "").lower() == fam
                        and m.get("cost") != "emergency"
                        and m.get("rating") != "unrated"
                        and _model_matches_need(
                            m,
                            need,
                            floors=floors,
                            emerg=emerg,
                            provider_id=pid,
                            group_name=cap.get("name") if cap is not None and not _is_lock_carrier(cap) else None,
                        )
                    ):
                        _add(pid)
                        break

    return usable


def _resume_not_yet(fu, now):
    """A `kind: resume` entry waits for its `after` time, unless it waits on an exhausted pool (`on_recovery`).

    Quota waits become due as soon as a matching model is usable again, because pools can renew early;
    deferrals on a renewing (not exhausted) provider keep waiting for `after`.
    """
    if fu.get("kind") != "resume" or not fu.get("after") or fu.get("on_recovery") is True:
        return False
    after = parse_time(fu.get("after"), now)
    return bool(after and after > now)


def due_followups(catalog, assets, now, available=None):
    if not available:
        return []
    due = []
    for fu in (assets or {}).get("followups") or []:
        if not isinstance(fu, dict) or _resume_not_yet(fu, now):
            continue
        usable = find_usable_in_available(
            catalog, assets, now, fu.get("needs"), available, fail_on_low=fu.get("fail_on_low") is True
        )
        if usable:
            due.append((fu, usable))
    return due


def pending_ready_followups(catalog, assets, now):
    ready = []
    for fu in (assets or {}).get("followups") or []:
        if not isinstance(fu, dict) or _resume_not_yet(fu, now):
            continue
        usable = find_usable_in_assets(catalog, assets, now, fu.get("needs"), fail_on_low=fu.get("fail_on_low") is True)
        if usable:
            ready.append((fu, usable))
    return ready


def _due_lines(due):
    """`followup due:` lines: one per follow-up under --full, else one per (kind, usable pool) listing its refs."""
    if _FULL:
        return [_due_line(fu, usable) for fu, usable in due]
    groups = {}
    for fu, usable in due:
        groups.setdefault((fu.get("kind", "review"), ", ".join(usable)), []).append(str(fu.get("ref", "")))
    return _capped([f"followup due: [{kind}] via {pool}: " + ", ".join(_capped(refs)) for (kind, pool), refs in groups.items()])


def _print_due(catalog, assets, now, available):
    """Print the due follow-ups (capped); return how many are due."""
    due = due_followups(catalog, assets, now, available=available)
    for line in _due_lines(due):
        print(line)
    return len(due)


def _pending_line(fu, usable=None):
    head = f"followup pending: needs {fu.get('needs')} — [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
    if usable:
        return head + f"(usable in assets via {', '.join(usable)}; pass --available to check this harness)"
    return head + f"(after {fu.get('after') or 'next reset'})"


def print_followups_status(catalog, assets, now, available=None, due_only=True):
    """Print follow-up lines (capped); return how many are due for this harness (0 without `available`)."""
    all_fus = [x for x in (assets or {}).get("followups") or [] if isinstance(x, dict)]
    lines, n_due = [], 0
    if available:
        due = due_followups(catalog, assets, now, available=available)
        n_due = len(due)
        due_ids = {id(fu) for fu, _ in due}
        lines += _due_lines(due)
        if not due_only:
            lines += [_pending_line(fu) for fu in all_fus if id(fu) not in due_ids]
            if not all_fus:
                lines.append("no followups")
    else:
        ready_map = {id(fu): usable for fu, usable in pending_ready_followups(catalog, assets, now)}
        if due_only:
            lines += [_pending_line(fu, usable) for fu in all_fus for usable in [ready_map.get(id(fu))] if usable]
        else:
            lines += [_pending_line(fu, ready_map.get(id(fu))) for fu in all_fus]
            if not all_fus:
                lines.append("no followups")
    for line in _capped(lines):
        print(line)
    return n_due


def classify_unmet_preference(catalog, assets, available, need, floor, now, fail_on_low=False):
    if not need or not available:
        return "not-offered", None
    cat = {norm(m["id"]): m for m in (catalog or {}).get("models") or []}
    emerg = set((catalog or {}).get("emergency_efforts", ["max", "ultra"]))
    offered_exhausted = False
    times = []

    def _collect(dt):
        if dt and dt > now:
            times.append(dt)

    for pair in available:
        prov, _, mid = pair.partition(":")
        m = cat.get(norm(mid))
        if not m or m.get("cost") == "emergency" or m.get("rating") == "unrated":
            continue
        if not _model_matches_need(m, need):
            continue
        if best_effort(m, floor, emerg) is None:
            continue
        p, prov_st, _ = provider_state(assets, prov, now)
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            continue
        g = model_group(p, mid, m)
        st = group_state(g, now)[0] if g is not None else prov_st
        lock_until = _locked(g, mid, m, now)
        if prov_st == "exhausted" or lock_until:
            st = "exhausted"
        if st == "exhausted" or (fail_on_low and st == "low"):
            offered_exhausted = True
            if g is not None and group_state(g, now)[0] == "exhausted":
                _collect(parse_time(g.get("exhausted_until") or g.get("resets_at"), now) or candidate_next_reset(p, g, now))
            elif prov_st == "exhausted":
                _collect(parse_time(p.get("exhausted_until") or p.get("resets_at"), now) or provider_next_reset(p, now))
            elif lock_until:
                _collect(lock_until)
            else:
                _collect(candidate_next_reset(p, g, now) if g is not None else provider_next_reset(p, now))

    if offered_exhausted:
        return "exhausted", (min(times).isoformat(timespec="seconds") if times else None)
    return "not-offered", None


def exhausted_tier_candidates(
    catalog,
    assets,
    available,
    tier,
    floor,
    now,
    ui_primary=False,
    prefer_family=None,
    max_effort=None,
    exclude_coordinator=False,
):
    if not available:
        return False, None, tier, []
    cat = {norm(m["id"]): m for m in (catalog or {}).get("models") or []}
    emerg = set((catalog or {}).get("emergency_efforts", ["max", "ultra"]))
    pref_fam = prefer_family.strip().lower() if prefer_family else None
    ex_cands = []

    for pair in available:
        prov, _, mid = pair.partition(":")
        m = cat.get(norm(mid))
        if not m or m.get("cost") == "emergency" or (exclude_coordinator and m.get("coordinator") is True) or m.get("rating") == "unrated":
            continue
        be = best_effort(m, floor, emerg, max_effort=max_effort)
        if be is None:
            continue
        p, prov_st, _ = provider_state(assets, prov, now)
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            continue
        g = model_group(p, mid, m)
        gst = group_state(g, now)[0] if g is not None else "ok"
        lock_until = _locked(g, mid, m, now)
        if prov_st != "exhausted" and gst != "exhausted" and not lock_until:
            continue

        dts = [lock_until] if lock_until else []
        if g is not None and gst == "exhausted":
            dt = parse_time(g.get("exhausted_until") or g.get("resets_at"), now) or candidate_next_reset(p, g, now)
            if dt and dt > now:
                dts.append(dt)
        if prov_st == "exhausted":
            dt = parse_time(p.get("exhausted_until") or p.get("resets_at"), now) or provider_next_reset(p, now)
            if dt and dt > now:
                dts.append(dt)
        reset_dt = max(dts) if dts else None
        reset_ts = reset_dt.timestamp() if reset_dt else float("inf")

        policy = p.get("policy", "normal")
        pol_rank = POLICY.get(policy, 2)
        ui_rank = (0 if "ui-ux-review" in (m.get("strengths") or []) else 1) if ui_primary else 0
        fam_rank = (0 if _model_matches_need(m, pref_fam) else 1) if pref_fam else 0
        key = (
            reset_ts,
            ui_rank,
            fam_rank,
            pol_rank,
            COST[m["cost"]],
            EFFORT_ORDER.get(be[0], 99),
            be[1],
        )
        ex_cands.append(
            {
                "pair": pair,
                "provider": prov,
                "group": g.get("name") if isinstance(g, dict) and not _is_lock_carrier(g) else None,
                "individual_only": bool(lock_until and prov_st != "exhausted" and gst != "exhausted"),
                "model": m,
                "effort": be[0],
                "score": be[1],
                "policy": policy,
                "status": "exhausted",
                "reset_dt": reset_dt,
                "resets": reset_dt.isoformat(timespec="seconds") if reset_dt else None,
                "key": key,
            }
        )

    ex_cands.sort(key=lambda c: c["key"])
    dts_all = [c["reset_dt"] for c in ex_cands if c["reset_dt"] is not None]
    earliest_dt = min(dts_all) if dts_all else None
    ex_scopes = {(c["provider"], c["group"]) for c in ex_cands}
    indiv_models = {(c["provider"], c["model"]["id"]) for c in ex_cands}
    if ex_cands and all(c["individual_only"] for c in ex_cands) and len(indiv_models) == 1:
        resume_needs = f"provider:{ex_cands[0]['provider']}/{ex_cands[0]['model']['id']}"
    elif ex_cands and not any(c["individual_only"] for c in ex_cands) and len(ex_scopes) == 1 and ex_cands[0]["group"]:
        resume_needs = f"provider:{ex_cands[0]['provider']}/{ex_cands[0]['group']}/{tier}"
    else:
        resume_needs = tier
    return len(ex_cands) > 0, earliest_dt, resume_needs, ex_cands


def earliest_exhausted_tier_reset(catalog, assets, available, floor, now):
    saw, dt, _, _ = exhausted_tier_candidates(catalog, assets, available, "T1", floor, now)
    return saw, dt


def candidates(
    catalog,
    assets,
    available,
    floor,
    floors,
    now,
    exclude_family=None,
    ui_primary=False,
    prefer_family=None,
    fail_on_low=False,
    pick_top=False,
    task_floor=None,
    max_effort=None,
    exclude_coordinator=False,
):
    cat = {norm(m["id"]): m for m in catalog["models"]}
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    out, notes = [], []
    reasons = {"exhausted": 0, "threshold": 0, "gated": 0, "askable": 0, "tier": 0}
    ppu_ask = ((assets or {}).get("preferences") or {}).get("pay_per_use") == "ask"
    t3_floor = floors.get("T3", FLOORS["T3"])
    eff_floor = task_floor if task_floor is not None else floor
    pref_fam = prefer_family.strip().lower() if prefer_family else None

    for pair in available:
        prov, _, mid = pair.partition(":")
        p, prov_st, prov_expired = provider_state(assets, prov, now)
        if prov_expired:
            notes.append(f"{pair}: previous exhaustion expired -> treating provider as ok")

        # Filter provider and quota-group gates BEFORE catalog/unrated checks.
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            notes.append(f"{pair}: pay-per-use/avoid, skipped")
            reasons["gated"] += 1
            reasons["askable"] += bool(p.get("pay_per_use") and p.get("policy") != "avoid" and ppu_ask)
            continue
        if prov_st == "exhausted":
            notes.append(f"{pair}: provider exhausted")
            reasons["exhausted"] += 1
            continue

        m = cat.get(norm(mid))
        g = model_group(p, mid, m)
        if g is not None:
            st, g_expired = group_state(g, now)
            gname = g.get("name") or "group"
            if g_expired:
                notes.append(f"{pair}: previous exhaustion in group {gname} expired -> treating as ok")
            if st == "exhausted":
                notes.append(f"{pair}: group {gname} exhausted")
                reasons["exhausted"] += 1
                continue
            lock = model_lock(g, mid, m, now)
            if lock and lock[1]:
                notes.append(f"{pair}: previous individual exhaustion expired -> treating as ok")
            elif lock:
                notes.append(f"{pair}: model individually exhausted (until {lock[0].isoformat(timespec='seconds')})")
                reasons["exhausted"] += 1
                continue
            if fail_on_low and st == "low":
                notes.append(f"{pair}: group {gname} low (blocked by pre-flight threshold)")
                reasons["threshold"] += 1
                continue
        else:
            st = prov_st
            if fail_on_low and st == "low":
                notes.append(f"{pair}: provider low (blocked by pre-flight threshold)")
                reasons["threshold"] += 1
                continue

        if not m:
            notes.append(f"{pair}: not in catalog -> research once (references/research-unrated.md)")
            reasons["tier"] += 1
            continue
        if m.get("cost") == "emergency":
            notes.append(f"{pair}: emergency, skipped")
            reasons["gated"] += 1
            reasons["askable"] += 1
            continue
        if exclude_coordinator and m.get("coordinator") is True:
            notes.append(f"{pair}: coordinator mode excluded: adds a second coordinator under yours")
            reasons["tier"] += 1
            continue
        if m.get("rating") == "unrated":
            notes.append(f"{pair}: unrated -> research once (references/research-unrated.md)")
            reasons["tier"] += 1
            continue
        if exclude_family and m["family"] == exclude_family:
            continue

        be = top_effort(m, emerg, max_effort=max_effort) if pick_top else best_effort(m, floor, emerg, max_effort=max_effort)
        if not be:
            missing = unrated_higher_efforts(m, emerg, max_effort=max_effort)
            cap_txt = f" (<= {max_effort})" if max_effort is not None else ""
            if missing:
                notes.append(
                    f"{pair}: no rated non-emergency effort{cap_txt} reaches {floor} (unrated efforts: {','.join(missing)} -> research once)"
                )
            else:
                notes.append(f"{pair}: no non-emergency effort{cap_txt} reaches {floor}")
            reasons["tier"] += 1
            continue

        policy = p.get("policy", "normal")
        pol_rank = POLICY.get(policy, 2)
        low_pen = 1 if (st == "low" and eff_floor >= t3_floor) else 0
        # Weak on T3+ work: a low-confidence score, or a best rated score (<= max_effort) less than 3 points above the floor.
        top = top_effort(m, emerg, max_effort=max_effort) or be
        weak = 1 if eff_floor >= t3_floor and (m.get("confidence") == "low" or top[1] < eff_floor + 3) else 0
        nxt = candidate_next_reset(p, g, now)
        reset_ts = nxt.timestamp() if nxt else float("inf")
        ui_rank = (0 if "ui-ux-review" in (m.get("strengths") or []) else 1) if ui_primary else 0
        fam_rank = (0 if _model_matches_need(m, pref_fam) else 1) if pref_fam else 0
        key = (
            ui_rank,
            fam_rank,
            low_pen,
            weak,
            pol_rank,
            reset_ts,
            COST[m["cost"]],
            EFFORT_ORDER.get(be[0], 99),
            be[1],
        )
        out.append(
            {
                "pair": pair,
                "provider": prov,
                "group": g.get("name") if isinstance(g, dict) else None,
                "model": m,
                "effort": be[0],
                "score": be[1],
                "policy": policy,
                "status": st,
                "resets": nxt.isoformat(timespec="seconds") if nxt else None,
                "key": key,
            }
        )
    return out, notes, reasons


def _fmt_cand(c):
    return (
        f"{c['provider']} / {c['model']['id']} @ {c['effort']} "
        f"(score {c['score']}, policy {c['policy']}"
        + (f", status {c['status']}" if c["status"] != "ok" else "")
        + ")"
    )


def _cand_ref(c):
    return f"{c['provider']}/{c['model']['id']} @ {c['effort']}"


def _action(text):
    """Print the final `action:` line: the one next step for the outcome `select` just decided (SKILL.md Step 3)."""
    print("action: " + text)


def _plus_margin(when):
    """ISO text for `when` (datetime or ISO string) plus the resume margin; None when unknown or unparseable."""
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return None
    return (when + RESUME_MARGIN).isoformat(timespec="seconds") if when else None


def _gate_cause(reasons):
    """Why a `select` exit `3` dispatched nothing: `low` (--fail-on-low), `ask` (emergency or pay-per-use under `ask`), or `excluded`."""
    return "low" if reasons["threshold"] > 0 else "ask" if reasons["askable"] > 0 else "excluded"


def _stop_action_text(rc, resume_iso, who="", cause="", notes=(), recorded=True):
    """`action:` text for a `select` exit that dispatches nothing (2, 3 or 4); `who` names a held step such as the review."""
    if rc == 2:
        research = any("research-unrated" in n for n in notes)
        return "no tier match: " + ("run references/research-unrated.md or ask the user" if research else "ask the user")
    if rc == 4:
        fu_msg = (
            "follow-up in place: schedule its wakeup"
            if recorded == "in_place"
            else "follow-up recorded: schedule its wakeup"
            if recorded
            else "no follow-up was recorded (no --ref): add one with followups --add --kind resume"
        )
        return f"hold {who + ' ' if who else ''}until {resume_iso or 'the earliest reset (time unknown)'}; {fu_msg}"
    if cause == "low":
        return (
            "rerun select without --fail-on-low and dispatch its " + ("reviewer" if who else "candidate")
            + " (hold only if the user asked to preserve this pool)"
        )
    if cause == "ask":
        return "ask the user once (only emergency or pay-per-use models remain, and spending them needs approval)"
    return "no usable model: report to the user (pay-per-use under `never` and `policy: avoid` stay excluded)"


def _stop_action(rc, resume_iso, who="", cause="", notes=(), recorded=True):
    """Print the `action:` line for a `select` exit that dispatches nothing (2, 3 or 4)."""
    _action(_stop_action_text(rc, resume_iso, who=who, cause=cause, notes=notes, recorded=recorded))


def reviewer_tier(tier, risk):
    return tier if risk != "high" else "T" + str(min(4, int(tier[1]) + 1))


def pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, tier_only=False):
    """Return `(stage, candidate, below_tier)` for the first usable reviewer, or None.

    Capability before independence (references/review.md): different family meeting `rt` ->
    same family, different model, meeting `rt` -> different family below `rt` (best score) ->
    same family, different model, below `rt` (best score) -> same model.
    """
    meet, _, _ = candidates(catalog, assets, avail, floors[rt], floors, now, fail_on_low=fail_on_low)
    anyc, _, _ = candidates(
        catalog,
        assets,
        avail,
        0,
        floors,
        now,
        fail_on_low=fail_on_low,
        pick_top=True,
        task_floor=floors[rt],
    )
    meet.sort(key=lambda c: c["key"])
    anyc.sort(key=lambda c: (-c["score"], c["key"]))
    fam, mid = impl_model["family"], impl_model["id"]
    diff = lambda c: c["model"]["family"] != fam
    same_fam = lambda c: c["model"]["family"] == fam and c["model"]["id"] != mid
    same_model = lambda c: c["model"]["id"] == mid
    order = (
        (1, meet, diff),
        (2, meet, same_fam),
        (3, anyc, diff),
        (4, anyc, same_fam),
        (5, meet, same_model),
        (5, anyc, same_model),
    )
    if tier_only:
        order = tuple(step for step in order if step[1] is meet)
    for stage, pool, pred in order:
        hit = [c for c in pool if pred(c)]
        if hit:
            return stage, hit[0], hit[0]["score"] < floors[rt]
    return None


def _reviewer_why(rt, pick, impl_model, floor):
    stage, c, below = pick
    if stage == 1:
        return None
    fam = impl_model["family"]
    reasons = {
        2: f"no different-family candidate meets {rt}; using same family {fam}, different model, fresh context",
        3: f"no different-family or same-family candidate meets {rt}; using the best different-family score",
        4: f"no different-family candidate usable; using same family {fam}, different model, fresh context",
        5: "no other model usable; using the implementer's model in a fresh conversation",
    }
    return reasons[stage] + (f" (below {rt}: score {c['score']} < {floor})" if below else "")


def _reviewer_lines(rt, pick, impl_model, floor, always=False):
    """Reviewer lines; the degradation note shows in compact mode only when the reviewer is the output (`always`)."""
    stage, c, below = pick
    labels = {
        1: "different family",
        2: "same family, different model, fresh context",
        3: "different family, below tier",
        4: "same family, different model, fresh context, below tier",
        5: "same model, fresh conversation",
    }
    lines = []
    if _FULL:
        lines.append(f"reviewer ({rt}, {labels[stage]}{', below tier' if below and stage == 5 else ''}): {_fmt_cand(c)}")
    why = _reviewer_why(rt, pick, impl_model, floor)
    if why and (always or _FULL):
        lines.append(f"[note] reviewer degraded: {why}. State this in the report.")
    return lines


def print_reviewer(rt, pick, impl_model, floor, always=False):
    """Print the reviewer lines and return the degradation reason (None for a different-family reviewer)."""
    for line in _reviewer_lines(rt, pick, impl_model, floor, always=always):
        print(line)
    return _reviewer_why(rt, pick, impl_model, floor)


def _resume_ref(ref_arg, assets_exists):
    return ref_arg or ("current-task" if assets_exists else None)


def _record_resume_lines(ap, ref, needs, after_iso, now, on_recovery=True, fail_on_low=False):
    """Record one `kind: resume` entry per ref; return `(status, line)`."""
    if not ref:
        return False, "[note] no assets file and no --ref; pass --ref to record the resume follow-up"
    entry = {"kind": "resume", "ref": ref, "needs": needs}
    if after_iso:
        entry["after"] = after_iso
    if on_recovery:
        entry["on_recovery"] = True
    if fail_on_low:
        entry["fail_on_low"] = True
    existing = find_existing_followup(ap, "resume", ref)
    after_txt = f", after {after_iso}" if after_iso else ""
    if existing and dict(existing) == entry:
        return "in_place", f"followup already scheduled: [resume] {ref} (needs {needs}{after_txt})"
    record_followup(ap, entry, now=now)
    if existing:
        return "in_place", f"followup updated: [resume] {ref} (needs {needs}{after_txt}); reschedule its wakeup"
    return "recorded", f"followup recorded: [resume] {ref} (needs {needs}{after_txt})"


def _record_resume(ap, ref, needs, after_iso, now, on_recovery=True, fail_on_low=False):
    """Record one `kind: resume` entry per ref; refresh it when its fields changed."""
    status, line = _record_resume_lines(
        ap, ref, needs, after_iso, now, on_recovery=on_recovery, fail_on_low=fail_on_low
    )
    print(line)
    return status


def _schedule_resume_line(label, earliest_dt):
    """Return `(resume_iso, optional_detail_line)` for SCHEDULE_RESUME."""
    if not earliest_dt:
        return None, (f"SCHEDULE_RESUME: all usable {label} exhausted (reset time unknown)." if _FULL else None)
    earliest_iso = earliest_dt.isoformat(timespec="seconds")
    resume_iso = (earliest_dt + RESUME_MARGIN).isoformat(timespec="seconds")
    line = (
        f"SCHEDULE_RESUME: all usable {label} exhausted (earliest reset: {earliest_iso}; schedule resume at {resume_iso})."
        if _FULL
        else None
    )
    return resume_iso, line


def _schedule_resume(label, earliest_dt):
    """Print the SCHEDULE_RESUME line; return the resume time (earliest reset + margin) or None."""
    resume_iso, line = _schedule_resume_line(label, earliest_dt)
    if line:
        print(line)
    return resume_iso


def choose_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, risk, below_tier_mode, impl_below_tier=False):
    """Return `(pick, wait)`; `wait` is `(earliest_reset, needs)` when a high-risk `stage` or provisional `proceed` review waits for `rt`."""
    if risk == "high" and (below_tier_mode == "stage" or (below_tier_mode == "proceed" and impl_below_tier)):
        pick = pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, tier_only=True)
        if pick:
            return pick, None
        saw, earliest_dt, needs, _ = exhausted_tier_candidates(catalog, assets, avail, rt, floors[rt], now)
        if saw:
            return None, (earliest_dt, needs)
    return pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low), None


def _show_entry(pair):
    """An --available entry on one output line: control characters escaped."""
    return pair if pair.isprintable() else repr(pair)


def bad_available_entries(pairs, catalog=None):
    """`--available` entries that are not one `provider:model` pair: no provider, an empty part, a `;` or control character,
    or a second pair or catalog model joined by spaces. Display names with spaces (`antigravity:Orchestrator (Neon)`) pass."""
    known = set(_catalog_name_map(catalog)) if catalog else set()
    bad = []
    for pair in pairs:
        prov, sep, mid = pair.partition(":")
        mid = mid.strip()
        extra = mid.split()[1:]
        joined = any(":" in t or norm(t) in known for t in extra)
        if not sep or not prov.strip() or not mid or ";" in pair or not pair.isprintable() or joined:
            bad.append(pair)
    return bad


def resolve_implementer(catalog, impl_id):
    """Catalog model for `--implementer`: a model id, name, or alias, optionally prefixed `provider:` or `provider/`
    and suffixed `@ effort` as select prints it. Longer candidates win, so a slash model id beats its last segment."""
    names = _catalog_name_map(catalog)
    by_id = {norm(x["id"]): x for x in catalog["models"]}
    base = re.sub(r"\s*@\s*\S+$", "", impl_id.strip())
    tries = [base] + [base[i + 1:] for i, ch in enumerate(base) if ch in ":/"]
    for t in tries:
        mid = names.get(norm(t))
        if mid and norm(mid) in by_id:
            return by_id[norm(mid)]
    return None


def _eval_select_reviewer(
    a, catalog, assets, avail, floors, tier, impl_id, now, ap=None, below_tier_mode="stage", assets_exists=False, record_followups=True
):
    """Evaluate the reviewer step without printing; return structured result dict."""
    lines = []
    detail = lambda s: lines.append(s) if _FULL else None
    m = resolve_implementer(catalog, impl_id)
    if not m:
        detail(f"implementer {impl_id}: not in catalog")
        return {
            "rc": 1,
            "kind": "error",
            "lines": lines,
            "action": f"fix --implementer ({_show_entry(impl_id)} is not in the catalog; pass the implementing <model-id> as discovery lists it) and rerun select; dispatch only the reviewer its action line names",
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    rt = reviewer_tier(tier, a.risk)
    fail_on_low = bool(a.fail_on_low)
    ref = _resume_ref(_opt_str(a, "ref"), assets_exists)
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    impl_below_tier = best_effort(m, floors[tier], emerg) is None
    detail(f"tier {tier} (>= {floors[tier]}); reviewer tier {rt} (>= {floors[rt]})")
    detail(f"implementer (given): {m['family']} / {m['id']}")
    pick, wait = choose_reviewer(
        catalog, assets, avail, floors, rt, now, m, fail_on_low, a.risk, below_tier_mode, impl_below_tier=impl_below_tier
    )
    if pick:
        lines.extend(_reviewer_lines(rt, pick, m, floors[rt], always=True))
        act = (
            f"dispatch reviewer {_cand_ref(pick[1])} now"
            + ("" if pick[0] == 1 else "; state the reviewer degradation in the report")
        )
        fu_suffix = ""
        if a.risk == "high" and pick[0] != 1 and record_followups:  # queue the cross-family review this pick could not give
            needs = f"!{m['family']}/{rt}"
            if ref:
                record_followup(ap, {"kind": "preferred-review", "ref": ref, "needs": needs}, now=now)
                lines.append(f"followup recorded: [preferred-review] {ref} (needs {needs})")
                fu_suffix = "; a cross-family re-review follow-up was recorded"
            else:
                fu_suffix = ("; no assets file and no --ref, so no follow-up was recorded: queue the cross-family re-review "
                             f"with followups --add --kind preferred-review --ref <item> --needs '{needs}'")
        act += fu_suffix
        return {
            "rc": 0,
            "kind": "dispatch_reviewer",
            "lines": lines,
            "action": act,
            "followup_suffix": fu_suffix,
            "candidate": pick[1],
            "provider": pick[1]["provider"],
            "reviewer_stage": pick[0],
            "provisional": False,
            "resume_iso": None,
        }
    if wait:
        earliest_dt, needs = wait
        detail(
            f"NO REVIEWER: high-risk work under below_tier={below_tier_mode} takes a reviewer meeting {rt} (>= {floors[rt]}); "
            "wait for one to renew"
        )
        resume_iso, sched_line = _schedule_resume_line(f"{rt} reviewers", earliest_dt)
        if sched_line:
            lines.append(sched_line)
        rec = False
        if record_followups:
            rec, rline = _record_resume_lines(ap, ref, needs, resume_iso, now, fail_on_low=fail_on_low)
            lines.append(rline)
        return {
            "rc": 4,
            "kind": "hold",
            "lines": lines,
            "action": _stop_action_text(4, resume_iso, "the review", recorded=rec),
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": resume_iso,
        }
    _, notes, reasons = candidates(
        catalog,
        assets,
        avail,
        0,
        floors,
        now,
        fail_on_low=fail_on_low,
        pick_top=True,
        task_floor=floors[rt],
    )
    detail("NO REVIEWER: no non-excluded model remains (quota recovery order in references/harnesses.md)")
    lines.extend(_note_lines(notes))
    if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["exhausted"] == 0):
        cause = _gate_cause(reasons)
        return {
            "rc": 3,
            "kind": "ask" if cause == "ask" else "stop",
            "lines": lines,
            "action": _stop_action_text(3, None, "the review", cause),
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    if reasons["exhausted"] > 0:
        saw, earliest_dt, needs, _ = exhausted_tier_candidates(catalog, assets, avail, rt, floors[rt], now)
        label = f"{rt} reviewers"
        if not saw:
            _, earliest_dt, _, _ = exhausted_tier_candidates(catalog, assets, avail, "any", 0, now)
            needs, label = "any", "reviewers"
        resume_iso, sched_line = _schedule_resume_line(label, earliest_dt)
        if sched_line:
            lines.append(sched_line)
        rec = False
        if record_followups:
            rec, rline = _record_resume_lines(ap, ref, needs, resume_iso, now, fail_on_low=fail_on_low)
            lines.append(rline)
        return {
            "rc": 4,
            "kind": "hold",
            "lines": lines,
            "action": _stop_action_text(4, resume_iso, "the review", recorded=rec),
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": resume_iso,
        }
    return {
        "rc": 2,
        "kind": "stop",
        "lines": lines,
        "action": _stop_action_text(2, None, notes=notes),
        "candidate": None,
        "provider": None,
        "reviewer_stage": None,
        "provisional": False,
        "resume_iso": None,
    }


def _parse_pme(val, allow_slash=False):
    """Parse `provider:model@effort` (and `provider/model @ effort` if `allow_slash`) -> `(provider, model, effort)` or None."""
    if not isinstance(val, str) or not val.strip() or not val.isprintable() or ";" in val:
        return None
    pair, sep, eff = val.strip().rpartition("@")
    if not sep:
        return None
    pair = pair.strip()
    if ":" in pair:
        prov, _, mid = pair.partition(":")
    elif allow_slash and "/" in pair:
        prov, _, mid = pair.partition("/")
    else:
        return None
    prov, mid, eff = prov.strip(), mid.strip(), eff.strip().lower()
    all_efforts = set(EFFORT_ORDER) | {"max", "ultra"}
    if not prov or not mid or not eff or any(c.isspace() for c in prov) or eff not in all_efforts:
        return None
    return prov, mid, eff


def _canon_pme(val, catalog, key=False):
    """Parse `--requested`/`--effective` to `provider:model-id@effort` or None. The provider id stays verbatim (it must match
    assets.yaml and the harness); `key=True` normalises it for comparing and grouping rows (old lowercase and verbatim alike)."""
    parsed = _parse_pme(val, allow_slash=True)
    if not parsed:
        return None
    prov, mid, eff = parsed
    names = _catalog_name_map(catalog)
    canon_mid = norm(names.get(norm(mid), mid))
    return f"{norm(prov) if key else prov}:{canon_mid}@{eff}"


def _parse_rubric(val):
    """Parse `size=N,uncertainty=N,deps=N,consequences=N,verification=N,coordination=N` -> dict or None."""
    if not isinstance(val, str) or not val.strip():
        return None
    found = {}
    for part in val.split(","):
        k, sep, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or k not in RUBRIC_KEYS or k in found or v not in ("0", "1", "2"):
            return None
        found[k] = int(v)
    if len(found) != len(RUBRIC_KEYS):
        return None
    return {k: found[k] for k in RUBRIC_KEYS}


def _parse_orch_tier_key(k):
    if isinstance(k, bool):
        return None
    if isinstance(k, int) and k in (0, 1, 2):
        return k
    if isinstance(k, str) and k.strip() in ("0", "1", "2"):
        return int(k.strip())
    return None


def _orchestrator_prefs(assets):
    """Return `(cfg, notes)` for `preferences.orchestrator` with safe defaults and clamping."""
    prefs = (assets or {}).get("preferences") or {}
    raw = prefs.get("orchestrator") if isinstance(prefs, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    notes, tiers, defaults = [], dict(ORCH_TIERS_DEFAULT), {}
    if isinstance(raw.get("tiers"), dict):
        for k, v in raw["tiers"].items():
            ik = _parse_orch_tier_key(k)
            if ik is not None and isinstance(v, str) and v in FLOORS:
                tiers[ik] = v
    max_effort = "high"
    if raw.get("max_effort") is not None:
        if isinstance(raw["max_effort"], str) and raw["max_effort"] in ORCH_EFFORTS:
            max_effort = raw["max_effort"]
        else:
            notes.append(f"preferences.orchestrator.max_effort: value {raw['max_effort']!r} invalid or above high -> capped at high")
    if isinstance(raw.get("defaults"), dict):
        for k, v in raw["defaults"].items():
            if not isinstance(k, str) or not k.strip():
                continue
            if not isinstance(v, str) or v not in ORCH_EFFORTS:
                notes.append(f"preferences.orchestrator.defaults[{k}]: invalid effort {v!r} -> ignored")
            elif EFFORT_ORDER[v] > EFFORT_ORDER[max_effort]:
                notes.append(f"preferences.orchestrator.defaults[{k}]: {v!r} exceeds max_effort {max_effort} -> capped at {max_effort}")
                defaults[k.strip()] = max_effort
            else:
                defaults[k.strip()] = v
    esc = raw.get("max_escalations")
    max_escalations = esc if isinstance(esc, int) and not isinstance(esc, bool) and 0 <= esc <= 3 else 2
    lad = raw.get("ladder")
    ladder = list(lad) if isinstance(lad, list) and lad else "auto"
    unf = raw.get("unflagged_findings")
    return {
        "tiers": tiers, "max_effort": max_effort, "defaults": defaults, "max_escalations": max_escalations, "ladder": ladder,
        "unflagged_findings": unf if _is_int(unf) and unf >= 1 else 3,
        "phase_effort": "on" if raw.get("phase_effort") == "on" else "off",
    }, notes


_is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731


def _escalation_prefs(assets):
    """`preferences.escalation` for workers with defaults: `findings` (trigger threshold, >= 1) and `max` (cap, 0..3)."""
    prefs = (assets or {}).get("preferences") or {}
    raw = prefs.get("escalation") if isinstance(prefs, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    f, m = raw.get("findings"), raw.get("max")
    return {"findings": f if _is_int(f) and f >= 1 else 3, "max": m if _is_int(m) and 0 <= m <= 3 else 2}


def _classify_rubric(rubric, assets=None):
    """Pure rubric math shared by `classify` and `launch`: `(complexity, risk, orch_tier, total, rubric_str)`."""
    oprefs, _ = _orchestrator_prefs(assets)
    total = rubric["size"] + rubric["uncertainty"] + rubric["deps"] + rubric["verification"]
    complexity = "trivial" if total <= 1 else "standard" if total <= 3 else "hard" if total <= 6 else "frontier"
    risk = ("low", "medium", "high")[rubric["consequences"]]
    orch_tier = oprefs["tiers"][rubric["coordination"]]
    rubric_str = ",".join(f"{k}={rubric[k]}" for k in RUBRIC_KEYS)
    return complexity, risk, orch_tier, total, rubric_str


def classify_cmd(a):
    _begin(a)
    ap = _opt_str(a, "assets") or def_assets()
    assets = load_file(ap) if os.path.exists(ap) else None
    rubric = {k: getattr(a, k) for k in RUBRIC_KEYS}
    complexity, risk, orch_tier, total, rubric_str = _classify_rubric(rubric, assets)
    print(f"complexity: {complexity} (size {a.size} + uncertainty {a.uncertainty} + deps {a.deps} + verification {a.verification} = {total})")
    print(f"risk: {risk} (consequences {a.consequences})")
    print(f"orchestrator: {orch_tier} (coordination {a.coordination})")
    print(f"rubric: {rubric_str}")
    _action(
        f"select --complexity {complexity} --risk {risk} for workers and "
        f"select --role orchestrator --coordination {a.coordination} for the orchestrator; "
        "pass --rubric <rubric> to log"
    )
    return 0


def _eval_explicit_ladder(ladder_items, catalog, assets, avail, tier, floor, floors, max_effort, now, fail_on_low=False):
    """Filter explicit `provider:model@effort` ladder entries via `candidates()` plus effort/floor checks."""
    names = _catalog_name_map(catalog)
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    avail_map = {}
    for pair in avail:
        aprov, _, amid = pair.partition(":")
        canon = names.get(norm(amid), amid)
        avail_map[(aprov, norm(amid))] = avail_map[(aprov, norm(canon))] = pair

    usable, cnotes, _ = candidates(
        catalog, assets, avail, 0, floors, now,
        fail_on_low=fail_on_low, pick_top=True, max_effort=max_effort, exclude_coordinator=True,
    )
    by_pair = {c["pair"]: c for c in usable}
    reason_by_pair = {}
    for n in cnotes:
        p, sep, r = n.partition(": ")
        if sep and p not in reason_by_pair:
            reason_by_pair[p] = r

    surviving, notes = [], []
    for item in ladder_items:
        parsed = _parse_pme(item)
        if not parsed:
            notes.append(f"{item}: invalid provider:model@effort entry")
            continue
        prov, mid, eff = parsed
        pair = avail_map.get((prov, norm(mid))) or avail_map.get((prov, norm(names.get(norm(mid), mid))))
        if not pair:
            notes.append(f"{item}: not in --available")
            continue
        c = by_pair.get(pair)
        if not c:
            notes.append(f"{item}: {reason_by_pair.get(pair, 'not usable')}")
            continue
        if eff in emerg or EFFORT_ORDER.get(eff, 99) > EFFORT_ORDER[max_effort]:
            notes.append(f"{item}: effort {eff} exceeds max_effort {max_effort}")
            continue
        sc = (c["model"].get("scores") or {}).get(eff)
        if not isinstance(sc, (int, float)) or isinstance(sc, bool):
            notes.append(f"{item}: effort {eff} is not rated")
            continue
        if sc < floor:
            notes.append(f"{item}: score {sc} below {tier} floor {floor}")
            continue
        surviving.append({**c, "effort": eff, "score": sc})
    return surviving, notes


def _compute_orchestrator_ladder(
    catalog, assets, avail, floors, coord, now, fail_on_low=False, ladder_override=None, max_escalations_override=None
):
    """Pure orchestrator ladder solver -> `(start_rung, same_rungs, switch_rung, notes, stop)`."""
    oprefs, pref_notes = _orchestrator_prefs(assets)
    if ladder_override is not None:
        oprefs = {**oprefs, "ladder": ladder_override}
    if max_escalations_override is not None:
        oprefs = {**oprefs, "max_escalations": max_escalations_override}
    tier, max_effort = oprefs["tiers"][coord], oprefs["max_effort"]
    floor = floors[tier]
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))

    same_rungs, switch_rung, extra_notes = [], None, list(pref_notes)
    if oprefs["ladder"] != "auto":
        surviving, exp_notes = _eval_explicit_ladder(
            oprefs["ladder"], catalog, assets, avail, tier, floor, floors, max_effort, now, fail_on_low=fail_on_low
        )
        extra_notes.extend(exp_notes)
        if surviving:
            start_prov = surviving[0]["provider"]
            same_rungs = [r for r in surviving if r["provider"] == start_prov][: oprefs["max_escalations"] + 1]
            switch_rung = next((r for r in surviving if r["provider"] != start_prov), None)
        else:
            extra_notes.append("explicit orchestrator ladder had no usable entry; falling back to auto")

    if not same_rungs:
        cands, notes, reasons = candidates(
            catalog, assets, avail, floor, floors, now,
            fail_on_low=fail_on_low, max_effort=max_effort, exclude_coordinator=True,
        )
        cands.sort(key=lambda c: c["key"])
        extra_notes.extend(notes)
        if not cands:
            saw_ex_tier, earliest_dt, resume_needs, ex_cands = exhausted_tier_candidates(
                catalog, assets, avail, tier, floor, now, max_effort=max_effort, exclude_coordinator=True,
            )
            if saw_ex_tier and reasons["threshold"] == 0:
                if not resume_needs.lower().startswith("provider:") and ex_cands:
                    resume_needs = f"provider:{ex_cands[0]['provider']}/{ex_cands[0]['model']['id']}"
                return None, [], None, extra_notes, {"rc": 4, "tier": tier, "floor": floor, "earliest_dt": earliest_dt, "resume_needs": resume_needs, "cause": ""}
            if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["tier"] == 0 and reasons["exhausted"] == 0):
                return None, [], None, extra_notes, {"rc": 3, "tier": tier, "floor": floor, "earliest_dt": None, "resume_needs": None, "cause": _gate_cause(reasons)}
            return None, [], None, extra_notes, {"rc": 2, "tier": tier, "floor": floor, "earliest_dt": None, "resume_needs": None, "cause": ""}

        start_c = cands[0]
        start_prov, start_e, start_sc = start_c["provider"], start_c["effort"], start_c["score"]
        rated_start = _rated_efforts(start_c["model"], emerg, max_effort=max_effort)
        rated_map = dict(rated_start)
        def_e = oprefs["defaults"].get(start_prov) or next((v for k, v in oprefs["defaults"].items() if norm(k) == norm(start_prov)), None)
        if def_e and def_e in rated_map and EFFORT_ORDER[def_e] > EFFORT_ORDER[start_e] and rated_map[def_e] >= floor:
            start_e, start_sc = def_e, rated_map[def_e]

        same_rungs, cur_max = [{**start_c, "effort": start_e, "score": start_sc}], start_sc
        for e, sc in rated_start:
            if EFFORT_ORDER[e] > EFFORT_ORDER[start_e] and sc > cur_max:
                same_rungs.append({**start_c, "effort": e, "score": sc})
                cur_max = sc
        for c in cands[1:]:
            if c["provider"] != start_prov or c["pair"] == start_c["pair"]:
                continue
            for e, sc in _rated_efforts(c["model"], emerg, max_effort=max_effort):
                if sc > cur_max:
                    same_rungs.append({**c, "effort": e, "score": sc})
                    cur_max = sc
        same_rungs = same_rungs[: oprefs["max_escalations"] + 1]

        ladder_top = max(r["score"] for r in same_rungs)
        others = [c for c in cands if c["provider"] != start_prov]
        for c in others:
            hit = next(((e, sc) for e, sc in _rated_efforts(c["model"], emerg, max_effort=max_effort) if sc > ladder_top), None)
            if hit:
                switch_rung = {**c, "effort": hit[0], "score": hit[1]}
                break
        if not switch_rung and others:
            scored = [(te[1], -idx, c, te[0]) for idx, c in enumerate(others) for te in [top_effort(c["model"], emerg, max_effort=max_effort)] if te]
            if scored:
                sc, _, c, e = max(scored)
                switch_rung = {**c, "effort": e, "score": sc}
                extra_notes.append(f"provider switch is not an escalation (score {sc} <= ladder top {ladder_top})")

    return same_rungs[0], same_rungs, switch_rung, extra_notes, {"rc": 0, "tier": tier, "floor": floor, "max_escalations": oprefs["max_escalations"]}


def _orch_action_suffix(switch_rung, name_rung=False):
    """Suffixes of select's orchestrator `action:` line, reused by `defer` refusals and `exhaust --self`; those pass
    `name_rung` so the clause names the phase-boundary provider-switch rung and is not read as a gate on the self-switch."""
    if switch_rung and name_rung:
        sw = f"; ask the user before the phase-boundary provider switch to {_cand_ref(switch_rung)}"
    else:
        sw = "; ask the user before the provider switch" if switch_rung else ""
    return sw + "; log it with --role orchestrator --rubric <rubric>"


def _eval_select_orchestrator(a, catalog, assets, avail, floors, now, ap=None, assets_exists=False, record_followups=True):
    """Evaluate orchestrator effort/model ladder and optional cross-provider switch without printing."""
    lines = []
    detail = lambda s: lines.append(s) if _FULL else None

    def _stop(rc, kind, action, resume_iso=None):
        return {
            "rc": rc,
            "kind": kind,
            "lines": lines,
            "action": action,
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": resume_iso,
        }

    if _opt_str(a, "implementer"):
        lines.append("error: --implementer cannot be used with --role orchestrator")
        return _stop(1, "error", "fix select arguments (drop --implementer for --role orchestrator) and rerun select")
    coord = _opt_int(a, "coordination")
    if coord is None or coord not in (0, 1, 2):
        lines.append("error: --coordination 0|1|2 is required for --role orchestrator")
        return _stop(1, "error", "fix --coordination (0|1|2 required for --role orchestrator) and rerun select")
    phase, cur_raw = _opt_str(a, "phase"), _opt_str(a, "current")
    cur = _parse_pme(cur_raw, allow_slash=True) if cur_raw else None
    if bool(phase) != bool(cur_raw) or (cur_raw and not cur):
        lines.append("error: --phase plan|review|supervise and --current provider:model@effort go together for --role orchestrator")
        return _stop(1, "error", "fix --phase/--current (pass both; --current is the orchestrator's base rung) and rerun select")
    fail_on_low, ref_arg = bool(a.fail_on_low), _opt_str(a, "ref")
    ignored = [
        flag for flag, present in (
            ("--complexity", bool(_opt_str(a, "complexity"))),
            ("--risk", bool(_opt_str(a, "risk"))),
            ("--ui", _opt_bool(a, "ui")),
            ("--prefer-family", bool(_opt_str(a, "prefer_family"))),
            ("--fallback", bool(_opt_str(a, "fallback"))),
            ("--below-tier", bool(_opt_str(a, "below_tier"))),
        ) if present
    ]
    ignored_line = f"note: ignored for --role orchestrator: {', '.join(ignored)}" if ignored else None

    _, same_rungs, switch_rung, extra_notes, info = _compute_orchestrator_ladder(
        catalog, assets, avail, floors, coord, now, fail_on_low=fail_on_low
    )
    tier, floor, stop_rc = info["tier"], info["floor"], info["rc"]
    if stop_rc != 0:
        if ignored_line:
            lines.append(ignored_line)
        if _FULL:
            lines.append("NO CANDIDATE:" + "".join("\n  " + n for n in extra_notes))
        else:
            lines.extend(_note_lines(extra_notes))
        if stop_rc == 4:
            sched_iso, sched_line = _schedule_resume_line(f"orchestrator {tier} candidates", info["earliest_dt"])
            if sched_line:
                lines.append(sched_line)
            rec = False
            if record_followups:
                rec, rline = _record_resume_lines(
                    ap, _resume_ref(ref_arg, assets_exists), info["resume_needs"], sched_iso, now, fail_on_low=fail_on_low
                )
                lines.append(rline)
            return _stop(4, "hold", _stop_action_text(4, sched_iso, recorded=rec), resume_iso=sched_iso)
        if stop_rc == 3:
            cause = info["cause"]
            return _stop(3, "ask" if cause == "ask" else "stop", _stop_action_text(3, None, "", cause))
        return _stop(2, "stop", _stop_action_text(2, None, notes=extra_notes))

    lines.append(f"orchestrator tier {tier} (>= {floor}; coordination {coord})")
    lines.append("ladder: " + " -> ".join(_cand_ref(r) for r in same_rungs))
    if switch_rung:
        lines.append(f"provider switch (phase boundary, ask the user first): {_cand_ref(switch_rung)}")
    if _FULL:
        for r in same_rungs:
            detail(f"  rung: {_fmt_cand(r)}")
        if switch_rung:
            detail(f"  switch: {_fmt_cand(switch_rung)}")
    if ignored_line:
        lines.append(ignored_line)
    lines.extend(_note_lines(extra_notes))
    phase_act = None
    if phase:
        line, phase_act = _phase_effort(catalog, _orchestrator_prefs(assets)[0], phase, cur)
        lines.append(line)
    act = phase_act or (
        f"launch the orchestrator on {_cand_ref(same_rungs[0])}; "
        "climb the ladder only on an escalation trigger (references/escalation.md)"
        + _orch_action_suffix(switch_rung)
    )
    return {
        "rc": 0,
        "kind": "dispatch_orchestrator",
        "lines": lines,
        "action": act,
        "candidate": same_rungs[0],
        "same_rungs": same_rungs,
        "switch_rung": switch_rung,
        "tier": tier,
        "provider": same_rungs[0]["provider"],
        "reviewer_stage": None,
        "provisional": False,
        "resume_iso": None,
    }


def _rung_model(catalog, mid):
    """Catalog model for a model id, name, or alias, or None."""
    canon = norm(_catalog_name_map(catalog).get(norm(mid), mid))
    return next((m for m in catalog.get("models") or [] if norm(m.get("id") or "") == canon), None)


def _phase_effort(catalog, oprefs, phase, cur):
    """`(line, action)` for the orchestrator's effort at a phase boundary: plan keeps the base rung `cur`, review takes the
    next higher rated effort (<= max_effort), supervise the next lower one (never below low); same model and provider.
    `action` is None while `preferences.orchestrator.phase_effort` is off (the line is then a note)."""
    prov, mid, eff = cur
    m = _rung_model(catalog, mid)
    here = f"{prov}/{m['id'] if m else mid} @ {eff}"
    if oprefs["phase_effort"] != "on":
        return f"note: phase effort is off (preferences.orchestrator.phase_effort); stay on {here}", None
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    rated = _rated_efforts(m, emerg, max_effort=oprefs["max_effort"]) if m else []
    rank, cur_sc = EFFORT_ORDER.get(eff, 99), _rung_score(m, eff)
    new = eff
    if phase == "review":
        new = next((e for e, v in rated if EFFORT_ORDER.get(e, 99) > rank and (cur_sc is None or v > cur_sc)), eff)
    elif phase == "supervise":
        new = next((e for e, _ in reversed(rated) if EFFORT_ORDER["low"] <= EFFORT_ORDER.get(e, 99) < rank), eff)
    to = f"{prov}/{m['id'] if m else mid} @ {new}"
    return f"phase effort: {phase} -> {to}", (f"at this phase boundary switch the orchestrator in place to {to}" if new != eff else f"stay on {here}")


def select_orchestrator(a, catalog, assets, avail, floors, now, ap=None, assets_exists=False):
    """Select an orchestrator effort/model ladder and optional cross-provider switch."""
    res = _eval_select_orchestrator(a, catalog, assets, avail, floors, now, ap=ap, assets_exists=assets_exists)
    for line in res["lines"]:
        print(line)
    _action(res["action"])
    return res["rc"]


def select_reviewer_only(a, catalog, assets, avail, floors, tier, impl_id, now, ap=None, below_tier_mode="stage", assets_exists=False):
    """Reviewer step: the implementer already ran, so rank the reviewer against the model that did."""
    res = _eval_select_reviewer(
        a, catalog, assets, avail, floors, tier, impl_id, now, ap=ap, below_tier_mode=below_tier_mode, assets_exists=assets_exists
    )
    for line in res["lines"]:
        print(line)
    _action(res["action"])
    return res["rc"]


def _eval_select(a, record_followups=True):
    """Run select's exact filtering, ranking, fallback, and reviewer logic without printing."""
    lines = []
    detail = lambda s: lines.append(s) if _FULL else None
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    catalog = load_catalog_with_local(DEF_CATALOG if not _opt_str(a, "catalog") else _opt_str(a, "catalog"), ap)
    assets_exists = os.path.exists(ap)
    assets = load_file(ap) if assets_exists else None
    if assets is None:
        detail(f"[note] no assets file at {ap}: defaults (all providers ok, policy normal)")
    prefs = (assets or {}).get("preferences") or {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    avail = [x.strip() for x in a.available.split(",") if x.strip()]
    bad = bad_available_entries(avail, catalog)
    if bad:
        lines.append("note: --available entries are not provider:model: " + ", ".join(_capped([_show_entry(x) for x in bad])))
        return {
            "rc": 1,
            "kind": "error",
            "lines": lines,
            "action": "fix --available (comma-separated <provider>:<model-id> pairs as discovery lists them) and rerun select; dispatch only the model its action line names",
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    raw_role = _opt_str(a, "role")
    impl_id = _opt_str(a, "implementer")
    if raw_role == "orchestrator":
        res = _eval_select_orchestrator(
            a, catalog, assets, avail, floors, now, ap=ap, assets_exists=assets_exists, record_followups=record_followups
        )
        res["lines"] = lines + res["lines"]
        return res
    if raw_role == "implementer" and impl_id:
        lines.append("error: --implementer cannot be used with --role implementer")
        return {
            "rc": 1,
            "kind": "error",
            "lines": lines,
            "action": "fix select arguments (use --role reviewer with --implementer, or drop --implementer) and rerun select",
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    if raw_role == "reviewer" and not impl_id:
        lines.append("error: --implementer is required for --role reviewer")
        return {
            "rc": 1,
            "kind": "error",
            "lines": lines,
            "action": "fix --implementer (required for --role reviewer) and rerun select",
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    eff_role = raw_role or ("reviewer" if impl_id else "implementer")
    complexity = _opt_str(a, "complexity")
    risk = _opt_str(a, "risk")
    if not complexity or not risk:
        lines.append(f"error: --complexity and --risk are required for --role {eff_role}")
        return {
            "rc": 1,
            "kind": "error",
            "lines": lines,
            "action": f"fix --complexity/--risk (required for --role {eff_role}) and rerun select",
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }
    tier = TIER[complexity][risk]
    prefer_family = _opt_str(a, "prefer_family")
    fallback_mode = _opt_str(a, "fallback") or prefs.get("ui_fallback") or "proceed"
    below_tier_mode = _opt_str(a, "below_tier") or prefs.get("below_tier") or "stage"
    ref_arg = _opt_str(a, "ref")
    ignored = [f for f, on in (("--coordination", _opt_int(a, "coordination") is not None), ("--phase", bool(_opt_str(a, "phase"))),
                               ("--current", bool(_opt_str(a, "current")))) if on]
    if ignored:
        lines.append(f"note: ignored for --role {eff_role}: {', '.join(ignored)}")

    if impl_id:
        res = _eval_select_reviewer(
            a,
            catalog,
            assets,
            avail,
            floors,
            tier,
            impl_id,
            now,
            ap=ap,
            below_tier_mode=below_tier_mode,
            assets_exists=assets_exists,
            record_followups=record_followups,
        )
        res["lines"] = lines + res["lines"]
        return res

    cands, notes, reasons = candidates(
        catalog,
        assets,
        avail,
        floors[tier],
        floors,
        now,
        ui_primary=bool(a.ui),
        prefer_family=prefer_family,
        fail_on_low=bool(a.fail_on_low),
    )
    cands.sort(key=lambda c: c["key"])
    detail(f"tier {tier} (>= {floors[tier]})")
    if not cands:
        below_cands, _, _ = candidates(
            catalog,
            assets,
            avail,
            0,
            floors,
            now,
            ui_primary=bool(a.ui),
            prefer_family=prefer_family,
            fail_on_low=bool(a.fail_on_low),
            pick_top=True,
            task_floor=floors[tier],
        )
        below_cands.sort(key=lambda c: (-c["score"], c["key"]))
        saw_ex_tier, earliest_dt, resume_needs, ex_cands = exhausted_tier_candidates(
            catalog,
            assets,
            avail,
            tier,
            floors[tier],
            now,
            ui_primary=bool(a.ui),
            prefer_family=prefer_family,
        )
        fmt = _fmt_cand
        rt = reviewer_tier(tier, a.risk)

        # Staging/proceeding below tier only makes sense while a tier-meeting model waits on quota renewal.
        can_stage = bool(below_cands) and saw_ex_tier
        ref_val = _resume_ref(ref_arg, assets_exists)
        earliest_iso = earliest_dt.isoformat(timespec="seconds") if earliest_dt else None
        resume_iso = (earliest_dt + RESUME_MARGIN).isoformat(timespec="seconds") if earliest_dt else None
        if can_stage and below_tier_mode == "stage":
            impl = below_cands[0]
            target = ex_cands[0] if ex_cands else None
            reset_txt = earliest_iso or "unknown"
            resume_txt = resume_iso or "reset + 2m"
            resume_sched_txt = f"; schedule phase 2 resume at {resume_iso}" if resume_iso else ""
            target_txt = (
                f"{target['provider']} / {target['model']['id']} @ {target['effort']} "
                f"(score {target['score']}, policy {target['policy']})"
                if target
                else f"first usable {tier} (>= {floors[tier]}) model"
            )
            detail(
                f"STAGE (below_tier=stage): no usable candidate meets {tier} (>= {floors[tier]}); "
                f"starting phase 1 below tier (earliest {tier} reset: {reset_txt}{resume_sched_txt})."
            )
            detail(f"implementer (phase 1, low-risk only — read, plan, failing tests/red; below {tier}): {fmt(impl)}")
            detail(
                f"phase 2 (risky change, destructive steps, review; requires {tier} >= {floors[tier]}): "
                f"at the phase 1 boundary switch same child in place to {target_txt} immediately. "
                "Send one continue message only for a verified or observed (harness, provider) pair that resumes "
                f"own-provider quota stops (references/harnesses.md); otherwise wait for the scheduled resume at {resume_txt}, "
                "a double check that re-reads the child and acts only while it is idle. "
                f"A harness that cannot switch in place should defer the new Phase 2 child until {resume_txt}; "
                f"on a harness without a scheduler, report the reset time ({reset_txt}) to the user."
            )
            if record_followups:
                _, rline = _record_resume_lines(ap, ref_val, resume_needs, resume_iso, now, fail_on_low=bool(a.fail_on_low))
                lines.append(rline)
                if ref_val:
                    assets = load_file(ap)
            if a.risk == "high":
                detail(f"reviewer ({rt}): high-risk work under stage takes a reviewer meeting {rt} (>= {floors[rt]}); select it when the review step starts in phase 2")
            else:
                detail(f"reviewer ({rt}): select when the review step starts in phase 2 (fallback order)")
            lines.extend(_note_lines(notes))
            lines.extend(_due_lines(due_followups(catalog, assets, now, available=avail)))
            act = (
                f"stage: dispatch Phase 1 {_cand_ref(impl)} now; switch to "
                + (_cand_ref(target) if target else f"the first usable {tier} model")
                + f" at the Phase 1 boundary (earliest reset {reset_txt}), with a resume double check at {resume_iso or 'reset + 2m'}"
            )
            return {
                "rc": 0,
                "kind": "stage",
                "lines": lines,
                "action": act,
                "candidate": impl,
                "target": target,
                "tier": tier,
                "provider": impl["provider"],
                "reviewer_stage": None,
                "provisional": False,
                "resume_iso": resume_iso,
            }

        if can_stage and below_tier_mode == "proceed":
            impl = below_cands[0]
            reset_txt = earliest_iso or "unknown"
            detail(
                f"[provisional] (below_tier=proceed): no usable candidate meets {tier} (>= {floors[tier]}, "
                f"earliest reset: {reset_txt}); proceeding with best available below-tier model for all phases "
                f"— tier-meeting ({tier}) re-review follow-up required"
            )
            if record_followups:
                _, rline = _record_resume_lines(ap, ref_val, resume_needs, resume_iso, now, fail_on_low=bool(a.fail_on_low))
                lines.append(rline)
                if ref_val:
                    assets = load_file(ap)
            detail(f"implementer: {fmt(impl)}")
            pick, wait = choose_reviewer(
                catalog, assets, avail, floors, rt, now, impl["model"], bool(a.fail_on_low), a.risk, below_tier_mode, impl_below_tier=True
            )
            if pick:
                lines.extend(_reviewer_lines(rt, pick, impl["model"], floors[rt]))
            elif wait:
                detail(
                    f"reviewer ({rt}): high-risk work under {below_tier_mode} takes a reviewer meeting {rt} (>= {floors[rt]}); "
                    f"wait for one to renew (needs {wait[1]})"
                )
            lines.extend(_note_lines(notes))
            lines.extend(_due_lines(due_followups(catalog, assets, now, available=avail)))
            act = (
                f"dispatch {_cand_ref(impl)} now (below {tier}); choose the reviewer when its step starts; "
                "mark it provisional (log --provisional)"
            )
            return {
                "rc": 0,
                "kind": "dispatch",
                "lines": lines,
                "action": act,
                "candidate": impl,
                "provider": impl["provider"],
                "reviewer_stage": None,
                "provisional": True,
                "resume_iso": resume_iso,
            }

        if _FULL:
            lines.append("NO CANDIDATE:" + ("".join("\n  " + n for n in notes)))
        else:
            lines.extend(_note_lines(notes))
        if saw_ex_tier and reasons["threshold"] == 0:
            sched_iso, sched_line = _schedule_resume_line(f"{tier} candidates", earliest_dt)
            if sched_line:
                lines.append(sched_line)
            rec = False
            if record_followups:
                rec, rline = _record_resume_lines(ap, ref_val, resume_needs, sched_iso, now, fail_on_low=bool(a.fail_on_low))
                lines.append(rline)
            return {
                "rc": 4,
                "kind": "hold",
                "lines": lines,
                "action": _stop_action_text(4, sched_iso, recorded=rec),
                "candidate": None,
                "provider": None,
                "reviewer_stage": None,
                "provisional": False,
                "resume_iso": sched_iso,
            }
        if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["tier"] == 0 and reasons["exhausted"] == 0):
            cause = _gate_cause(reasons)
            return {
                "rc": 3,
                "kind": "ask" if cause == "ask" else "stop",
                "lines": lines,
                "action": _stop_action_text(3, None, "", cause),
                "candidate": None,
                "provider": None,
                "reviewer_stage": None,
                "provisional": False,
                "resume_iso": None,
            }
        return {
            "rc": 2,
            "kind": "stop",
            "lines": lines,
            "action": _stop_action_text(2, None, notes=notes),
            "candidate": None,
            "provider": None,
            "reviewer_stage": None,
            "provisional": False,
            "resume_iso": None,
        }

    impl = cands[0]
    fmt = _fmt_cand

    pref_need = None
    pref_kind = None
    provisional = False
    skipped_followup = False
    if a.ui and "ui-ux-review" not in (impl["model"].get("strengths") or []):
        pref_need = "ui-ux-review"
        pref_kind = "ui-review"
    elif prefer_family and not _model_matches_need(impl["model"], prefer_family):
        pref_need = prefer_family.strip().lower()
        pref_kind = "preferred-review"

    if pref_need:
        pref_state, earliest = classify_unmet_preference(
            catalog, assets, avail, pref_need, floors[tier], now, fail_on_low=bool(a.fail_on_low)
        )
        if pref_state == "not-offered":
            lines.append(
                f"[note] preferred {pref_need} not offered by this harness; "
                f"using best available model ({fmt(impl)})"
            )
        else:
            reset_txt = earliest or "unknown"
            if fallback_mode == "defer":
                detail(
                    f"DEFER: preferred {pref_need} exhausted (earliest reset: {reset_txt}). "
                    "Recommend waiting for reset (non-urgent work)."
                )
                detail(f"  fallback if urgent: {fmt(impl)}")
                lines.extend(_note_lines(notes))
                when = _plus_margin(earliest)
                return {
                    "rc": 3,
                    "kind": "hold",
                    "lines": lines,
                    "action": (
                        f"hold until {when or 'the preferred model renews (time unknown)'} "
                        f"(preferred {pref_need} exhausted); record a resume follow-up"
                    ),
                    "candidate": None,
                    "provider": None,
                    "reviewer_stage": None,
                    "provisional": False,
                    "resume_iso": when,
                    "pref_need": pref_need,
                }
            if fallback_mode == "ask":
                detail(
                    f"ASK: preferred {pref_need} exhausted (earliest reset: {reset_txt}). "
                    f"Ask the user once: proceed provisionally with {fmt(impl)} (and queue follow-up review) or wait for reset?"
                )
                lines.extend(_note_lines(notes))
                return {
                    "rc": 3,
                    "kind": "ask",
                    "lines": lines,
                    "action": (
                        f"ask the user once (preferred {pref_need} exhausted: proceed provisionally with "
                        f"{_cand_ref(impl)} and queue a follow-up review, or wait until {_plus_margin(earliest) or 'it renews'})"
                    ),
                    "candidate": None,
                    "provider": None,
                    "reviewer_stage": None,
                    "provisional": False,
                    "resume_iso": _plus_margin(earliest),
                }
            # proceed (default)
            provisional = True
            detail(
                f"[provisional] preferred {pref_need} exhausted (earliest reset: {reset_txt}); "
                "proceeding with fallback model — follow-up review required"
            )
            if assets_exists or ref_arg:
                if record_followups:
                    fu_entry = {
                        "kind": pref_kind,
                        "ref": ref_arg or "current-task",
                        "needs": pref_need,
                    }
                    if earliest:
                        fu_entry["after"] = earliest
                    record_followup(ap, fu_entry, now=now)
                    lines.append(f"followup recorded: [{fu_entry['kind']}] {fu_entry['ref']} (needs {pref_need}, after {reset_txt})")
                    assets = load_file(ap)
            else:
                skipped_followup = True

    detail(f"implementer: {fmt(impl)}")

    rt = reviewer_tier(tier, a.risk)
    pick, wait = choose_reviewer(
        catalog, assets, avail, floors, rt, now, impl["model"], bool(a.fail_on_low), a.risk, below_tier_mode
    )
    if pick:
        lines.extend(_reviewer_lines(rt, pick, impl["model"], floors[rt]))
    elif wait:
        detail(
            f"reviewer ({rt}): high-risk work under {below_tier_mode} takes a reviewer meeting {rt} (>= {floors[rt]}); "
            f"wait for one to renew (needs {wait[1]})"
        )
    lines.extend(_note_lines(notes))
    lines.extend(_due_lines(due_followups(catalog, assets, now, available=avail)))
    act = (
        f"dispatch {_cand_ref(impl)} now; choose the reviewer when its step starts"
        + ("; mark it provisional (log --provisional)" if provisional else "")
        + ("; no assets file, so no follow-up was recorded: queue the preferred-model review yourself" if skipped_followup else "")
    )
    return {
        "rc": 0,
        "kind": "dispatch",
        "lines": lines,
        "action": act,
        "candidate": impl,
        "provider": impl["provider"],
        "reviewer_stage": None,
        "provisional": provisional,
        "resume_iso": None,
        "pref_need": pref_need,
    }


def select(a):
    _begin(a)
    res = _eval_select(a, record_followups=True)
    for line in res["lines"]:
        print(line)
    _action(res["action"])
    return res["rc"]


def _parse_pool_busy(raw):
    """Parse `pool=N[,pool/group=N]` entries -> list of `(prov_raw, grp_raw_or_none, count)` or raise ValueError."""
    entries = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        key, sep, cnt_s = tok.partition("=")
        key, cnt_s = key.strip(), cnt_s.strip()
        if not sep or not key or not re.fullmatch(r"\d+", cnt_s):
            raise ValueError(f"invalid --pool-busy entry {tok!r} (expected <provider>=<N> or <provider>/<group>=<N>)")
        if "/" in key:
            prov_r, _, grp_r = key.partition("/")
            prov_r, grp_r = prov_r.strip(), grp_r.strip()
            if not prov_r or not grp_r:
                raise ValueError(f"invalid --pool-busy entry {tok!r} (expected <provider>/<group>=<N>)")
            entries.append((prov_r, grp_r, int(cnt_s)))
        else:
            entries.append((key, None, int(cnt_s)))
    if not entries:
        raise ValueError("--pool-busy must list at least one <provider>=<N> entry")
    return entries


def _canon_needs_target(target, catalog):
    """Return canonical target string matching `_model_matches_need` (tier, family, strength, or canonical model id), or None."""
    s = target.strip()
    n = s.lower()
    if not n:
        return None
    t_key = n[len("tier:"):].upper() if n.startswith("tier:") else n.upper()
    if t_key in FLOORS:
        return f"tier:{t_key}" if n.startswith("tier:") else t_key
    models = (catalog or {}).get("models") or []
    families = {str(m.get("family") or "").lower() for m in models if m.get("family")}
    if n.startswith("family:"):
        fam = n[len("family:"):]
        return f"family:{fam}" if fam in families else None
    if n in families:
        return n
    strengths = {str(st).lower() for m in models for st in (m.get("strengths") or []) if st}
    if n in strengths:
        return n
    canon_id = _catalog_name_map(catalog).get(norm(n))
    return canon_id


def _match_group_by_name(p, grp_raw):
    """Match a provider group by exact normalized name or case-insensitive substring of its name (not model ids)."""
    groups = [g for g in ((p or {}).get("groups") or []) if isinstance(g, dict) and g.get("name")]
    if not groups:
        return None
    gn = norm(grp_raw)
    exact = next((g for g in groups if norm(g["name"]) == gn), None)
    if exact is not None:
        return exact
    low = grp_raw.strip().lower()
    return next((g for g in groups if low in str(g["name"]).lower()), None)


def _never_due_note(target, catalog):
    """`note:` when a `--needs` model is emergency or unrated: neither ever counts as usable, so the follow-up never becomes due."""
    m = next((x for x in (catalog or {}).get("models") or [] if isinstance(x, dict) and norm(x.get("id") or "") == norm(target)), None)
    why = None if not m else "an emergency model" if m.get("cost") == "emergency" else "unrated" if m.get("rating") == "unrated" else None
    if not why:
        return None
    return f"note: --needs {target} is {why}, so this follow-up never becomes due; name a tier or a rated model instead"


def _validate_defer_needs(needs, catalog, assets, has_after=False):
    """Validate and canonicalize `--needs` for `defer --user-hold` so it matches `_model_matches_need`.

    Returns `(canonical_needs, notes, error_msg)`. Unlisted providers emit a `note:` instead of an error.
    """
    s = needs.strip()
    low = s.lower()
    notes = []
    if low in ("any", "*"):
        if has_after:
            return s, notes, None
        return None, notes, "--needs 'any' requires --after <ISO> (otherwise the hold is immediately due)"
    if low.startswith("provider:"):
        parts = [p.strip() for p in s[len("provider:"):].split("/") if p.strip()]
        if len(parts) not in (2, 3):
            return None, notes, f"invalid --needs {s!r} (expected provider:<id>/<tier|model> or provider:<id>/<group>/<tier>)"
        pid_raw = parts[0]
        providers = [p for p in ((assets or {}).get("providers") or []) if isinstance(p, dict) and p.get("id")]
        by_norm = {norm(p["id"]): p for p in providers}
        p = by_norm.get(norm(pid_raw))
        if p is not None:
            canon_pid = p["id"]
        else:
            canon_pid = pid_raw
            notes.append(f"note: provider {pid_raw} is not in assets.yaml (unlisted providers default to ok)")
        if len(parts) == 3:
            grp_raw, raw_target = parts[1], parts[2]
            if p is not None:
                matched_g = _match_group_by_name(p, grp_raw)
                if matched_g is None:
                    return None, notes, f"unknown quota group {grp_raw!r} for provider {canon_pid!r} in --needs"
                canon_grp = matched_g["name"]
            else:
                canon_grp = grp_raw
        else:
            canon_grp, raw_target = None, parts[1]
        canon_target = _canon_needs_target(raw_target, catalog)
        if canon_target is None:
            return None, notes, f"unknown target {raw_target!r} in --needs (expected a tier T1-T4, model id, family, or strength)"
        canon = f"provider:{canon_pid}/{canon_grp}/{canon_target}" if canon_grp else f"provider:{canon_pid}/{canon_target}"
        notes.extend(n for n in [_never_due_note(canon_target, catalog)] if n)
        return canon, notes, None
    canon_target = _canon_needs_target(s, catalog)
    if canon_target is None:
        return None, notes, f"unknown --needs {s!r} (expected provider:<id>/<tier>, tier T1-T4, family, strength, or catalog model id)"
    notes.extend(n for n in [_never_due_note(canon_target, catalog)] if n)
    return canon_target, notes, None


def _pool_cap_filter(avail, busy_entries, pool_cap, catalog, assets):
    """Validate `--pool-busy` entries and return `(uncapped_avail, capped_lookup, capped_labels)`.

    Raises ValueError when a `--pool-busy` provider has no entry in `avail` or a `<provider>/<group>` does not resolve.
    """
    avail_providers = {norm(pair.partition(":")[0]): pair.partition(":")[0].strip() for pair in avail if ":" in pair}
    providers_by_norm = {
        norm(p["id"]): p for p in ((assets or {}).get("providers") or []) if isinstance(p, dict) and p.get("id")
    }
    cat = {norm(m["id"]): m for m in (catalog or {}).get("models") or []}
    capped_lookup = {}
    capped_labels = {}
    for prov_r, grp_r, cnt in busy_entries:
        np = norm(prov_r)
        if np not in avail_providers:
            raise ValueError(f"--pool-busy provider {prov_r!r} has no entry in --available")
        disp_prov = avail_providers[np]
        if grp_r is not None:
            p = providers_by_norm.get(np)
            matched_g = _find_provider_group(p, group_arg=grp_r, catalog=catalog) if p else None
            if matched_g is None or not matched_g.get("name"):
                raise ValueError(f"--pool-busy group '{prov_r}/{grp_r}' does not match any quota group for {prov_r} in assets.yaml")
            if cnt >= pool_cap:
                gn = norm(matched_g["name"])
                capped_lookup[(np, gn)] = max(capped_lookup.get((np, gn), 0), cnt)
                capped_labels[(np, gn)] = (f"{disp_prov}/{matched_g['name']}", capped_lookup[(np, gn)])
        elif cnt >= pool_cap:
            capped_lookup[(np, None)] = max(capped_lookup.get((np, None), 0), cnt)
            capped_labels[(np, None)] = (disp_prov, capped_lookup[(np, None)])

    if not capped_lookup:
        return list(avail), capped_lookup, []

    uncapped = []
    for pair in avail:
        prov, _, mid = pair.partition(":")
        np = norm(prov)
        p = providers_by_norm.get(np)
        m = cat.get(norm(mid))
        g = model_group(p, mid, m) if p else None
        gn = norm(g["name"]) if (isinstance(g, dict) and g.get("name")) else None
        if (np, None) in capped_lookup or (gn is not None and (np, gn) in capped_lookup):
            continue
        uncapped.append(pair)
    return uncapped, capped_lookup, list(capped_labels.values())


def _cand_cap_match(cand, capped_lookup):
    """Return `(pool_label, busy_n)` if `cand`'s pool is in `capped_lookup`, else None.

    A provider-level cap is labelled `<provider>` (never `<provider>/<group>`);
    a group-level cap is labelled `<provider>/<group>`.
    """
    prov = cand["provider"]
    grp = cand.get("group")
    np = norm(prov)
    gn = norm(grp) if grp else None
    if gn is not None and (np, gn) in capped_lookup:
        return f"{prov}/{grp}", capped_lookup[(np, gn)]
    if (np, None) in capped_lookup:
        return prov, capped_lookup[(np, None)]
    return None


def _free_result_is_no_worse(res_all, res_free):
    """True when `res_free` keeps the same action kind, adds no newly unmet preference or provisional degradation,
    and (for reviewers) stays at the same or better fallback stage."""
    if res_free["kind"] != res_all["kind"]:
        return False
    if res_free["provisional"] and not res_all["provisional"]:
        return False
    if res_free.get("pref_need") and not res_all.get("pref_need"):
        return False
    if res_all["kind"] == "dispatch_reviewer" and res_free["reviewer_stage"] > res_all["reviewer_stage"]:
        return False
    return True


def _defer_refuse(res, ref):
    """Print refusal lines and action for a `defer` call where `select` would dispatch or stage; return exit 5."""
    for line in res["lines"]:
        print(line)
    if res["kind"] == "dispatch":
        act = f"dispatch {_cand_ref(res['candidate'])} now; do not defer {ref}"
        if res["provisional"]:
            act += "; mark it provisional (log --provisional)"
    elif res["kind"] == "dispatch_reviewer":
        act = f"dispatch reviewer {_cand_ref(res['candidate'])} now; do not defer {ref}"
        if res["reviewer_stage"] != 1:
            act += "; state the reviewer degradation in the report"
        act += res.get("followup_suffix") or ""
    elif res["kind"] == "dispatch_orchestrator":
        act = f"launch the orchestrator on {_cand_ref(res['candidate'])}; do not defer {ref}" + _orch_action_suffix(res.get("switch_rung"), name_rung=True)
    else:
        act = res["action"] + f"; do not defer {ref}"
    _action(act)
    return 5


def _defer_hold_or_stop(res, ap, ref, now, extra_lines=()):
    """Print hold/ask/stop output for `defer`, recording a `resume` follow-up for `ui_fallback: defer`; return 0."""
    if res["kind"] == "hold" and res.get("pref_need"):
        pref_need = res["pref_need"]
        when = res["resume_iso"]
        status, rline = _record_resume_lines(ap, ref, pref_need, when, now, on_recovery=True)
        for line in res["lines"]:
            print(line)
        for line in extra_lines:
            print(line)
        print(rline)
        fu_msg = "follow-up in place: schedule its wakeup" if status == "in_place" else "follow-up recorded: schedule its wakeup"
        _action(f"hold until {when or 'the preferred model renews (time unknown)'} (preferred {pref_need} exhausted); {fu_msg}")
        return 0
    for line in res["lines"]:
        print(line)
    for line in extra_lines:
        print(line)
    _action(res["action"])
    return 0


def defer_cmd(a):
    """Gate before deferring work over quota or reviewer family: refuses when select would dispatch or stage."""
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    ref = _opt_str(a, "ref")
    if not ref:
        print("error: defer requires --ref <item>")
        _action("rerun defer with --ref <item>")
        return 1

    user_hold = _opt_bool(a, "user_hold")
    needs = _opt_str(a, "needs")
    after = _opt_str(a, "after")
    on_rec = _opt_bool(a, "on_recovery")
    comp = _opt_str(a, "complexity")
    risk = _opt_str(a, "risk")
    coord = _opt_int(a, "coordination")
    avail = _opt_str(a, "available")
    role = _opt_str(a, "role")
    impl = _opt_str(a, "implementer")
    pool_busy_raw = _opt_str(a, "pool_busy")
    raw_pb = getattr(a, "pool_busy", None)
    if pool_busy_raw is None and isinstance(raw_pb, (int, float)) and not isinstance(raw_pb, bool):
        pool_busy_raw = str(raw_pb)

    if user_hold:
        if any((comp, risk, coord is not None, avail, impl, _opt_bool(a, "ui"), _opt_str(a, "prefer_family"), _opt_str(a, "below_tier"), pool_busy_raw)):
            print("error: defer --user-hold does not accept selection or --pool-busy flags")
            _action("rerun defer --user-hold with --ref, --needs (and optional --after / --on-recovery)")
            return 1
        if not needs or not needs.strip():
            print("error: defer --user-hold requires --needs (e.g. provider:<id>/<tier>)")
            _action("rerun defer --user-hold with --needs provider:<id>/<tier> (and optional --after <ISO>)")
            return 1
        needs = needs.strip()
        if after is not None and parse_time(after, now) is None:
            print(f"error: invalid --after timestamp {after!r} (expected ISO date or datetime)")
            _action("rerun defer --user-hold with an ISO --after timestamp (e.g. 2026-11-23T08:02:00+00:00)")
            return 1
        catalog = load_catalog_with_local(DEF_CATALOG if not _opt_str(a, "catalog") else _opt_str(a, "catalog"), ap)
        assets = load_file(ap) if os.path.exists(ap) else None
        canon_needs, needs_notes, needs_err = _validate_defer_needs(needs, catalog, assets, has_after=bool(after))
        if needs_err:
            print(f"error: {needs_err}")
            _action("rerun defer --user-hold with a valid --needs (e.g. provider:<id>/<tier>, tier T1-T4, family, or model id)")
            return 1
        for note in needs_notes:
            print(note)
        status, line = _record_resume_lines(ap, ref, canon_needs, after, now, on_recovery=on_rec)
        print(line)
        fu_msg = "follow-up in place: schedule its wakeup" if status == "in_place" else "follow-up recorded: schedule its wakeup"
        _action(f"hold {ref} as the user asked; {fu_msg}")
        return 0

    if needs or after or on_rec:
        print("error: --needs, --after, and --on-recovery require --user-hold")
        _action("rerun defer with --user-hold (or omit --needs/--after/--on-recovery)")
        return 1

    eff_role = role or ("reviewer" if impl else "implementer")
    if eff_role == "orchestrator":
        if any((comp, risk, impl, _opt_bool(a, "ui"), _opt_str(a, "prefer_family"), _opt_str(a, "below_tier"))):
            print("error: defer --role orchestrator does not accept worker flags (--complexity/--risk/--implementer/--ui/--prefer-family/--below-tier)")
            _action("rerun defer --role orchestrator with --coordination 0|1|2 and --available")
            return 1
        if coord is None or coord not in (0, 1, 2) or not avail:
            print("error: defer --role orchestrator requires --coordination 0|1|2 and --available")
            _action("rerun defer --role orchestrator with --coordination 0|1|2 and --available")
            return 1
    else:
        if coord is not None:
            print(f"error: --coordination only applies with --role orchestrator (got --role {eff_role})")
            _action("rerun defer with --role orchestrator --coordination 0|1|2 (or omit --coordination)")
            return 1
        if not (comp and risk and avail):
            print("error: defer requires --complexity, --risk, and --available (or --user-hold)")
            _action("rerun defer with --complexity, --risk, and --available (or pass --user-hold)")
            return 1
        if eff_role == "reviewer" and not impl:
            print("error: defer --role reviewer requires --implementer <model-id>")
            _action("rerun defer --role reviewer with --implementer <model-id>")
            return 1
        if role == "implementer" and impl:
            print("error: --implementer only applies with --role reviewer")
            _action("rerun defer with --role reviewer --implementer <model-id> (or omit --implementer)")
            return 1

    pool_cap = _opt_int(a, "pool_cap")
    if pool_cap is None:
        pool_cap = 2
    if pool_cap < 1:
        print(f"error: --pool-cap must be >= 1 (got {pool_cap})")
        _action("rerun defer with --pool-cap >= 1")
        return 1

    busy_entries = []
    if pool_busy_raw is not None:
        try:
            busy_entries = _parse_pool_busy(pool_busy_raw)
        except ValueError as exc:
            print(f"error: {exc}")
            _action("rerun defer with --pool-busy <provider>=<N>[,<provider>/<group>=<N>]")
            return 1

    sel_a = argparse.Namespace(
        role=eff_role,
        complexity=comp,
        risk=risk,
        coordination=coord if eff_role == "orchestrator" else None,
        available=avail,
        ui=_opt_bool(a, "ui"),
        prefer_family=_opt_str(a, "prefer_family"),
        fallback=None,
        below_tier=_opt_str(a, "below_tier"),
        ref=ref,
        fail_on_low=False,
        implementer=impl if eff_role == "reviewer" else None,
        catalog=_opt_str(a, "catalog"),
        assets=ap,
        now=_opt_str(a, "now"),
        full=_FULL,
    )

    catalog = load_catalog_with_local(DEF_CATALOG if not _opt_str(a, "catalog") else _opt_str(a, "catalog"), ap)
    assets = load_file(ap) if os.path.exists(ap) else None
    avail_pairs = [x.strip() for x in avail.split(",") if x.strip()]
    bad_avail = bad_available_entries(avail_pairs, catalog)
    if bad_avail:
        res_err = _eval_select(sel_a, record_followups=False)
        for line in res_err["lines"]:
            print(line)
        _action(res_err["action"])
        return 1

    try:
        uncapped_pairs, capped_lookup, capped_labels = _pool_cap_filter(avail_pairs, busy_entries, pool_cap, catalog, assets)
    except ValueError as exc:
        print(f"error: {exc}")
        _action("rerun defer with valid --pool-busy <provider>=<N>[,<provider>/<group>=<N>] matching --available and assets.yaml")
        return 1

    dispatch_kinds = ("dispatch", "dispatch_reviewer", "dispatch_orchestrator", "stage")
    if not capped_lookup:
        res = _eval_select(sel_a, record_followups=True)
        if res["kind"] == "error":
            for line in res["lines"]:
                print(line)
            _action(res["action"])
            return 1
        if res["kind"] in dispatch_kinds:
            return _defer_refuse(res, ref)
        return _defer_hold_or_stop(res, ap, ref, now)

    # One or more pools are at cap: evaluate the full set first (dry, no follow-up write yet).
    res_all = _eval_select(sel_a, record_followups=False)
    if res_all["kind"] == "error":
        for line in res_all["lines"]:
            print(line)
        _action(res_all["action"])
        return 1
    if res_all["kind"] not in dispatch_kinds:
        # Even with capped pools included, nothing dispatches: genuine quota/policy hold or ask.
        res_real = _eval_select(sel_a, record_followups=True)
        cap_notes = ()
        if res_real["kind"] == "ask" and capped_labels:
            cap_txt = ", ".join(f"{lbl} ({cnt}/{pool_cap})" for lbl, cnt in capped_labels)
            cap_notes = (f"note: pool(s) at cap: {cap_txt}",)
        return _defer_hold_or_stop(res_real, ap, ref, now, extra_lines=cap_notes)

    # If the pool select picked (Phase 1 pool for stage) is NOT capped, refuse with that exact result.
    cap_hit = _cand_cap_match(res_all["candidate"], capped_lookup)
    if cap_hit is None:
        res_real = _eval_select(sel_a, record_followups=True)
        return _defer_refuse(res_real, ref)

    # The picked pool IS capped: accept a free-pool candidate only when it is no worse.
    if uncapped_pairs:
        free_a = argparse.Namespace(**{**vars(sel_a), "available": ",".join(uncapped_pairs)})
        res_free_dry = _eval_select(free_a, record_followups=False)
        if _free_result_is_no_worse(res_all, res_free_dry):
            res_free = _eval_select(free_a, record_followups=True)
            return _defer_refuse(res_free, ref)

    # No equivalent uncapped pool would dispatch -> queue for the next batch on the capped pool.
    pool_label, busy_n = cap_hit
    for line in res_all["lines"]:
        print(line)
    _action(f"queue {ref} for the next batch (pool {pool_label} at cap {busy_n}/{pool_cap}); treat as a batch cap, not a quota wait")
    return 0


# ---------- launch spec & T3 payload ----------
_harness_mid = lambda c: c["pair"].partition(":")[2].strip()
_launch_ref = lambda c: f"{c['provider']}/{_harness_mid(c)} @ {c['effort']}"


def _default_payload_path(spec_path):
    base = os.path.abspath(spec_path)
    for ext in (".launch.yaml", ".launch.yml", ".yaml", ".yml"):
        if base.endswith(ext):
            return base[: -len(ext)] + ".payload.json"
    return base + ".payload.json"


def _effort_option_key(assets, prov):
    p = next((x for x in (assets or {}).get("providers", []) or [] if isinstance(x, dict) and x.get("id") == prov), {})
    ek = p.get("effort_key")
    return ek.strip() if isinstance(ek, str) and ek.strip() else next((key for pfx, key in EFFORT_KEY_PREFIXES if prov.startswith(pfx)), None)


def _validate_launch_spec(spec_path, cli_available, catalog):
    if not os.path.isfile(spec_path):
        return [f"spec file not found: {spec_path}"], None
    try:
        spec = load_file(spec_path)
    except OSError as exc:
        return [f"cannot read {spec_path}: {exc}"], None
    if not isinstance(spec, dict) or not spec:
        return [f"{spec_path}: must be a non-empty YAML map"], None

    errs = [f"unknown top-level key {k!r}" for k in spec if k not in LAUNCH_KEYS]
    _is_str = lambda v: isinstance(v, str) and bool(v.strip())
    _nonneg = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
    ver = spec.get("version", 1)
    if ver is not None and (isinstance(ver, bool) or ver != 1):
        errs.append(f"version: bad value {ver!r} (expected 1)")
    role = spec.get("role") if isinstance(spec.get("role"), str) and spec.get("role") in VALID_ROLES else None
    if role is None:
        errs.append(f"role: bad value {spec.get('role')!r} (expected {'|'.join(VALID_ROLES)})")
    for k in ("title", "repo"):
        if spec.get(k) is not None and not _is_str(spec.get(k)):
            errs.append(f"{k}: must be a non-empty string")
    title = spec["title"].strip() if _is_str(spec.get("title")) else os.path.basename(spec_path)

    raw_brief, brief_text = spec.get("brief"), None
    if not _is_str(raw_brief):
        errs.append("brief: required relative path to a non-empty brief file")
    else:
        bpath = raw_brief.strip() if os.path.isabs(raw_brief.strip()) else os.path.join(os.path.dirname(os.path.abspath(spec_path)), raw_brief.strip())
        if not os.path.isfile(bpath):
            errs.append(f"brief: file not found: {raw_brief.strip()}")
        else:
            try:
                with open(bpath, encoding="utf-8") as fh:
                    brief_text = fh.read()
            except OSError as exc:
                errs.append(f"brief: cannot read {raw_brief.strip()}: {exc}")
            if brief_text is not None and not brief_text.strip():
                errs.append(f"brief: file is empty: {raw_brief.strip()}")

    raw_rubric, rubric = spec.get("rubric"), None
    if not isinstance(raw_rubric, dict) or set(raw_rubric.keys()) != set(RUBRIC_KEYS) or any(
        isinstance(raw_rubric[k], bool) or not isinstance(raw_rubric[k], int) or raw_rubric[k] not in (0, 1, 2) for k in RUBRIC_KEYS
    ):
        errs.append(f"rubric: must be a map with all six keys ({', '.join(RUBRIC_KEYS)}) in 0..2")
    else:
        rubric = {k: raw_rubric[k] for k in RUBRIC_KEYS}

    raw_ladder, ladder = spec.get("ladder"), None
    if raw_ladder == "auto":
        ladder = "auto"
    elif raw_ladder is not None:
        if not isinstance(raw_ladder, list) or not raw_ladder:
            errs.append(f"ladder: bad value {raw_ladder!r} (expected auto or list of provider:model@effort)")
        else:
            bad_l = [idx for idx, item in enumerate(raw_ladder) if not _parse_pme(item)]
            errs.extend(f"ladder[{idx}]: bad entry {raw_ladder[idx]!r} (expected provider:model@effort)" for idx in bad_l)
            ladder = list(raw_ladder) if not bad_l else None

    raw_impl = spec.get("implementer")
    if role == "reviewer":
        if not _is_str(raw_impl):
            errs.append("implementer: required for role reviewer")
        elif resolve_implementer(catalog, raw_impl) is None:
            errs.append(f"implementer: {raw_impl!r} is not in catalog.yaml")
    elif raw_impl is not None and not _is_str(raw_impl):
        errs.append("implementer: must be a non-empty string")

    avail, ra = [], spec.get("available")
    if cli_available:
        avail = [x.strip() for x in cli_available.split(",") if x.strip()]
    elif isinstance(ra, list):
        avail = [str(x).strip() for x in ra if str(x).strip()]
    elif isinstance(ra, str):
        avail = [x.strip() for x in ra.split(",") if x.strip()]
    elif ra is not None:
        errs.append("available: must be a list of provider:model pairs")
    if not avail:
        errs.append("available: provide available in the spec or --available on the CLI")
    else:
        bad_av = bad_available_entries(avail, catalog)
        if bad_av:
            errs.append("available: malformed provider:model entries: " + ", ".join(_show_entry(x) for x in bad_av))

    raw_ws, ws = spec.get("workspace"), {"strategy": "root", "present": False}
    if raw_ws is not None and not isinstance(raw_ws, dict):
        errs.append("workspace: must be a map")
    elif isinstance(raw_ws, dict):
        ws["present"] = True
        errs.extend(f"workspace: unknown key {wk!r}" for wk in raw_ws if wk not in ("strategy", "base", "children", "worktree_path", "branch"))
        strat = raw_ws.get("strategy")
        if strat not in ("worktree", "root", "existing_worktree"):
            errs.append(f"workspace.strategy: bad value {strat!r} (expected worktree|root|existing_worktree)")
        else:
            ws["strategy"] = strat
        for req_s, key in (("worktree", "base"), ("existing_worktree", "worktree_path")):
            val = raw_ws.get(key)
            if strat == req_s and not _is_str(val):
                errs.append(f"workspace.{key}: required when strategy is {req_s}")
            elif val is not None and not _is_str(val):
                errs.append(f"workspace.{key}: must be a non-empty string")
            elif _is_str(val):
                ws[key] = val.strip()
        ch = raw_ws.get("children")
        if ch is not None and ch not in ("shared", "worktree"):
            errs.append(f"workspace.children: bad value {ch!r} (expected shared|worktree)")
        elif ch:
            ws["children"] = ch
        if raw_ws.get("branch") is not None:
            if _is_str(raw_ws.get("branch")):
                ws["branch"] = raw_ws["branch"].strip()
            else:
                errs.append("workspace.branch: must be a non-empty string")

    raw_caps, caps = spec.get("caps"), {"max_escalations": None, "max_children_per_pool": None, "budget": None}
    if raw_caps is not None and not isinstance(raw_caps, dict):
        errs.append("caps: must be a map")
    elif isinstance(raw_caps, dict):
        errs.extend(f"caps: unknown key {ck!r}" for ck in raw_caps if ck not in ("max_escalations", "max_children_per_pool", "budget"))
        me, mc, bg = raw_caps.get("max_escalations"), raw_caps.get("max_children_per_pool"), raw_caps.get("budget")
        if me is not None and (not _nonneg(me) or me > 3):
            errs.append(f"caps.max_escalations: bad value {me!r} (expected int 0..3)")
        elif me is not None:
            caps["max_escalations"] = me
        if mc is not None and not _nonneg(mc):
            errs.append(f"caps.max_children_per_pool: bad value {mc!r} (expected non-negative int)")
        elif mc is not None:
            caps["max_children_per_pool"] = mc
        if bg is not None and (not isinstance(bg, dict) or not bg):
            errs.append("caps.budget: must be a non-empty map of provider: non-negative int")
        elif isinstance(bg, dict):
            errs.extend(f"caps.budget[{bk}]: bad value {bv!r} (expected non-negative int)" for bk, bv in bg.items() if not _is_str(bk) or not _nonneg(bv))
            if all(_is_str(bk) and _nonneg(bv) for bk, bv in bg.items()):
                caps["budget"] = {bk.strip(): bv for bk, bv in bg.items()}

    if errs:
        return errs, None
    return [], {
        "role": role, "title": title, "repo": spec["repo"].strip() if _is_str(spec.get("repo")) else None,
        "brief_text": brief_text, "rubric": rubric, "ladder": ladder,
        "implementer": raw_impl.strip() if _is_str(raw_impl) else None,
        "available": avail, "workspace": ws, "caps": caps,
    }


def _is_launch_payload(path):
    """True only for a JSON payload an earlier `launch` wrote (so a mistyped --out never deletes a spec or brief)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "modelSelection" in data and "clientRequestId" in data


def _cleanup_stale_payload(out_path):
    if out_path and os.path.isfile(out_path) and _is_launch_payload(out_path):
        try:
            os.remove(out_path)
            print(f"note: removed stale payload {out_path}")
        except OSError:
            pass


def _build_launch_header(role, cand, same_rungs, switch_rung, max_esc, caps, ws, rubric_str, complexity, risk, deg_line=None, thresholds=(3, 3)):
    lines = [f"{role.upper()} — repeat this block, updated, at every phase boundary"]
    if role == "orchestrator":
        lines.append(f"model: {_launch_ref(cand)} (rung 1 of {len(same_rungs)})")
        lines.append("ladder: " + " -> ".join(_launch_ref(r) for r in same_rungs))
        if switch_rung:
            lines.append(f"provider switch (phase boundary, ask the user first): {_launch_ref(switch_rung)}")
        esc_noun = "escalation" if max_esc == 1 else "escalations"
        lines.append(
            f"escalate on (orch.py escalate): outcome rework | {thresholds[1]}+ findings exposing a gap in your brief or plan "
            f"(references/escalation.md) (workers: {thresholds[0]}+) | a child's STATUS: ESCALATE"
        )
        lines.append(f"escalation cap: at most {max_esc} {esc_noun}; never step down mid-phase")
    else:
        lines.append(f"model: {_launch_ref(cand)}")
        if deg_line:
            lines.append(deg_line)
    cap_parts = ([f"children per pool {caps['max_children_per_pool']}"] if caps.get("max_children_per_pool") is not None else []) + (
        ["budget " + ", ".join(f"{k} {v}" for k, v in caps["budget"].items())] if caps.get("budget") else []
    )
    if cap_parts:
        lines.append("caps: " + "; ".join(cap_parts))
    if ws.get("present"):
        strat = ws["strategy"]
        ws_txt = f"worktree from {ws['base']}" if strat == "worktree" else f"existing_worktree at {ws['worktree_path']}" if strat == "existing_worktree" else "root"
        lines.append(f"workspace: {ws_txt}" + (f"; children {ws['children']}" if ws.get("children") else ""))
    lines.append(f"rubric: {rubric_str} ({complexity}/{risk})")
    lines.append("verify: the launcher compares requested vs effective model/effort after launch (references/harnesses.md)")
    return lines


def launch_cmd(a):
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    cat_path = _opt_str(a, "catalog") or DEF_CATALOG
    out_path = _opt_str(a, "out") or _default_payload_path(a.spec)
    catalog = load_catalog_with_local(cat_path, ap)
    errs, parsed = _validate_launch_spec(a.spec, _opt_str(a, "available"), catalog)
    if errs:
        for line in _capped(["error: " + e for e in errs]):
            print(line)
        _cleanup_stale_payload(out_path)
        print(f"error: {len(errs)} problem(s); action: fix {a.spec} and rerun launch")
        return 1

    avail = parsed["available"]
    pre_notes = []
    if not _opt_bool(a, "no_probe"):
        if not os.path.exists(ap):
            pre_notes.append("note: no assets file: probe skipped (run orch.py probe --init)")
        else:
            import contextlib, io
            probe_ns = argparse.Namespace(
                assets=ap, catalog=cat_path, now=_opt_str(a, "now"), provider=None,
                low_threshold=None, exhausted_threshold=None, available=",".join(avail), fresh=False, full=False,
            )
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    p_rc = _probe(probe_ns, ap)
                if p_rc:
                    pre_notes.append(f"note: probe failed (exit {p_rc}); resolving on the last known quota")
            except Exception as exc:
                reason = str(exc).splitlines()[0][:80] if str(exc) else exc.__class__.__name__
                pre_notes.append(f"note: probe failed ({reason}); resolving on the last known quota")

    assets = load_file(ap) if os.path.exists(ap) else None
    prefs = (assets or {}).get("preferences") or {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    role, rubric = parsed["role"], parsed["rubric"]
    if role != "orchestrator":
        ign = [k for k, v in (("ladder", parsed["ladder"] not in (None, "auto")), ("caps.max_escalations", parsed["caps"]["max_escalations"] is not None)) if v]
        if ign:
            pre_notes.append(f"note: ignored for role {role}: {', '.join(ign)}")
    complexity, risk, orch_tier, _, rubric_str = _classify_rubric(rubric, assets)
    avail_csv = ",".join(avail)
    avail_arg = avail_csv if len(avail_csv) <= 80 else "<available>"

    def _fail_non_dispatch(notes_list, sel_cmd):
        for pn in pre_notes:
            print(pn)
        _print_notes(notes_list)
        _cleanup_stale_payload(out_path)
        _action(f"run {sel_cmd} and follow its action line; launch only resolves a dispatchable model")
        return 3

    same_rungs, switch_rung, max_esc, notes, rev_pick, rev_rt, rev_impl = [], None, 2, [], None, None, None
    if role == "orchestrator":
        cand, same_rungs, switch_rung, notes, info = _compute_orchestrator_ladder(
            catalog, assets, avail, floors, rubric["coordination"], now,
            ladder_override=parsed["ladder"], max_escalations_override=parsed["caps"]["max_escalations"],
        )
        if info["rc"] != 0 or not cand:
            return _fail_non_dispatch(notes, f"select --role orchestrator --coordination {rubric['coordination']} --available {avail_arg}")
        max_esc = info["max_escalations"]
        role_head = f"role: orchestrator · {complexity}/{risk} · tier {orch_tier} (coordination {rubric['coordination']})"
    elif role == "implementer":
        tier = TIER[complexity][risk]
        cands, notes, _ = candidates(catalog, assets, avail, floors[tier], floors, now)
        cands.sort(key=lambda c: c["key"])
        if not cands:
            return _fail_non_dispatch(notes, f"select --complexity {complexity} --risk {risk} --available {avail_arg}")
        cand, role_head = cands[0], f"role: implementer · {complexity}/{risk} · tier {tier}"
    else:
        tier, rev_rt = TIER[complexity][risk], reviewer_tier(TIER[complexity][risk], risk)
        rev_impl = resolve_implementer(catalog, parsed["implementer"])
        emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
        rev_pick, _ = choose_reviewer(
            catalog, assets, avail, floors, rev_rt, now, rev_impl, False, risk,
            prefs.get("below_tier") or "stage", impl_below_tier=best_effort(rev_impl, floors[tier], emerg) is None,
        )
        _, notes, _ = candidates(catalog, assets, avail, 0, floors, now, pick_top=True, task_floor=floors[rev_rt])
        if not rev_pick:
            return _fail_non_dispatch(notes, f"select --role reviewer --complexity {complexity} --risk {risk} --implementer {parsed['implementer']} --available {avail_arg}")
        cand, role_head = rev_pick[1], f"role: reviewer · {complexity}/{risk} · tier {rev_rt} (implementer {rev_impl['id']})"

    prov = cand["provider"]
    effort_key = _effort_option_key(assets, prov)
    if not effort_key:
        print(f"error: no effort option key for {prov}: set effort_key on it in assets.yaml")
        _cleanup_stale_payload(out_path)
        print(f"error: 1 problem(s); action: set effort_key on {prov} in assets.yaml and rerun launch")
        return 1

    deg_why = _reviewer_why(rev_rt, rev_pick, rev_impl, floors[rev_rt]) if rev_pick else None
    deg_header = f"reviewer degraded: {deg_why} (step {rev_pick[0]}); state it in the report" if deg_why else None
    h_mid = _harness_mid(cand)
    options = [{"id": effort_key, "value": cand["effort"]}] + ([{"id": "serviceTier", "value": "default"}] if prov.startswith("codex") else [])
    ws = parsed["workspace"]
    if ws["strategy"] == "worktree":
        ws_strat = {"type": "worktree", "baseRef": ws["base"], "startFromOrigin": "/" in ws["base"], **({"branch": ws["branch"]} if ws.get("branch") else {})}
    elif ws["strategy"] == "existing_worktree":
        ws_strat = {"type": "existing_worktree", "worktreePath": ws["worktree_path"]}
    else:
        ws_strat = {"type": "root"}

    header_lines = _build_launch_header(role, cand, same_rungs, switch_rung, max_esc, parsed["caps"], ws, rubric_str, complexity, risk, deg_line=deg_header,
                                        thresholds=(_escalation_prefs(assets)["findings"], _orchestrator_prefs(assets)[0]["unflagged_findings"]))
    slug = norm(parsed["title"])[:48].strip("-") or "task"
    ts_tag = now.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    payload = {
        "title": parsed["title"],
        "message": "\n".join(header_lines) + "\n\n" + parsed["brief_text"],
        "modelSelection": {"instanceId": prov, "model": h_mid, "options": options},
        "workspaceStrategy": ws_strat,
        "clientRequestId": f"launch-{slug}-{prov}-{h_mid}-{cand['effort']}-{ts_tag}",
    }
    try:
        if os.path.dirname(os.path.abspath(out_path)):
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        reason = exc.strerror or str(exc)
        print(f"error: cannot write payload to {out_path}: {reason}")
        _cleanup_stale_payload(out_path)
        print("error: 1 problem(s); action: pass a writable file path to --out and rerun launch")
        return 1

    req_str = f"{prov}:{h_mid}@{cand['effort']}"
    repo_base = os.path.basename(os.path.normpath((parsed["repo"] or os.getcwd()).rstrip("/\\"))) or "unknown"
    print(role_head)
    print(f"requested: {req_str}")
    if role == "orchestrator":
        print("ladder: " + " -> ".join(_launch_ref(r) for r in same_rungs) + (f" ; provider switch: {_launch_ref(switch_rung)}" if switch_rung else ""))
        for r in (same_rungs if _FULL else ()):
            _detail(f"  rung: {_fmt_cand(r)}")
        if _FULL and switch_rung:
            _detail(f"  switch: {_fmt_cand(switch_rung)}")
    elif role == "reviewer":
        print_reviewer(rev_rt, rev_pick, rev_impl, floors[rev_rt], always=True)
    else:
        _detail(f"  candidate: {_fmt_cand(cand)}")
    for pn in pre_notes:
        print(pn)
    _print_notes(notes)
    print(f"payload: {out_path}")
    # Runnable as written once --effective is filled in. Optional flags (log defaults: --event select, --repo = cwd
    # basename, --surface subagent) are dropped in this order only to keep the line within the output contract.
    optional = [("--event", "select"), ("--surface", "conversation"), ("--repo", repo_base)]
    required = (
        f"--role {role} --complexity {complexity} --risk {risk} --provider {prov} --model {h_mid} "
        f"--effort {cand['effort']} --rubric {rubric_str} --requested {req_str} --effective <P:M@E>"
    )
    dropped = []
    while True:
        log_line = "log: orch.py log " + "".join(f"{k} {v} " for k, v in optional) + required
        if len(log_line) <= 350 or not optional:
            break
        dropped.append(optional.pop(0))
    lost = [f"{k} {v}" for k, v in dropped if k != "--event"]
    if lost:
        print("note: the log: line omits " + ", ".join(lost) + " to fit; add them when you run it")
    print(log_line)
    deg_tail = "; state the reviewer degradation in the report" if deg_why else ""
    act_full = f"call t3_thread_launch with the JSON in {out_path}; then read the new thread's model (t3_thread_configure with only threadId) and run the log: line above with --effective <P:M@E> filled in{deg_tail}"
    if len("action: " + act_full) > 350:
        act_full = f"call t3_thread_launch with the JSON in the payload: path above; then read the new thread's model (t3_thread_configure with only threadId) and run the log: line above with --effective <P:M@E> filled in{deg_tail}"
    _action(act_full)
    return 0


# ---------- assets edits & reset parsing ----------
def parse_reset(msg, now):
    tzm = re.search(r"\((UTC|GMT|[A-Za-z_][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+)\)", msg)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzm.group(1)) if tzm else now.tzinfo
    except Exception:
        tz = now.tzinfo

    # 1. Relative durations: "Resets in 4h29m1s", "resets in 2 hours 30 minutes", "try again in 1 day, 2 hrs and 15 mins"
    rel = re.search(
        r"(?:resets?|try again)\s+in\s+((?:\d+\s*(?:days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)(?![A-Za-z])(?:[\s,]+(?:and\s+)?)?)+)",
        msg,
        re.IGNORECASE,
    )
    if rel:
        unit_map = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
        kw = {"days": 0, "hours": 0, "minutes": 0, "seconds": 0}
        for num, unit in re.findall(
            r"(\d+)\s*(days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)(?![A-Za-z])",
            rel.group(1),
            re.IGNORECASE,
        ):
            kw[unit_map[unit[0].lower()]] += int(num)
        if any(kw.values()):
            return now + timedelta(**kw)

    # 2. ISO date or datetime: "2026-12-01" or "2026-12-01 08:30" or "2026-12-01T08:30:00+00:00"
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?)?", msg)
    if m:
        d = date.fromisoformat(m.group(1))
        if m.group(2) is not None:
            hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
            if m.group(5):
                iso = f"{m.group(1)}T{hh:02d}:{mm:02d}:{ss:02d}{m.group(5)}"
                return parse_time(iso, now)
            return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=tz)
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)

    # 3. Named month with time (12h AM/PM or 24h HH:MM)
    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?,?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])\b",
        msg,
    )
    if m:
        mon, day, yr, hh, mm, ap = m.groups()
        h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
        month = datetime.strptime(mon, "%b").month
        year = int(yr) if yr else now.year
        t = datetime(year, month, int(day), h, int(mm or 0), tzinfo=tz)
        if not yr and t <= now:
            t = t.replace(year=year + 1)
        return t

    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?,?\s*(?:at\s*)?(\d{1,2}):(\d{2})\b",
        msg,
    )
    if m:
        mon, day, yr, hh, mm = m.groups()
        month = datetime.strptime(mon, "%b").month
        year = int(yr) if yr else now.year
        t = datetime(year, month, int(day), int(hh), int(mm), tzinfo=tz)
        if not yr and t <= now:
            t = t.replace(year=year + 1)
        return t

    # 4. Date-only named month -> end of that day (23:59:59)
    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
        msg,
    )
    if m:
        mon, day, yr = m.groups()
        month = datetime.strptime(mon, "%b").month
        year = int(yr) if yr else now.year
        t = datetime(year, month, int(day), 23, 59, 59, tzinfo=tz)
        if not yr and t <= now:
            t = t.replace(year=year + 1)
        return t

    return None


def ensure_assets(path, now):
    now_iso = now.isoformat(timespec="seconds")
    if not os.path.exists(path):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"version: 1\nupdated: {now_iso}\nproviders:\n")
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not re.search(r"(?m)^providers:\s*(?:#.*)?$", text):
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + "providers:\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def _update_top_updated(lines, now_iso):
    for i, l in enumerate(lines):
        if re.match(r"^updated:\s*", l):
            lines[i] = f"updated: {now_iso}"
            return
    for i, l in enumerate(lines):
        if re.match(r"^version:\s*", l):
            lines.insert(i + 1, f"updated: {now_iso}")
            return
    lines.insert(0, f"updated: {now_iso}")


def _providers_span(lines):
    pstart = next((i for i, l in enumerate(lines) if re.match(r"^providers:\s*(?:#.*)?$", l)), None)
    if pstart is None:
        return None, None
    pend = pstart + 1
    while pend < len(lines):
        l = lines[pend]
        if l.strip() and not l.startswith(" ") and not l.startswith("#"):
            break
        pend += 1
    return pstart, pend


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or s in ("true", "false", "null", "~") or not re.fullmatch(r"[A-Za-z0-9._:+/-]+", s):
        return '"' + s.replace('"', "'") + '"'
    return s


def _format_yaml_field(ind, k, v):
    pad = " " * ind
    if isinstance(v, list):
        if not v:
            return [f"{pad}{k}: []"]
        if all(isinstance(item, dict) for item in v):
            out = [f"{pad}{k}:"]
            for item in v:
                first = True
                for ik, iv in item.items():
                    if iv is None:
                        continue
                    if isinstance(iv, list):
                        rendered = "[" + ", ".join(_yaml_scalar(x) for x in iv) + "]"
                    elif isinstance(iv, dict):
                        rendered = "{" + ", ".join(f'"{str(dk).replace(chr(34), chr(39))}": {_yaml_scalar(dv)}' for dk, dv in iv.items()) + "}"
                    else:
                        rendered = _yaml_scalar(iv)
                    prefix = f"{pad}  - " if first else f"{pad}    "
                    out.append(f"{prefix}{ik}: {rendered}")
                    first = False
            return out
        return [f"{pad}{k}: [" + ", ".join(_yaml_scalar(x) for x in v) + "]"]
    return [f"{pad}{k}: {v}"]


def edit_provider(path, pid, fields, now=None, clear_keys=()):
    now = now or get_now()
    ensure_assets(path, now)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    _update_top_updated(lines, now.isoformat(timespec="seconds"))

    pstart, pend = _providers_span(lines)
    start = next(
        (
            i
            for i in range(pstart + 1, pend)
            if re.match(rf"^\s*-\s+id:\s*{re.escape(pid)}\s*(?:#.*)?$", lines[i])
        ),
        None,
    )
    if start is None:
        insert_at = pend
        while insert_at > pstart + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"  - id: {pid}")
        start = insert_at
        pend += 1

    ind = len(lines[start]) - len(lines[start].lstrip()) + 2
    end = start + 1
    while end < pend:
        l = lines[end]
        if not l.strip():
            end += 1
            continue
        cur_ind = len(l) - len(l.lstrip())
        if cur_ind < ind or (cur_ind == ind - 2 and l.lstrip().startswith("- ")):
            break
        end += 1

    drop = set(fields.keys()) | set(clear_keys)
    drop_pat = re.compile(rf"^{' ' * ind}({'|'.join(re.escape(k) for k in drop)}):") if drop else None
    keep = []
    i = start + 1
    while i < end:
        l = lines[i]
        if drop_pat and drop_pat.match(l):
            i += 1
            while i < end and lines[i].strip() and (len(lines[i]) - len(lines[i].lstrip())) > ind:
                i += 1
            continue
        keep.append(l)
        i += 1

    new = []
    for k, v in fields.items():
        if v is not None:
            new.extend(_format_yaml_field(ind, k, v))
    lines[start + 1 : end] = new + keep
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def _top_section_span(lines, key):
    pat = re.compile(rf"^{re.escape(key)}:\s*(?:#.*)?$|^{re.escape(key)}:\s*\[\s*\]\s*(?:#.*)?$")
    start = next((i for i, l in enumerate(lines) if pat.match(l)), None)
    if start is None:
        return None, None
    end = start + 1
    while end < len(lines):
        l = lines[end]
        if l.strip() and not l.startswith(" ") and not l.startswith("#"):
            break
        end += 1
    return start, end


def write_followups(path, followups, now=None):
    now = now or get_now()
    ensure_assets(path, now)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    _update_top_updated(lines, now.isoformat(timespec="seconds"))

    fstart, fend = _top_section_span(lines, "followups")
    rendered = _format_yaml_field(0, "followups", followups) if followups else []
    if fstart is not None:
        lines[fstart:fend] = rendered
    elif rendered:
        lines.extend(rendered)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def find_existing_followup(path, kind, ref, needs=None):
    if not os.path.exists(path):
        return None
    assets = load_file(path) or {}
    for fu in (assets.get("followups") or []):
        if not isinstance(fu, dict):
            continue
        if kind == "resume":
            if fu.get("kind") == "resume" and str(fu.get("ref") or "") == str(ref or ""):
                return fu
        elif (fu.get("kind"), fu.get("ref"), fu.get("needs")) == (kind, ref, needs):
            return fu
    return None


def record_followup(path, item, now=None):
    now = now or get_now()
    ensure_assets(path, now)
    assets = load_file(path) or {}
    existing = [x for x in (assets.get("followups") or []) if isinstance(x, dict)]
    is_resume = item.get("kind") == "resume"
    key = ("resume", item.get("ref")) if is_resume else (item.get("kind"), item.get("ref"), item.get("needs"))
    updated = []
    replaced = False
    for fu in existing:
        fu_key = ("resume", fu.get("ref")) if (is_resume and fu.get("kind") == "resume") else (
            fu.get("kind"),
            fu.get("ref"),
            fu.get("needs"),
        )
        if fu_key == key:
            updated.append(item)
            replaced = True
        else:
            updated.append(fu)
    if not replaced:
        updated.append(item)
    write_followups(path, updated, now=now)
    return updated


def _find_provider_group(p, group_arg=None, model_arg=None, catalog=None):
    groups = [g for g in ((p or {}).get("groups") or []) if isinstance(g, dict)]
    if not groups:
        return None
    cat_models = (catalog or {}).get("models") or []
    cat_by_id = {norm(m["id"]): m for m in cat_models if isinstance(m, dict) and m.get("id")}
    name_map = _catalog_name_map(catalog)
    if group_arg:
        gn = norm(group_arg)
        exact = next((g for g in groups if norm(g.get("name")) == gn), None)
        if exact is not None:
            return exact
        low = group_arg.strip().lower()
        sub = next((g for g in groups if low in str(g.get("name") or "").lower()), None)
        if sub is not None:
            return sub
        cid = name_map.get(gn) or group_arg
        return model_group(p, cid, cat_by_id.get(norm(cid)))
    if model_arg:
        pid = (p or {}).get("id") or ""
        raw_mid = model_arg.partition(":")[2] if ":" in model_arg and norm(model_arg.partition(":")[0]) == norm(pid) else model_arg
        cid = name_map.get(norm(raw_mid)) or raw_mid
        return model_group(p, cid, cat_by_id.get(norm(cid)))
    return None


def _exhaust_next_action(a, ap):
    """Print the follow-on action line for `exhaust` (selection args, `--self`, or default guidance)."""
    is_self = _opt_bool(a, "is_self") or _opt_bool(a, "self")
    comp = _opt_str(a, "complexity")
    risk = _opt_str(a, "risk")
    coord = _opt_int(a, "coordination")
    avail = _opt_str(a, "available")
    role = _opt_str(a, "role")
    impl = _opt_str(a, "implementer")
    has_sel = any((comp, risk, coord is not None, avail, role, impl, _opt_bool(a, "ui"), _opt_str(a, "below_tier"), _opt_str(a, "ref")))

    if is_self:
        self_ref = _opt_str(a, "ref") or "orchestrator-self"
        sel_a = argparse.Namespace(
            role="orchestrator",
            complexity=None,
            risk=None,
            coordination=coord,
            available=avail,
            ui=False,
            prefer_family=None,
            fallback=None,
            below_tier=None,
            ref=self_ref,
            fail_on_low=False,
            implementer=None,
            catalog=_opt_str(a, "catalog"),
            assets=ap,
            now=_opt_str(a, "now"),
            full=_FULL,
        )
        res = _eval_select(sel_a, record_followups=True)
        for line in res["lines"]:
            print(line)
        if res["kind"] == "error":
            _action(res["action"])
            return 1
        if res["kind"] == "dispatch_orchestrator":
            _action(
                f'switch this conversation to {_cand_ref(res["candidate"])} in place, or hand off with orch.py launch <spec> '
                '(references/orchestrator.md "Orchestrator\'s own quota")' + _orch_action_suffix(res.get("switch_rung"), name_rung=True)
            )
            return 0
        if res["kind"] == "hold":
            when = res["resume_iso"] or "the earliest reset (time unknown)"
            _action(f"hold until {when}; schedule this conversation's resume")
            return res["rc"]
        _action(res["action"])
        return res["rc"]

    if has_sel:
        eff_role = role or ("reviewer" if impl else "implementer")
        sel_a = argparse.Namespace(
            role=eff_role,
            complexity=comp,
            risk=risk,
            coordination=coord if eff_role == "orchestrator" else None,
            available=avail,
            ui=_opt_bool(a, "ui"),
            prefer_family=None,
            fallback=None,
            below_tier=_opt_str(a, "below_tier"),
            ref=_opt_str(a, "ref"),
            fail_on_low=False,
            implementer=impl if eff_role == "reviewer" else None,
            catalog=_opt_str(a, "catalog"),
            assets=ap,
            now=_opt_str(a, "now"),
            full=_FULL,
        )
        res = _eval_select(sel_a, record_followups=True)
        for line in res["lines"]:
            print(line)
        _action(res["action"])
        return res["rc"]

    _action("run select (or defer) with the exhausted model removed and follow its action: line")
    return 0


def exhaust(a):
    _begin(a)
    is_self = _opt_bool(a, "is_self") or _opt_bool(a, "self")
    comp = _opt_str(a, "complexity")
    risk = _opt_str(a, "risk")
    coord = _opt_int(a, "coordination")
    avail = _opt_str(a, "available")
    role = _opt_str(a, "role")
    impl = _opt_str(a, "implementer")
    extra_sel = any((_opt_bool(a, "ui"), _opt_str(a, "below_tier"), _opt_str(a, "ref")))
    has_sel = any((comp, risk, coord is not None, avail, role, impl)) or extra_sel
    if is_self:
        if any((comp, risk, role, impl, _opt_bool(a, "ui"), _opt_str(a, "below_tier"))):
            print("error: exhaust --self uses the orchestrator ladder (--coordination 0|1|2); omit worker flags")
            _action("rerun exhaust --self with --coordination 0|1|2 and --available")
            return 1
        if coord is None or coord not in (0, 1, 2) or not avail:
            print("error: exhaust --self requires --coordination 0|1|2 and --available")
            _action("rerun exhaust --self with --coordination 0|1|2 and --available")
            return 1
    elif has_sel:
        if role == "orchestrator":
            if any((comp, risk, impl, _opt_bool(a, "ui"), _opt_str(a, "below_tier"))):
                print("error: exhaust --role orchestrator does not accept worker flags")
                _action("rerun exhaust --role orchestrator with --coordination 0|1|2 and --available")
                return 1
            if coord is None or coord not in (0, 1, 2) or not avail:
                print("error: exhaust --role orchestrator requires --coordination 0|1|2 and --available")
                _action("rerun exhaust --role orchestrator with --coordination 0|1|2 and --available")
                return 1
        else:
            if coord is not None:
                print("error: --coordination only applies with --self or --role orchestrator")
                _action("rerun exhaust with --self --coordination 0|1|2 (or omit --coordination)")
                return 1
            if not (comp and risk and avail):
                print("error: exhaust selection arguments require --complexity, --risk, and --available")
                _action("rerun exhaust with --complexity, --risk, and --available (or omit all selection flags to only record the lockout)")
                return 1
            if role == "reviewer" and not impl:
                print("error: exhaust --role reviewer requires --implementer <model-id>")
                _action("rerun exhaust --role reviewer with --implementer <model-id>")
                return 1
            if role == "implementer" and impl:
                print("error: --implementer only applies with --role reviewer")
                _action("rerun exhaust with --role reviewer --implementer <model-id> (or omit --implementer)")
                return 1
    if avail:
        bad_ap = _opt_str(a, "assets") or def_assets()
        bad_cat_path = _opt_str(a, "catalog") or DEF_CATALOG
        bad_cat = load_catalog_with_local(bad_cat_path, bad_ap) if os.path.exists(bad_cat_path) else {"models": []}
        bad = bad_available_entries([x.strip() for x in avail.split(",") if x.strip()], bad_cat)
        if bad:
            print("error: --available entries are not provider:model: " + ", ".join(_capped([_show_entry(x) for x in bad])))
            _action("fix --available (comma-separated <provider>:<model-id> pairs as discovery lists them) and rerun exhaust; nothing was recorded")
            return 1

    now = get_now(_opt_str(a, "now"))
    t = parse_reset(a.message, now)
    raw_note = a.message.replace('"', "'")[:200]
    quoted_note = '"' + raw_note + '"'
    until = (t or now + timedelta(hours=1)).isoformat(timespec="seconds")
    ap = _opt_str(a, "assets") or def_assets()
    group_arg = _opt_str(a, "group")
    model_arg = _opt_str(a, "model")
    # A per-model quota inside a shared pool (Antigravity "Individual quota reached"): lock only that model.
    individual = bool(model_arg and not group_arg and re.search(r"individual quota", a.message, re.IGNORECASE))
    unparsed_txt = "" if t else " (unparsed, +1h default)"

    if not group_arg and not model_arg and os.path.exists(ap):
        cur = load_file(ap) or {}
        cur_p = next((x for x in (cur.get("providers") or []) if isinstance(x, dict) and x.get("id") == a.provider), None)
        cur_groups = [g for g in ((cur_p or {}).get("groups") or []) if isinstance(g, dict)]
        if cur_groups and partitions_quota(cur_p):
            names = ", ".join(str(g.get("name")) for g in cur_groups)
            print(
                f"error: {a.provider}: quota is split into groups ({names}); pass --model <id> for the model that hit "
                "the limit (or --group <name>) so the other groups stay usable"
            )
            _action("rerun exhaust with --model <id> (or --group <name>)")
            return 1

    if group_arg or model_arg:
        ensure_assets(ap, now)
        assets = load_file(ap) or {}
        p = next((x for x in (assets.get("providers") or []) if isinstance(x, dict) and x.get("id") == a.provider), None)
        catalog_path = _opt_str(a, "catalog") or DEF_CATALOG
        catalog = load_catalog_with_local(catalog_path, ap) if os.path.exists(catalog_path) else {"models": []}
        groups = [g for g in ((p or {}).get("groups") or []) if isinstance(g, dict)]
        raw_mid = (model_arg.partition(":")[2] if ":" in model_arg and norm(model_arg.partition(":")[0]) == norm(a.provider) else model_arg) if model_arg else None
        if groups:
            target_g = _find_provider_group(p, group_arg=group_arg, model_arg=model_arg, catalog=catalog)
            if target_g is None and model_arg and not group_arg and (individual or not partitions_quota(p)):
                # Another per-model cap on an ungrouped account, or an individual-quota carrier for a model not in any group yet.
                cid = _catalog_name_map(catalog).get(norm(raw_mid)) or raw_mid
                target_g = {"name": cid, "models": [cid]}
                groups.append(target_g)
            if target_g is None:
                print(f"error: {a.provider}: no matching quota group for {group_arg or model_arg}")
                _action("rerun exhaust with a --group name or --model id listed in the provider's groups in assets.yaml")
                return 1
            if individual:
                cid = _catalog_name_map(catalog).get(norm(raw_mid)) or raw_mid
                same = _model_keys(cid, _rung_model(catalog, cid))
                old = {k: v for k, v in (target_g.get("exhausted_models") or {}).items() if norm(k) not in same}
                target_g["exhausted_models"] = {**old, cid: until}
                edit_provider(ap, a.provider, {"groups": groups}, now=now)
                print(f"{a.provider} ({cid} in {target_g.get('name')}): individually exhausted until {until}{unparsed_txt}")
                return _exhaust_next_action(a, ap)
            target_g["status"] = "exhausted"
            target_g["exhausted_until"] = until
            target_g["status_note"] = raw_note
            if not t:
                target_g["unparsed"] = True
            else:
                target_g.pop("unparsed", None)
            if partitions_quota(p):
                best = _summarize_groups(groups, now)
                st = best.get("status", "ok")
                fields = {"status": st, "groups": groups}
                clear = []
                if st == "exhausted":
                    fields["exhausted_until"] = best.get("exhausted_until") or best.get("resets_at") or until
                    clear.extend(["status_note", "unparsed"])
                else:
                    clear.extend(["exhausted_until", "status_note", "unparsed"])
                edit_provider(ap, a.provider, fields, now=now, clear_keys=clear)
            else:
                edit_provider(ap, a.provider, {"groups": groups}, now=now)
            print(
                f"{a.provider} ({target_g.get('name')}): exhausted until {until}"
                + ("" if t else " (unparsed, +1h default)")
            )
            return _exhaust_next_action(a, ap)

        # Provider has no groups block yet; seed the targeted group entry so subsequent probe merges it.
        name_map = _catalog_name_map(catalog)
        cid = (name_map.get(norm(raw_mid)) or raw_mid) if raw_mid else None
        gname = group_arg or cid or "default"
        if individual:  # the first probe moves this lock into the pool that lists the model
            seeded = {"name": gname, "models": [cid], "exhausted_models": {cid: until}}
        else:
            seeded = {
                "name": gname,
                "models": [cid] if cid else [],
                "status": "exhausted",
                "exhausted_until": until,
                "status_note": raw_note,
            }
            if not t:
                seeded["unparsed"] = True
        prov_st = (p or {}).get("status", "ok")
        edit_provider(ap, a.provider, {"status": prov_st, "groups": [seeded]}, now=now)
        print(f"{a.provider} ({gname}): {'individually ' if individual else ''}exhausted until {until}{unparsed_txt}")
        return _exhaust_next_action(a, ap)

    f = {"status": "exhausted", "exhausted_until": until, "status_note": quoted_note}
    clear = [] if not t else ["unparsed"]
    if not t:
        f["unparsed"] = "true"
    edit_provider(ap, a.provider, f, now=now, clear_keys=clear)
    print(f"{a.provider}: exhausted until {until}" + ("" if t else " (unparsed, +1h default)"))
    return _exhaust_next_action(a, ap)


# ---------- automatic quota probes ----------
def _fmt_pct(val):
    r = round(float(val), 1)
    return int(r) if r == int(r) else r


def _fmt_window_minutes(mins):
    if mins is None:
        return "unknown"
    m = int(mins)
    if m == 10080:
        return "7d"
    if m % 1440 == 0 and m >= 1440:
        return f"{m // 1440}d"
    if m % 60 == 0 and m >= 60:
        return f"{m // 60}h"
    return f"{m}m"


def probe_codex(cfg, now):
    codex_home = os.path.expanduser(cfg.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex")
    sessions_dir = os.path.join(codex_home, "sessions")
    if not os.path.isdir(sessions_dir):
        return None, f"unverified (no sessions dir at {sessions_dir})"

    # Scan today + yesterday across both UTC and local dates so midnight boundaries are covered.
    days = set()
    for ref in (now.astimezone(timezone.utc), now):
        for delta in (0, 1):
            d = (ref - timedelta(days=delta)).date()
            days.add(os.path.join(sessions_dir, f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"))

    files = []
    for day_dir in sorted(days):
        if os.path.isdir(day_dir):
            files.extend(glob.glob(os.path.join(day_dir, "rollout-*.jsonl")))
    if not files:
        return None, "unverified (no rollout logs for today/yesterday)"

    now_ts = now.timestamp()
    best_ev_ts = None
    best_windows = None
    saw_expired = False

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "token_count" not in line or "rate_limits" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    payload = ev.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    rl = payload.get("rate_limits")
                    if not isinstance(rl, dict):
                        continue
                    ev_dt = parse_time(ev.get("timestamp"), now)
                    if not ev_dt:
                        continue
                    ev_ts = ev_dt.timestamp()
                    active = []
                    for label in ("primary", "secondary"):
                        w = rl.get(label)
                        if not isinstance(w, dict) or w.get("used_percent") is None:
                            continue
                        r_at = w.get("resets_at")
                        if isinstance(r_at, (int, float)):
                            if r_at <= now_ts:
                                saw_expired = True
                                continue
                            r_dt = datetime.fromtimestamp(r_at, timezone.utc)
                        else:
                            r_dt = parse_time(r_at, now)
                            if r_dt and r_dt.timestamp() <= now_ts:
                                saw_expired = True
                                continue
                        active.append(
                            {
                                "used_percent": float(w["used_percent"]),
                                "window": _fmt_window_minutes(w.get("window_minutes")),
                                "resets_at": r_dt.astimezone(timezone.utc).isoformat(timespec="seconds") if r_dt else None,
                                "resets_ts": r_dt.timestamp() if r_dt else 0.0,
                            }
                        )
                    if active and (best_ev_ts is None or ev_ts > best_ev_ts):
                        best_ev_ts = ev_ts
                        best_windows = active
        except OSError:
            continue

    if not best_windows:
        if saw_expired:
            return {"used_percent": 0, "window": "reset", "resets_at": None}, None
        return None, "unverified (no active rate-limit window in today/yesterday logs)"

    chosen = max(best_windows, key=lambda w: (w["used_percent"], w["resets_ts"]))
    return {
        "used_percent": _fmt_pct(chosen["used_percent"]),
        "window": chosen["window"],
        "resets_at": chosen["resets_at"],
    }, None


def _read_claude_token(config_dir):
    cred_path = os.path.join(config_dir, ".credentials.json")
    if not os.path.isfile(cred_path):
        return None
    try:
        with open(cred_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth") or data.get("oauth") or {}
    if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
        return oauth["accessToken"]
    if isinstance(data.get("accessToken"), str):
        return data["accessToken"]
    return None


USAGE_CACHE_TTL = 300
USAGE_BACKOFF = 600
USAGE_BACKOFF_MAX = 86400


def _usage_cache_read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _usage_cache_write(path, entry):
    """Atomically replace one config dir's file (no read-merge, so concurrent sessions never drop another's entry).

    Holds only parsed results and timestamps, never the token.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _retry_after_seconds(value, now):
    """Seconds or HTTP-date form; unparseable falls back to USAGE_BACKOFF, capped at one day."""
    text = str(value or "").strip()
    try:
        if text.isascii() and text.isdigit():
            secs = int(text)
        else:
            dt = parsedate_to_datetime(text)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # "-0000" parses naive but means UTC
            secs = max(0, int(dt.timestamp() - now.timestamp()))
    except Exception:
        secs = USAGE_BACKOFF
    return min(secs, USAGE_BACKOFF_MAX)


def _hhmmz(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%MZ")


def probe_claude(cfg, now):
    """Return (result, note). Reuses a shared cache so concurrent sessions do not hammer the usage endpoint.

    Stale data never becomes a result: it only appears in the note, so callers treat the provider as unverified.
    """
    config_dir = os.path.expanduser(
        cfg.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    )
    token = _read_claude_token(config_dir)
    if not token:
        return None, f"unverified (no OAuth credentials in {config_dir})"

    cache_dir = cfg.get("_cache_dir")
    key = hashlib.sha256(os.path.abspath(config_dir).encode("utf-8")).hexdigest()[:16]
    cpath = os.path.join(cache_dir, f"claude-{key}.json") if cache_dir else None
    entry = _usage_cache_read(cpath) if cpath else {}
    ts = now.timestamp()
    res, fetched = entry.get("result"), entry.get("fetched_at")
    if not (isinstance(res, dict) and isinstance(fetched, (int, float))):
        res = fetched = None
    reset = parse_time(res.get("resets_at"), now) if res else None
    backoff = entry.get("backoff_until")
    backoff = backoff if isinstance(backoff, (int, float)) else 0

    def stale(reason):
        if res is None:
            return None, f"unverified ({reason})"
        return None, (
            f"unverified ({reason}); last known {res.get('used_percent')}% of {res.get('window')} "
            f"at {_hhmmz(fetched)}"
        )

    ttl = cfg.get("_cache_ttl", USAGE_CACHE_TTL)
    if res is not None and not cfg.get("_fresh") and 0 <= ts - fetched < ttl and not (reset and reset <= now):
        return dict(res), None
    if ts < backoff:
        return stale(f"HTTP 429, backing off until {_hhmmz(backoff)}")

    req = urllib.request.Request(
        ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            until = ts + _retry_after_seconds((e.headers or {}).get("Retry-After"), now)
            if cpath:
                _usage_cache_write(cpath, {**entry, "backoff_until": until})
            return stale(f"HTTP 429, backing off until {_hhmmz(until)}")
        return stale(f"HTTP {e.code}")
    except Exception:
        return stale("usage request failed")

    now_ts = now.timestamp()
    active = []
    for win_name, wkey in (("5h", "five_hour"), ("7d", "seven_day")):
        w = body.get(wkey)
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        r_dt = parse_time(w.get("resets_at"), now)
        if r_dt and r_dt.timestamp() <= now_ts:
            continue
        active.append(
            {
                "used_percent": float(w["utilization"]),
                "window": win_name,
                "resets_at": r_dt.astimezone(timezone.utc).isoformat(timespec="seconds") if r_dt else None,
                "resets_ts": r_dt.timestamp() if r_dt else 0.0,
            }
        )

    if not active:
        return None, "unverified (no active usage windows returned)"

    chosen = max(active, key=lambda w: (w["used_percent"], w["resets_ts"]))
    result = {
        "used_percent": _fmt_pct(chosen["used_percent"]),
        "window": chosen["window"],
        "resets_at": chosen["resets_at"],
    }
    if cpath:
        _usage_cache_write(cpath, {"result": result, "fetched_at": ts, "backoff_until": 0})
    return result, None


def _catalog_name_map(catalog):
    by_norm = {}
    for m in (catalog or {}).get("models") or []:
        mid = m.get("id")
        if not mid:
            continue
        by_norm[norm(mid)] = mid
        if m.get("name"):
            by_norm[norm(m["name"])] = mid
        for alias in (m.get("aliases") or []):
            if alias:
                by_norm[norm(alias)] = mid
    return by_norm


def _extract_group_models(group, name_map):
    raw_names = []
    if isinstance(group.get("models"), list) and group["models"]:
        raw_names = [str(x).strip() for x in group["models"] if str(x).strip()]
    else:
        desc = str(group.get("description") or "")
        m = re.search(r"Models within this group:\s*([^\n]+)", desc, re.IGNORECASE)
        if m:
            tail = m.group(1).strip().rstrip(".")
            raw_names = [part.strip() for part in tail.split(",") if part.strip()]
        else:
            gname = str(group.get("name") or "").strip()
            if gname:
                raw_names = [gname]

    mapped, unmatched = [], []
    seen = set()
    for raw in raw_names:
        cid = name_map.get(norm(raw))
        val = cid if cid else raw
        if not cid:
            unmatched.append(raw)
        if val not in seen:
            seen.add(val)
            mapped.append(val)
    return mapped, unmatched


def _quota_status(used_percent, threshold, exhausted_threshold=DEFAULT_EXHAUSTED_THRESHOLD):
    if used_percent >= exhausted_threshold:
        return "exhausted"
    if used_percent >= threshold:
        return "low"
    return "ok"


def _summarize_groups(groups, now):
    def _sort_key(g):
        st_rank = STATUS_RANK.get(g.get("status", "ok"), 0)
        r_dt = parse_time(g.get("exhausted_until") or g.get("resets_at"), now)
        r_ts = r_dt.timestamp() if r_dt else float("inf")
        pct = float(g.get("used_percent") or 0)
        if st_rank == STATUS_RANK["exhausted"]:
            return (st_rank, r_ts, pct)
        return (st_rank, pct, r_ts)

    return min([g for g in groups if not _is_lock_carrier(g)] or groups, key=_sort_key)


def _is_error_exhaustion(entry):
    if not isinstance(entry, dict) or entry.get("status") != "exhausted":
        return False
    if not entry.get("exhausted_until"):
        return False
    return bool(
        entry.get("status_note")
        or entry.get("unparsed")
        or entry.get("exhausted_until") != entry.get("resets_at")
    )


def _scope_recovered_in_probe(prev, probed_status, probed_pct, probed_window, probed_resets_at, now):
    if probed_status == "exhausted":
        return False
    if probed_window == "reset" or float(probed_pct) == 0.0:
        return True
    prev_pct = prev.get("used_percent")
    if isinstance(prev_pct, (int, float)) and not isinstance(prev_pct, bool):
        if float(probed_pct) < float(prev_pct):
            return True
    prev_win = prev.get("window")
    prev_reset = parse_time(prev.get("resets_at"), now)
    new_reset = parse_time(probed_resets_at, now)
    if prev_win and prev_win == probed_window and prev_reset and new_reset and new_reset > prev_reset:
        return True
    return False


def _keep_error_exhaustion(prev, probed_status, probed_pct, probed_window, probed_resets_at, now):
    if not _is_error_exhaustion(prev):
        return False
    until = parse_time(prev.get("exhausted_until"), now)
    if not until or until <= now:
        return False
    return not _scope_recovered_in_probe(prev, probed_status, probed_pct, probed_window, probed_resets_at, now)


def parse_antigravity_usage(data, now, catalog=None, group_filter=""):
    raw_groups = None
    if isinstance(data, dict):
        cmd = data.get("command")
        if isinstance(cmd, dict) and isinstance(cmd.get("data"), dict):
            raw_groups = cmd["data"].get("groups")
        if raw_groups is None:
            raw_groups = data.get("groups") or data.get("quotas")
    elif isinstance(data, list):
        raw_groups = data

    if not isinstance(raw_groups, list):
        return None, "unverified (agy /usage JSON format unparsed)"

    name_map = _catalog_name_map(catalog)
    now_ts = now.timestamp()
    flt = (group_filter or "").lower()
    groups = []
    unmatched_all = []
    seen_unmatched = set()

    for item in raw_groups:
        if not isinstance(item, dict):
            continue
        gname = str(item.get("name") or item.get("group") or "").strip()
        if flt and flt not in gname.lower():
            continue
        buckets = item.get("buckets")
        if not isinstance(buckets, list):
            continue

        active = []
        saw_expired = False
        for b in buckets:
            if not isinstance(b, dict) or b.get("disabled"):
                continue
            rem = b.get("remaining_fraction")
            if isinstance(rem, bool) or not isinstance(rem, (int, float)):
                continue
            r_dt = parse_time(b.get("reset_time") or b.get("resets_at"), now)
            if r_dt and r_dt.timestamp() <= now_ts:
                saw_expired = True
                continue
            raw_win = str(b.get("window") or "unknown").strip().lower()
            win = _WINDOW_ALIAS.get(raw_win, raw_win)
            used_pct = max(0.0, min(100.0, 100.0 - float(rem) * 100.0))
            active.append(
                {
                    "used_percent": used_pct,
                    "window": win,
                    "resets_at": r_dt.astimezone(timezone.utc).isoformat(timespec="seconds") if r_dt else None,
                    "resets_ts": r_dt.timestamp() if r_dt else 0.0,
                }
            )

        if active:
            chosen = max(active, key=lambda w: (w["used_percent"], w["resets_ts"]))
            g_used = _fmt_pct(chosen["used_percent"])
            g_win = chosen["window"]
            g_reset = chosen["resets_at"]
        elif saw_expired:
            g_used = 0
            g_win = "reset"
            g_reset = None
        else:
            continue

        models, unmatched = _extract_group_models(item, name_map)
        for u in unmatched:
            if u not in seen_unmatched:
                seen_unmatched.add(u)
                unmatched_all.append(u)
        groups.append(
            {
                "name": gname or "default",
                "models": models,
                "used_percent": g_used,
                "window": g_win,
                "resets_at": g_reset,
            }
        )

    if not groups:
        return None, "unverified (agy /usage JSON format unparsed)"
    return {"groups": groups, "unmatched": unmatched_all}, None


def probe_antigravity(cfg, now, catalog=None):
    if not shutil.which("agy"):
        return None, "unverified (agy CLI not installed)"
    try:
        res = subprocess.run(
            ["agy", "-p", "/usage", "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return None, "unverified (agy timed out; complete first sign-in in the interactive agy TUI)"
    except Exception:
        return None, "unverified (agy invocation failed)"
    if res.returncode != 0 or not res.stdout.strip():
        return None, f"unverified (agy exited {res.returncode})"
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None, "unverified (agy output is not JSON)"

    return parse_antigravity_usage(data, now, catalog=catalog, group_filter=cfg.get("group") or "")


def _infer_probe(p):
    if isinstance(p.get("probe"), dict) and p["probe"].get("kind"):
        return p["probe"]
    if p.get("pay_per_use") or p.get("policy") == "avoid":
        return None
    pid = (p.get("id") or "").lower()
    if pid == "codex":
        return {"kind": "codex"}
    if pid == "claude":
        return {"kind": "claude", "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"}
    if pid == "antigravity":
        return {"kind": "antigravity"}
    return None


def probe(a):
    _begin(a)
    ap = a.assets or def_assets()
    if os.path.exists(ap) or _opt_bool(a, "init"):
        return _probe(a, ap)
    print(f"[note] no assets file at {ap}: probing with defaults without saving (pass --init to create it)")
    tmp_dir = tempfile.mkdtemp(prefix="orch-probe-")
    try:
        return _probe(a, os.path.join(tmp_dir, "assets.yaml"), cache_home=ap, unsaved=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _probe(a, ap, cache_home=None, unsaved=False):
    now = get_now(a.now)
    catalog_path = a.catalog or DEF_CATALOG
    catalog = load_catalog_with_local(catalog_path, ap) if os.path.exists(catalog_path) else {"models": []}
    existed = os.path.exists(ap)
    ensure_assets(ap, now)
    assets = load_file(ap) or {}
    providers = list(assets.get("providers") or [])

    # Seed defaults when probing against a brand-new empty assets file.
    if not existed and not providers:
        if os.path.isdir(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")):
            providers.append({"id": "codex", "probe": {"kind": "codex"}})
        cdir = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        if os.path.isfile(os.path.expanduser(os.path.join(cdir, ".credentials.json"))):
            providers.append({"id": "claude", "probe": {"kind": "claude", "config_dir": cdir}})
        if shutil.which("agy"):
            providers.append({"id": "antigravity", "probe": {"kind": "antigravity"}})

    prefs = (assets.get("preferences") or {})
    low_arg = _opt_num(a, "low_threshold")
    threshold = low_arg if low_arg is not None else prefs.get("low_threshold", DEFAULT_LOW_THRESHOLD)
    ex_arg = _opt_num(a, "exhausted_threshold")
    exhausted_threshold = (
        ex_arg if ex_arg is not None else prefs.get("exhausted_threshold", DEFAULT_EXHAUSTED_THRESHOLD)
    )

    lines = []  # (text, shown in compact mode); compact keeps recoveries, errors and non-ok providers
    tally = {}

    def emit(text, compact=True, short=None):
        """`compact` False hides the line unless --full; `short` replaces its text in compact mode."""
        lines.append((text, compact, short))

    # 1. Auto-clear expired exhausted entries (provider and group level) and persist them once per scope.
    for p in providers:
        pid = p.get("id")
        if not pid:
            continue
        groups = p.get("groups")
        has_groups = isinstance(groups, list) and bool(groups)
        cleared_names = []
        for g in (groups if has_groups else []):
            if not isinstance(g, dict) or g.get("status") != "exhausted":
                continue
            g_until = parse_time(g.get("exhausted_until") or g.get("resets_at"), now)
            if g_until and g_until <= now:
                g["status"] = "ok"
                g.pop("exhausted_until", None)
                g.pop("status_note", None)
                g.pop("unparsed", None)
                g_reset = parse_time(g.get("resets_at"), now)
                if g_reset and g_reset <= now:
                    g.pop("resets_at", None)
                cleared_names.append(g.get("name") or "group")
        cleared_locks = []
        for g in list(groups if has_groups else []):
            locks = g.get("exhausted_models") if isinstance(g, dict) else None
            if isinstance(locks, dict) and locks:
                carrier = _is_lock_carrier(g)
                live = {k: v for k, v in locks.items() if (parse_time(v, now) or now) > now}
                cleared_locks.extend(k for k in locks if k not in live)
                if live:
                    g["exhausted_models"] = live
                else:
                    g.pop("exhausted_models", None)
                    if carrier:
                        groups.remove(g)
        if cleared_locks:
            has_groups = bool(groups)
            edit_provider(ap, pid, {"groups": groups} if groups else {}, now=now, clear_keys=() if groups else ("groups",))
            for mid in cleared_locks:
                emit(f"{pid} ({mid}): expired individual lockout cleared -> ok")

        p_until = parse_time(p.get("exhausted_until") or p.get("resets_at"), now)
        p_expired = p.get("status") == "exhausted" and p_until is not None and p_until <= now
        # A provider lock that only mirrored an expiring group needs no separate recovery line.
        report_p = p_expired and (bool(p.get("status_note") or p.get("unparsed")) or not cleared_names)
        if has_groups and partitions_quota(p):
            # Provider status summarizes its groups; a live provider-level error lockout is kept until it expires.
            if cleared_names or p_expired:
                best = _summarize_groups([g for g in groups if isinstance(g, dict)], now)
                keep_p_err = _is_error_exhaustion(p) and not p_expired
                fields = {"groups": groups}
                clear = []
                if keep_p_err:
                    st_after = "exhausted"
                else:
                    st_after = best.get("status", "ok")
                    clear.extend(["status_note", "unparsed"])
                    best_until = best.get("exhausted_until") or best.get("resets_at")
                    if st_after == "exhausted" and best_until:
                        fields["exhausted_until"] = best_until
                    else:
                        clear.append("exhausted_until")
                fields["status"] = st_after
                p_reset = parse_time(p.get("resets_at"), now)
                if p_reset and p_reset <= now:
                    clear.append("resets_at")
                edit_provider(ap, pid, fields, now=now, clear_keys=clear)
                p.update(fields)
                for ck in clear:
                    if ck not in fields:
                        p.pop(ck, None)
                if report_p:
                    emit(f"{pid}: expired lockout cleared -> {st_after}")
                for gname in cleared_names:
                    emit(f"{pid} ({gname}): expired lockout cleared -> ok")
            continue

        # Ungrouped account, or per-model cap groups layered on the account-level status.
        if cleared_names:
            edit_provider(ap, pid, {"groups": groups}, now=now)
            for gname in cleared_names:
                emit(f"{pid} ({gname}): expired lockout cleared -> ok")
        if p_expired:
            clear = ["exhausted_until", "status_note", "unparsed"]
            p_reset = parse_time(p.get("resets_at"), now)
            if p_reset and p_reset <= now:
                clear.append("resets_at")
            edit_provider(ap, pid, {"status": "ok"}, now=now, clear_keys=clear)
            p["status"] = "ok"
            for ck in clear:
                p.pop(ck, None)
            if report_p:
                emit(f"{pid}: expired lockout cleared -> ok")

    # 2. Run probes.
    targets = [p for p in providers if (not a.provider or p.get("id") == a.provider)]
    if a.provider and not targets:
        targets = [{"id": a.provider}]

    worst = 0
    probers = {"codex": probe_codex, "claude": probe_claude, "antigravity": probe_antigravity}
    for p in targets:
        pid = p.get("id")
        cfg = _infer_probe(p)
        if not cfg:
            continue
        kind = cfg.get("kind")
        fn = probers.get(kind)
        if not fn:
            emit(f"{pid}: unverified (unknown probe kind {kind})")
            tally["unverified"] = tally.get("unverified", 0) + 1
            continue
        if kind == "claude":
            ttl = prefs.get("usage_cache_ttl_seconds")
            ttl = ttl if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0 else USAGE_CACHE_TTL
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(cache_home or ap)), "usage-cache")
            cfg = {**cfg, "_cache_dir": cache_dir, "_fresh": _opt_bool(a, "fresh"), "_cache_ttl": ttl}
        result, err = fn(cfg, now, catalog) if kind == "antigravity" else fn(cfg, now)
        if err:
            emit(f"{pid}: {err}")
            tally["unverified"] = tally.get("unverified", 0) + 1
            continue

        if "groups" in result:
            all_groups = [eg for eg in (p.get("groups") or []) if isinstance(eg, dict)]
            existing_groups = [eg for eg in all_groups if not _is_lock_carrier(eg)]
            g_model_map = {}
            for eg in existing_groups:
                for em in eg.get("models") or []:
                    g_model_map[norm(em)] = eg
            matched_ids = set()
            persisted_groups = []
            for g in result["groups"]:
                prev_g = next(
                    (eg for eg in existing_groups if norm(eg.get("name")) == norm(g["name"])),
                    None,
                )
                if prev_g is None:
                    for gm in g.get("models") or []:
                        cand_eg = g_model_map.get(norm(gm))
                        if cand_eg is not None and id(cand_eg) not in matched_ids:
                            prev_g = cand_eg
                            break
                if prev_g is not None:
                    matched_ids.add(id(prev_g))

                gst = _quota_status(g["used_percent"], threshold, exhausted_threshold)
                keep_g_err = prev_g is not None and _keep_error_exhaustion(
                    prev_g, gst, g["used_percent"], g["window"], g.get("resets_at"), now
                )
                if keep_g_err:
                    gst = "exhausted"
                entry = {
                    "name": g["name"],
                    "models": g["models"],
                    "status": gst,
                    "used_percent": g["used_percent"],
                    "window": g["window"],
                }
                if g.get("resets_at"):
                    entry["resets_at"] = g["resets_at"]
                if keep_g_err:
                    entry["exhausted_until"] = prev_g["exhausted_until"]
                    if prev_g.get("status_note"):
                        entry["status_note"] = prev_g["status_note"]
                    if prev_g.get("unparsed"):
                        entry["unparsed"] = True
                persisted_groups.append(entry)

            if cfg.get("group"):
                for eg in existing_groups:
                    if id(eg) not in matched_ids:
                        persisted_groups.append(eg)
            # Individual-quota model locks outlive the probe until they expire: each moves to the pool that lists its model,
            # else to a one-model carrier entry that the provider summary ignores.
            carriers = []
            for eg in all_groups:
                for mid, until in (eg.get("exhausted_models") or {}).items():
                    if not _locked(eg, mid, None, now):
                        continue
                    tgt = next((g for g in persisted_groups if norm(mid) in {norm(x) for x in g.get("models") or []}), None)
                    if tgt is None:
                        tgt = {"name": mid, "models": [mid]}
                        carriers.append(tgt)
                    tgt["exhausted_models"] = {**(tgt.get("exhausted_models") or {}), mid: until}

            best = _summarize_groups(persisted_groups, now)
            st = best["status"]
            r_at = best.get("exhausted_until") or best.get("resets_at")

            keep_prov_err = False
            if _is_error_exhaustion(p):
                p_until = parse_time(p.get("exhausted_until"), now)
                if p_until and p_until > now:
                    prev_by_name = {norm(eg.get("name")): eg for eg in existing_groups if eg.get("name")}
                    all_zero = all(
                        g["status"] != "exhausted" and (g["window"] == "reset" or float(g["used_percent"]) == 0.0)
                        for g in persisted_groups
                    )
                    def _group_recovered(g):
                        prev_g = prev_by_name.get(norm(g["name"]))
                        if prev_g is None or g["status"] == "exhausted":
                            return False
                        if prev_g.get("used_percent") is None:
                            # A group locked without usage data that the probe now shows usable has recovered.
                            return prev_g.get("status") == "exhausted"
                        return float(prev_g.get("used_percent") or 0) > 0 and _scope_recovered_in_probe(
                            prev_g, g["status"], g["used_percent"], g["window"], g.get("resets_at"), now
                        )

                    any_group_recovered = any(_group_recovered(g) for g in persisted_groups)
                    if not (all_zero or any_group_recovered):
                        keep_prov_err = True
                        st = "exhausted"

            if st == "exhausted":
                worst = max(worst, 4)
            elif st == "low":
                worst = max(worst, 3)

            fields = {
                "status": st,
                "used_percent": best["used_percent"],
                "window": best["window"],
                "groups": persisted_groups + carriers,
            }
            clear = []
            if best.get("resets_at"):
                fields["resets_at"] = best["resets_at"]
            else:
                clear.append("resets_at")
            if keep_prov_err:
                fields["exhausted_until"] = p["exhausted_until"]
            elif st == "exhausted" and r_at:
                fields["exhausted_until"] = r_at
                clear.extend(["status_note", "unparsed"])
            else:
                clear.extend(["exhausted_until", "status_note", "unparsed"])

            edit_provider(ap, pid, fields, now=now, clear_keys=clear)
            usable = sum(1 for g in persisted_groups if g["status"] != "exhausted")
            eff_reset = p["exhausted_until"] if keep_prov_err else r_at
            reset_msg = f", resets {eff_reset}" if (st == "exhausted" and eff_reset) else ""
            bad = [g["name"] for g in persisted_groups if g["status"] != "ok"]
            head = f"{pid}: {st} ({usable}/{len(persisted_groups)} groups usable{reset_msg})"
            emit(head, st != "ok" or bool(bad), head + (f" [not ok: {', '.join(bad)}]" if bad else ""))
            tally[st] = tally.get(st, 0) + 1
            for g in persisted_groups:
                g_eff_reset = g.get("exhausted_until") or g.get("resets_at")
                gr = f", resets {g_eff_reset}" if g_eff_reset else ""
                ms = ", ".join(g.get("models") or [])
                emit(f"  group {g['name']}: {g['status']} ({g['used_percent']}% of {g['window']}{gr}) [{ms}]", False)
            unmatched = result.get("unmatched") or []
            if unmatched:
                emit(f"  note: unmatched model names kept as-is: {', '.join(unmatched)}", False)
            continue

        pct = result["used_percent"]
        win = result["window"]
        r_at = result.get("resets_at")
        st = _quota_status(pct, threshold, exhausted_threshold)
        keep_err = _keep_error_exhaustion(p, st, pct, win, r_at, now)
        if keep_err:
            st = "exhausted"
        if st == "exhausted":
            worst = max(worst, 4)
        elif st == "low":
            worst = max(worst, 3)

        fields = {"status": st, "used_percent": pct, "window": win}
        clear = []
        if r_at:
            fields["resets_at"] = r_at
        else:
            clear.append("resets_at")
        if keep_err:
            fields["exhausted_until"] = p["exhausted_until"]
        elif st == "exhausted" and r_at:
            fields["exhausted_until"] = r_at
            clear.extend(["status_note", "unparsed"])
        else:
            clear.extend(["exhausted_until", "status_note", "unparsed"])

        edit_provider(ap, pid, fields, now=now, clear_keys=clear)
        eff_reset = p["exhausted_until"] if keep_err else r_at
        reset_msg = f", resets {eff_reset}" if eff_reset else ""
        emit(f"{pid}: {st} ({pct}% of {win}{reset_msg})", st != "ok")
        tally[st] = tally.get(st, 0) + 1

    for text in _capped([t if _FULL or not short else short for t, compact, short in lines if _FULL or compact]):
        print(text)
    updated_assets = load_file(ap) or {}
    avail_str = _opt_str(a, "available")
    avail = [x.strip() for x in avail_str.split(",") if x.strip()] if avail_str else None
    n_due = print_followups_status(catalog, updated_assets, now, available=avail, due_only=True)
    total = sum(tally.values())
    summary = ", ".join(f"{tally[k]} {k}" for k in ("ok", "low", "exhausted", "unverified") if tally.get(k))
    _action(
        (f"probed {total} provider(s) ({summary}); " if total else "no provider was probed; ")
        + ("results not saved (no assets file): offer probe --init to the user, then run select" if unsaved else "run select to apply them")
        + (f"; {n_due} follow-up(s) due above" if n_due else "")
    )

    return worst


def followups_cmd(a):
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    catalog_path = _opt_str(a, "catalog") or DEF_CATALOG
    catalog = load_catalog_with_local(catalog_path, ap) if os.path.exists(catalog_path) else {"models": []}
    avail_str = _opt_str(a, "available")
    avail = [x.strip() for x in avail_str.split(",") if x.strip()] if avail_str else None

    done_ref = _opt_str(a, "done")
    if done_ref:
        if not os.path.exists(ap):
            print(f"no assets file at {ap}")
            return 0
        assets = load_file(ap) or {}
        existing = [x for x in (assets.get("followups") or []) if isinstance(x, dict)]
        kept = [x for x in existing if str(x.get("ref") or "") != done_ref]
        removed = len(existing) - len(kept)
        write_followups(ap, kept, now=now)
        print(f"cleared {removed} followup(s) for {done_ref}")
        return 0

    if _opt_bool(a, "add"):
        kind = _opt_str(a, "kind") or "ui-review"
        if kind not in VALID_FOLLOWUP_KINDS:
            print(f"error: invalid followup kind: {kind} (expected {'|'.join(VALID_FOLLOWUP_KINDS)})")
            _action("rerun followups --add with a valid --kind")
            return 1
        ref_val = _opt_str(a, "ref")
        needs = _opt_str(a, "needs") or ("any" if kind == "resume" else "ui-ux-review")
        if not ref_val:
            print("error: followups --add requires --ref")
            _action("rerun followups --add with --ref <item>")
            return 1
        after = _opt_str(a, "after")
        if kind == "resume":
            _record_resume(ap, ref_val, needs, after, now, on_recovery=_opt_bool(a, "on_recovery"))
            return 0
        entry = {"kind": kind, "ref": ref_val, "needs": needs}
        if after:
            entry["after"] = after
        if kind == "resume" and _opt_bool(a, "on_recovery"):
            entry["on_recovery"] = True
        record_followup(ap, entry, now=now)
        print(f"followup recorded: [{kind}] {ref_val} (needs {needs})" + (f", after {after}" if after else ""))
        return 0

    assets = load_file(ap) if os.path.exists(ap) else {}
    n_due = print_followups_status(catalog, assets, now, available=avail, due_only=_opt_bool(a, "due"))
    _action(
        f"run the {n_due} due follow-up(s) above, then clear each with followups --done <ref>"
        if n_due else "no follow-up is due" + ("" if avail else "; pass --available to check this harness")
    )
    return 0


def _concrete_reset(now, *vals):
    """Earliest future concrete timestamp/date among `vals` (ignores rolling cadences like `5h` or `weekly`)."""
    dts = [dt for v in vals if (dt := parse_time(v, now)) and dt > now]
    return min(dts) if dts else None


def _cand_reset_dt(p, prov_st, g, mid, m, now, fail_on_low=False):
    """Earliest concrete time a blocked candidate becomes usable again, or None when unknown/cadence-only."""
    gst = group_state(g, now)[0] if g is not None else "ok"
    dts = [u] if (u := _locked(g, mid, m, now)) else []
    p_vals = (p.get("exhausted_until"), p.get("resets_at"), p.get("resets"), p.get("renews"))
    if g is not None and gst == "exhausted":
        dt = parse_time(g.get("exhausted_until") or g.get("resets_at"), now) or _concrete_reset(now, g.get("resets_at"), p.get("resets"), p.get("renews"))
        if not (dt and dt > now):
            return None
        dts.append(dt)
    if prov_st == "exhausted":
        dt = parse_time(p.get("exhausted_until") or p.get("resets_at"), now) or _concrete_reset(now, *p_vals)
        if dt and dt > now:
            dts.append(dt)
        elif not (g is not None and gst == "exhausted" and partitions_quota(p)):
            return None
    if not dts and fail_on_low and (prov_st == "low" or gst == "low"):
        dt = _concrete_reset(now, *([g.get("resets_at")] if g is not None else []), *p_vals)
        if dt and dt > now:
            dts.append(dt)
    return max(dts) if dts else None


def _earliest_need_reset(catalog, assets, now, need, available=None, fail_on_low=False):
    """Return `(matched, earliest_dt)` for blocked models matching `need`."""
    models = (catalog or {}).get("models") or []
    cat = {norm(m["id"]): m for m in models}
    emerg = set((catalog or {}).get("emergency_efforts", ["max", "ultra"]))
    prefs = ((assets or {}).get("preferences") or {}) if isinstance(assets, dict) else {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    matched, dts = False, []

    def _check(p, prov_st, g, pid, mid, m):
        nonlocal matched
        gname = g.get("name") if isinstance(g, dict) and not _is_lock_carrier(g) else None
        if not m or m.get("cost") == "emergency" or m.get("rating") == "unrated":
            return
        if not _model_matches_need(m, need, floors=floors, emerg=emerg, provider_id=pid, group_name=gname):
            return
        matched = True
        dt = _cand_reset_dt(p, prov_st, g, mid, m, now, fail_on_low=fail_on_low)
        if dt and dt > now:
            dts.append(dt)

    if available is not None:
        for pair in available:
            prov, _, mid = pair.partition(":")
            m = cat.get(norm(mid))
            p, prov_st, _ = provider_state(assets, prov, now)
            if not p.get("pay_per_use") and p.get("policy") != "avoid":
                _check(p, prov_st, model_group(p, mid, m), prov, mid, m)
    else:
        for p in (assets or {}).get("providers") or []:
            if not isinstance(p, dict) or not p.get("id") or p.get("pay_per_use") or p.get("policy") == "avoid":
                continue
            pid, (_, prov_st, _) = p["id"], provider_state(assets, p["id"], now)
            real_g = [g for g in (p.get("groups") or []) if isinstance(g, dict) and not _is_lock_carrier(g)]
            if real_g and partitions_quota(p):
                for g in real_g:
                    for mid in g.get("models") or []:
                        _check(p, prov_st, g, pid, mid, cat.get(norm(mid)))
            elif (fam := _provider_inferred_family(p)):
                for m in models:
                    if str(m.get("family") or "").lower() == fam:
                        _check(p, prov_st, model_group(p, m["id"], m), pid, m["id"], m)
    return matched, (min(dts) if dts else None)


def _is_land_followup(fu):
    need = str(fu.get("needs") or "").strip()
    return bool(need) and (fu.get("kind") == "preferred-review" or (fu.get("kind") == "resume" and need.startswith("!")))


def _land_ref_key(ref):
    s = str(ref or "").strip()
    return re.sub(r"[^a-z0-9]+", "-", _clean_ref(s).lower()).strip("-") if s else ""


def _done_hint(all_fus, done_ref, lead="then clear it with"):
    qref = f"'{done_ref}'" if re.search(r"[\s#'\"$]", done_ref) else done_ref
    others = [x for x in all_fus if str(x.get("ref") or "") == done_ref and not _is_land_followup(x)]
    if not others:
        return f"{lead} followups --done {qref}"
    kinds = ", ".join(dict.fromkeys(str(x.get("kind") or "other") for x in others))
    return f"then remove only this entry in assets.yaml (followups --done {qref} would also clear {kinds})"


def land_check_cmd(a):
    _begin(a)
    if a.risk in ("low", "medium"):
        _action("land now")
        return 0
    now, ap = get_now(_opt_str(a, "now")), _opt_str(a, "assets") or def_assets()
    assets = load_file(ap) if os.path.exists(ap) else {}
    cpath = _opt_str(a, "catalog") or DEF_CATALOG
    catalog = load_catalog_with_local(cpath, ap) if os.path.exists(cpath) else {"models": []}
    avail_str = _opt_str(a, "available")
    avail = [x.strip() for x in avail_str.split(",") if x.strip()] if avail_str else None
    prefs = (assets or {}).get("preferences") or {}
    raw_hh = prefs.get("land_hold_hours") if isinstance(prefs, dict) else None
    hold_hours = float(raw_hh) if (isinstance(raw_hh, (int, float)) and not isinstance(raw_hh, bool) and raw_hh >= 0) else DEFAULT_LAND_HOLD_HOURS
    all_fus = [fu for fu in (assets.get("followups") or []) if isinstance(fu, dict)]
    target_key = _land_ref_key(a.ref)
    fus = [
        (fu, str(fu.get("ref") or ""), sk == "current-task" and target_key != "current-task")
        for fu in all_fus
        if _is_land_followup(fu) and (sk := _land_ref_key(fu.get("ref"))) and (sk == target_key or sk == "current-task")
    ]
    if not fus:
        _action("land now")
        return 0
    lines, usable_pick, soon_dts, later_picks, all_needs = [], None, [], [], []
    cutoff = now + timedelta(hours=hold_hours)
    for fu, stored_ref, any_ref in fus:
        need, fol = str(fu.get("needs") or "").strip(), fu.get("fail_on_low") is True
        all_needs.append(need)
        usable = find_usable_in_available(catalog, assets, now, need, avail, fail_on_low=fol) if avail is not None else find_usable_in_assets(catalog, assets, now, need, fail_on_low=fol)
        if usable:
            status = "usable now"
            usable_pick = usable_pick or (need, stored_ref)
        else:
            matched, dt = _earliest_need_reset(catalog, assets, now, need, available=avail, fail_on_low=fol)
            if dt is not None:
                status = f"renews {dt.isoformat(timespec='seconds')}"
                if dt <= cutoff:
                    soon_dts.append(dt)
                else:
                    later_picks.append((dt, need, stored_ref))
            else:
                status = "renews unknown" if matched else "no matching model offered"
        ref_label = f"{stored_ref.strip()} (applies to any --ref)" if any_ref else stored_ref.strip()
        lines.append(f"followup: {fu.get('kind')} {ref_label} needs {need} — {status}")
    counts = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    for line in _capped([f"{line} (×{n})" if n > 1 else line for line, n in counts.items()]):
        print(line)
    if usable_pick is not None:
        need, done_ref = usable_pick
        _action(f"hold landing: run the cross-family review now (needs {need}; select --role reviewer …), {_done_hint(all_fus, done_ref)}")
        return 3
    if soon_dts:
        _action(f"hold landing until {_plus_margin(min(soon_dts))}; schedule a wakeup, run the cross-family review, then land; at the wakeup, rerun land-check")
        return 3
    if later_picks:
        dt, need, done_ref = min(later_picks, key=lambda x: x[0])
        _action(f"land now; schedule a wakeup at {_plus_margin(dt)} to run the cross-family review (needs {need}), fix forward on its findings, {_done_hint(all_fus, done_ref, lead='then')}")
        return 0
    uniq = list(dict.fromkeys(all_needs))
    needs_str = ", ".join(uniq[:3]) + (f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else "")
    _action(f"land now; the cross-family review stays queued as a post-merge follow-up (needs {needs_str}); the next probe/followups --due shows it as due; run it then and fix forward")
    return 0


def _clean_ref(ref):
    """Ledger label for an item: a short slug, never a path (references/improvement.md)."""
    label = ref.strip()
    if re.match(r"(?:[/~\\]|\.\.?/|[A-Za-z]:)", label):  # a filesystem path: keep only its last segment
        label = os.path.basename(label.rstrip("/\\"))
    return re.sub(r"[^A-Za-z0-9._#:-]+", "-", label).strip("-")[:64] or "item"


def _append_ledger(ledger_path, entry):
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _ledger_entries(ledger_path):
    """`(position, entry)` for each JSON object line of the ledger; unreadable lines and a missing file yield nothing."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for pos, line in enumerate(fh):
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    yield pos, e
    except OSError:
        return


_entry_role = lambda e: e.get("role") if e.get("role") in VALID_ROLES else "implementer"  # noqa: E731


def log_cmd(a):
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    ledger_path = _opt_str(a, "ledger") or def_ledger(ap)

    outcome = _opt_str(a, "outcome")
    event = _opt_str(a, "event") or ("complete" if outcome else "select")
    if event not in ("select", "complete"):
        print(f"error: invalid event: {event}")
        _action("rerun log with a valid --event select|complete")
        return 1
    if outcome is not None and outcome not in VALID_OUTCOMES:
        print(f"error: invalid outcome: {outcome}")
        _action("rerun log with --outcome " + "|".join(VALID_OUTCOMES))
        return 1
    surface = _opt_str(a, "surface") or "subagent"
    if surface not in VALID_SURFACES:
        print(f"error: invalid surface: {surface}")
        _action("rerun log with a valid --surface subagent|conversation")
        return 1

    raw_repo = _opt_str(a, "repo") or os.getcwd()
    repo = os.path.basename(os.path.normpath(raw_repo)) or "unknown"

    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "event": event,
        "repo": repo,
        "surface": surface,
        "complexity": a.complexity,
        "risk": a.risk,
        "ui": bool(a.ui),
        "provider": a.provider,
        "model": a.model,
        "effort": a.effort,
        "reviewer": _opt_str(a, "reviewer"),
        "outcome": outcome,
        "findings": _opt_int(a, "findings"),
    }
    if _opt_bool(a, "provisional"):
        entry["provisional"] = True
    if _opt_int(a, "unflagged") is not None:  # orchestrator: findings exposing a gap in its brief or plan (references/escalation.md)
        entry["unflagged"] = _opt_int(a, "unflagged")
    ref = _opt_str(a, "ref")
    if ref:
        entry["ref"] = _clean_ref(ref)
    fclass = _opt_str(a, "failure_class")
    if fclass:
        if fclass not in VALID_FAILURE_CLASSES:
            print(f"error: invalid failure class: {fclass}")
            _action("rerun log with --failure-class " + "|".join(VALID_FAILURE_CLASSES))
            return 1
        entry["failure_class"] = fclass
    role = _opt_str(a, "role") or "implementer"
    if role not in VALID_ROLES:
        print(f"error: invalid role: {role}")
        _action("rerun log with --role " + "|".join(VALID_ROLES))
        return 1
    entry["role"] = role
    rubric_raw = _opt_str(a, "rubric")
    if rubric_raw is not None:
        rubric = _parse_rubric(rubric_raw)
        if rubric is None:
            print(f"error: invalid rubric: {rubric_raw}")
            _action("rerun log with --rubric size=N,uncertainty=N,deps=N,consequences=N,verification=N,coordination=N (N in 0..2)")
            return 1
        entry["rubric"] = rubric
    requested = _opt_str(a, "requested")
    effective = _opt_str(a, "effective")
    cat = None
    if requested is not None or effective is not None:
        cat_path = _opt_str(a, "catalog") or DEF_CATALOG
        cat = load_catalog_with_local(cat_path, ap) if os.path.exists(cat_path) else {"models": []}
    if requested is not None:
        canon_req = _canon_pme(requested, cat)
        if not canon_req:
            print(f"error: invalid --requested: {requested} (expected provider:model@effort or provider/model @ effort)")
            _action("rerun log with --requested provider:model@effort")
            return 1
        entry["requested"] = canon_req
    if effective is not None:
        canon_eff = _canon_pme(effective, cat)
        if not canon_eff:
            print(f"error: invalid --effective: {effective} (expected provider:model@effort or provider/model @ effort)")
            _action("rerun log with --effective provider:model@effort")
            return 1
        entry["effective"] = canon_eff
    mismatch_msg = None
    if "requested" in entry and "effective" in entry and (
            _canon_pme(effective, cat, key=True) != _canon_pme(requested, cat, key=True)):
        entry["mismatch"] = True
        mismatch_msg = f"warning: effective {entry['effective']} differs from requested {entry['requested']}; check the thread settings"

    dup = None
    if event == "complete" and entry.get("ref") and os.path.exists(ledger_path):
        prior = read_ledger_refs(ledger_path)[0].get((entry["ref"], role), {}).get("complete")
        if prior:
            dup = (f"warning: {entry['ref']} ({role}) already has a complete for this attempt (outcome {prior.get('outcome')}); "
                   "history keeps the latest — log one complete per attempt, after the review gate")
    _append_ledger(ledger_path, entry)
    if dup:
        print(dup)
    if mismatch_msg:
        print(mismatch_msg)
    print(f"logged {event} ({repo}) -> {ledger_path}")
    return 0


def read_ledger_refs(ledger_path):
    """`(runs, unreferenced)` from the ledger: per `(ref, role)` the latest attempt and its file position.

    A `select` starts a new attempt only for its own role (dropping that role's earlier completion and reviewer), so a
    reviewer or orchestrator logged under the item's ref never hides the implementer's outcome. Entries without `role`
    count as `implementer`.
    """
    runs, unreferenced = {}, 0
    for pos, e in _ledger_entries(ledger_path):
        ref = e.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            unreferenced += 1
            continue
        if e.get("event") == "escalate":  # counted by `escalate` and `history --by`, not an attempt
            continue
        r = runs.setdefault((_clean_ref(ref), _entry_role(e)), {})
        r["pos"] = pos
        if e.get("event") == "select":  # a new attempt (retry, other model) of this role starts clean
            r.pop("complete", None)
            r.pop("reviewer", None)
        if e.get("event") in ("select", "complete"):
            r[e["event"]] = e
        if e.get("reviewer"):
            r["reviewer"] = e["reviewer"]
    return runs, unreferenced


def history_line(ref, run, followups, role="implementer"):
    """One line per ref and role: model, reviewer, outcome, findings, provisional flag, pending follow-up kinds."""
    sel, done = run.get("select") or {}, run.get("complete") or {}
    who = sel or done
    parts = [f"{who.get('provider')}:{who.get('model')} @ {who.get('effort')}" if who else "no select logged"]
    parts.append(f"reviewer {run.get('reviewer') or 'none'}")
    outcome = done.get("outcome") or "open"
    if done.get("failure_class"):
        outcome += f" ({done['failure_class']})"
    if done.get("findings") is not None:
        outcome += f", {done['findings']} finding(s)"
    parts.append(outcome)
    if sel.get("provisional"):
        parts.append("provisional")
    if followups:
        parts.append("pending " + ", ".join(str(fu.get("kind", "review")) for fu in followups))
    if role != "implementer":
        parts.append(f"role {role}")
    if sel.get("mismatch") or done.get("mismatch"):
        parts.append("effective differs")
    return f"{ref}: " + "; ".join(parts)


def history_cmd(a):
    _begin(a)
    ap = _opt_str(a, "assets") or def_assets()
    ledger_path = _opt_str(a, "ledger") or def_ledger(ap)
    want = _clean_ref(_opt_str(a, "ref")) if _opt_str(a, "ref") else None
    last = _opt_int(a, "last")
    want_role = _opt_str(a, "role")
    if _opt_str(a, "by") is not None:
        return history_by(a, ledger_path, ap, want_role)
    runs, unreferenced = read_ledger_refs(ledger_path)
    try:
        assets = load_file(ap) if os.path.exists(ap) else {}
    except (OSError, ValueError):
        assets = {}
    pending = {}  # cleaned ref -> that ref's follow-ups still in assets
    for fu in (assets or {}).get("followups") or []:
        if isinstance(fu, dict) and fu.get("ref") is not None:
            pending.setdefault(_clean_ref(str(fu["ref"])), []).append(fu)
    keys = sorted(runs, key=lambda k: runs[k]["pos"], reverse=True)  # (ref, role), most recent first
    if want:
        keys = [k for k in keys if k[0] == want]
    if want_role:
        keys = [k for k in keys if k[1] == want_role]
    if last is not None:  # the N most recent refs, with every role line of each
        keep = set(list(dict.fromkeys(ref for ref, _ in keys))[: max(last, 0)])
        keys = [k for k in keys if k[0] in keep]
    shown = set()  # refs whose pending follow-ups are listed (once, on the ref's most recent line)
    lines = []
    for ref, role in keys:
        fus = pending.get(ref) if ref not in shown else None
        shown.add(ref)
        lines.append(history_line(ref, runs[(ref, role)], fus, role))
    logged = {k[0] for k in runs}
    for ref in sorted(pending):  # follow-ups whose item has no ledger entry
        if ref not in logged and (not want or ref == want) and last is None and not want_role:
            shown.add(ref)
            lines.append(f"{ref}: no ledger entry; pending " + ", ".join(str(fu.get("kind", "review")) for fu in pending[ref]))
    if want and not lines:
        print(f"error: no ledger entry or follow-up for ref {want}")
        print("error: 1 problem(s); action: rerun history without --ref to list the refs")
        return 1
    for line in _capped(lines):
        print(line)
    n_pending = sum(len(v) for k, v in pending.items() if k in shown)
    tail = f"ok: {len(shown)} ref(s); {n_pending} pending follow-up(s)"  # one line per ref and role
    if unreferenced and not want:
        tail += f"; {unreferenced} ledger entr{'y has' if unreferenced == 1 else 'ies have'} no ref (log with --ref to track them)"
    print(tail)
    return 0


def history_by(a, ledger_path, ap, role):
    """`history --role R --by model,effort`: per provider:model@effort of that role's attempts (a `select` starts one, as in
    read_ledger_refs), the items, rework rate, findings (and unflagged) averages, escalations away from it, and cost class."""
    if _opt_str(a, "by") != "model,effort" or not role:
        print("error: --by takes model,effort and needs --role" if role else "error: --by needs --role")
        print("error: 1 problem(s); action: rerun history with --role implementer|reviewer|orchestrator --by model,effort")
        return 1
    ignored = [f for f, on in (("--ref", bool(_opt_str(a, "ref"))), ("--last", _opt_int(a, "last") is not None)) if on]
    if ignored:
        print(f"note: ignored with --by: {', '.join(ignored)}")
    catalog = load_catalog_with_local(_opt_str(a, "catalog") or DEF_CATALOG, ap)

    shown = {}  # group key -> the most recent verbatim spelling
    names = _catalog_name_map(catalog)

    def combo(e, raw=None):
        raw = raw or f"{e.get('provider') or '?'}:{e.get('model') or '?'}@{e.get('effort') or '?'}"
        k, disp = _canon_pme(raw, catalog, key=True), _canon_pme(raw, catalog)
        if not k:
            pair, sep, eff = raw.strip().rpartition("@")
            prov, psep, mid = (pair if sep else raw).partition(":")
            prov, mid = prov.strip() or "?", (mid.strip() if psep else "") or "?"
            eff = (eff.strip().lower() if sep else "") or "?"
            canon_mid = norm(names.get(norm(mid), mid)) if mid != "?" else "?"
            k = f"{norm(prov) if prov != '?' else '?'}:{canon_mid}@{eff}"
            disp = f"{prov}:{canon_mid}@{eff}"
        shown[k] = disp
        return k

    open_by_ref, attempts, escalations = {}, [], {}
    for _, e in _ledger_entries(ledger_path):
        ref, ev = e.get("ref"), e.get("event")
        if not isinstance(ref, str) or not ref.strip() or _entry_role(e) != role:
            continue
        if ev == "escalate" and isinstance(e.get("from"), str):
            k = combo(e, e["from"])
            escalations[k] = escalations.get(k, 0) + 1
        elif ev in ("select", "complete"):
            ref = _clean_ref(ref)
            if ev == "select" or ref not in open_by_ref:
                open_by_ref[ref] = {"ref": ref}
                attempts.append(open_by_ref[ref])
                open_by_ref[ref]["key"] = combo(e)
            open_by_ref[ref][ev] = e
    stats = {}
    new = lambda: {"refs": set(), "done": 0, "rework": 0, "findings": [], "unflagged": []}  # noqa: E731
    for at in attempts:
        st = stats.setdefault(at["key"], new())
        st["refs"].add(at["ref"])
        done = at.get("complete") or {}
        if done.get("outcome") in ("ok", "rework"):  # the rework rate compares finished work only
            st["done"] += 1
            st["rework"] += done["outcome"] == "rework"
        for k in ("findings", "unflagged"):
            if _is_int(done.get(k)):
                st[k].append(done[k])
    for k in escalations:
        stats.setdefault(k, new())
    avg = lambda xs: f"{sum(xs) / len(xs):.1f}" if xs else "n/a"  # noqa: E731
    lines = []
    for k in sorted(stats, key=lambda k: (-len(stats[k]["refs"]), k)):
        st, m = stats[k], _rung_model(catalog, k.partition(":")[2].rpartition("@")[0])
        parts = [f"{len(st['refs'])} item(s)", "rework " + (f"{round(100 * st['rework'] / st['done'])}%" if st["done"] else "n/a"),
                 f"findings avg {avg(st['findings'])}"]
        if role == "orchestrator":
            parts.append(f"unflagged avg {avg(st['unflagged'])}")
        parts += [f"escalations {escalations.get(k, 0)}", f"cost {(m or {}).get('cost') or '?'}"]
        lines.append(f"{shown.get(k, k)}: " + "; ".join(parts))
    for line in _capped(lines):
        print(line)
    print(f"ok: {len(lines)} combination(s) for role {role}")
    return 0


# ---------- quality escalation (references/escalation.md) ----------
def _rung_score(m, eff):
    """Catalog score of model `m` at effort `eff`, or None: an unrated rung's score is unknown, never 0."""
    v = ((m or {}).get("scores") or {}).get(eff)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _effort_step(catalog, usable, current, m_cur, cur_sc, max_effort=None, floor=0):
    """The next higher rated non-emergency effort (<= max_effort) of the current pair, when that pair is in `usable`, scoring
    above `cur_sc` (any rated score when `cur_sc` is unknown) and meeting `floor`; None otherwise."""
    prov, _, eff = current
    cur_c = next((c for c in usable if norm(c["provider"]) == norm(prov) and c["model"]["id"] == m_cur["id"]), None)
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    hit = next(((e, v) for e, v in _rated_efforts(m_cur, emerg, max_effort=max_effort)
                if EFFORT_ORDER.get(e, 99) > EFFORT_ORDER.get(eff, 99) and v >= floor and (cur_sc is None or v > cur_sc)), None) if cur_c else None
    return {**cur_c, "effort": hit[0], "score": hit[1], "from_score": cur_sc} if hit else None


def escalation_target(catalog, assets, avail, floors, tier, now, current, exclude_model=None):
    """Next worker rung above `current` `(provider, model, effort)`, or None. (1) The same model's next higher rated effort
    meeting `floors[tier]` and scoring higher, while usable (skipped for a reviewer on `exclude_model`).
    (2) Only when the current score is known: a tier-meeting model scoring strictly higher in `candidates()` order (same
    provider first), or for a reviewer in the reviewer fallback order (tier-meeting different/same family, then below-tier
    effort step or different/same family only when no tier-meeting candidate exists), never the implementer's."""
    m_cur = _rung_model(catalog, current[1])
    if not m_cur:
        return None
    cur_sc, floor = _rung_score(m_cur, current[2]), floors[tier]
    usable, _, _ = candidates(catalog, assets, avail, 0, floors, now, pick_top=True, task_floor=floor)
    can_step = not (exclude_model and m_cur["id"] == exclude_model["id"])
    if can_step:
        step = _effort_step(catalog, usable, current, m_cur, cur_sc, floor=floor)
        if step:
            return step
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    keep = lambda c: not (norm(c["provider"]) == norm(current[0]) and c["model"]["id"] == m_cur["id"]) and not (  # noqa: E731
        exclude_model and c["model"]["id"] == exclude_model["id"])
    meet = [c for c in sorted(candidates(catalog, assets, avail, floor, floors, now)[0], key=lambda c: c["key"]) if keep(c)]
    if cur_sc is None:
        if exclude_model and can_step and not meet:
            return _effort_step(catalog, usable, current, m_cur, None, floor=0)
        return None

    def up(c, start):
        hit = next(((e, v) for e, v in _rated_efforts(c["model"], emerg) if EFFORT_ORDER.get(e, 99) >= start and v > cur_sc), None)
        return {**c, "effort": hit[0], "score": hit[1], "from_score": cur_sc} if hit else None

    if exclude_model:
        diff = lambda c: c["model"]["family"] != exclude_model["family"]  # noqa: E731
        hit = next((t for pred in (diff, lambda c: not diff(c)) for c in meet if pred(c)
                    for t in [up(c, EFFORT_ORDER.get(c["effort"], 99))] if t), None)
        if hit or meet:
            return hit
        if can_step:
            step = _effort_step(catalog, usable, current, m_cur, cur_sc, floor=0)
            if step:
                return step
        below = [c for c in sorted(usable, key=lambda c: (-c["score"], c["key"])) if keep(c)]
        return next((t for pred in (diff, lambda c: not diff(c)) for c in below if pred(c)
                     for t in [up(c, 0)] if t), None)
    ups = [t for c in meet for t in [up(c, EFFORT_ORDER.get(c["effort"], 99))] if t]
    return next((c for c in ups if norm(c["provider"]) == norm(current[0])), ups[0] if ups else None)


def _emergency_higher(catalog, assets, avail, now, cur_sc, exclude_model=None):
    """`provider:model@effort` of an offered model in a usable pool that beats `cur_sc` only at an emergency effort, or of an
    emergency model (`@<effort>` when unrated); None when there is none."""
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    for pair in avail:
        prov, _, mid = pair.partition(":")
        p, st, _ = provider_state(assets, prov, now)
        m = _rung_model(catalog, mid)
        if not m or st == "exhausted" or p.get("pay_per_use") or p.get("policy") == "avoid" or (exclude_model and m["id"] == exclude_model["id"]):
            continue
        g = model_group(p, mid, m)
        if g is not None and (group_state(g, now)[0] == "exhausted" or _locked(g, mid, m, now)):
            continue
        em_model = m.get("cost") == "emergency"
        nums = [(e, v) for e, v in (m.get("scores") or {}).items() if (em_model or e in emerg) and isinstance(v, (int, float)) and not isinstance(v, bool)]
        hit = next((e for e, v in nums if v > cur_sc), "<effort>" if em_model and not nums else None)
        if hit:
            return f"{prov}:{m['id']}@{hit}"
    return None


def _orchestrator_escalation(catalog, assets, avail, floors, coord, now, current, m_cur, cur_sc):
    """`(rung, cross_provider, notes)`: with an unknown current score only the same model's next higher rated effort (<=
    max_effort, meeting the tier floor); for explicit ladders search all surviving entries (same provider first); for auto
    ladders take the effort step first, then every usable tier-meeting same-provider candidate in rank order (no slice),
    then other-provider tier-meeting candidates in rank order."""
    oprefs, _ = _orchestrator_prefs(assets)
    tier, max_effort = oprefs["tiers"][coord], oprefs["max_effort"]
    floor, prov = floors[tier], current[0]
    if cur_sc is None:
        usable, notes, _ = candidates(catalog, assets, avail, 0, floors, now, pick_top=True, max_effort=max_effort, exclude_coordinator=True)
        return _effort_step(catalog, usable, current, m_cur, None, max_effort=max_effort, floor=floor), False, notes
    if oprefs["ladder"] != "auto":
        surviving, exp_notes = _eval_explicit_ladder(oprefs["ladder"], catalog, assets, avail, tier, floor, floors, max_effort, now)
        if surviving:
            same_ok = lambda r: not (r["model"]["id"] == m_cur["id"] and EFFORT_ORDER.get(r["effort"], 99) <= EFFORT_ORDER.get(current[2], 99))  # noqa: E731
            hit = next((r for r in surviving if norm(r["provider"]) == norm(prov) and r["score"] > cur_sc and same_ok(r)), None)
            if hit:
                return hit, False, []
            hit = next((r for r in surviving if norm(r["provider"]) != norm(prov) and r["score"] > cur_sc), None)
            return hit, True, exp_notes
    usable, _, _ = candidates(catalog, assets, avail, 0, floors, now, pick_top=True, max_effort=max_effort, exclude_coordinator=True)
    step = _effort_step(catalog, usable, current, m_cur, cur_sc, max_effort=max_effort, floor=floor)
    if step:
        return step, False, []
    cands, notes, _ = candidates(catalog, assets, avail, floor, floors, now, max_effort=max_effort, exclude_coordinator=True)
    cands.sort(key=lambda c: c["key"])
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))

    def up(c):
        hit = next(((e, v) for e, v in _rated_efforts(c["model"], emerg, max_effort=max_effort) if v >= floor and v > cur_sc), None)
        return {**c, "effort": hit[0], "score": hit[1], "from_score": cur_sc} if hit else None

    own = next((t for c in cands if norm(c["provider"]) == norm(prov) and c["model"]["id"] != m_cur["id"] for t in [up(c)] if t), None)
    if own:
        return own, False, []
    other = next((t for c in cands if norm(c["provider"]) != norm(prov) for t in [up(c)] if t), None)
    return other, True, notes


def escalate_cmd(a):
    """Quality escalation for any role: on rework, too many findings, or a child's `STATUS: ESCALATE`, the next rung strictly
    above --current (effort, then model; never down), capped per item and role, logged with its cause. An ask (unknown
    score, emergency-only, orchestrator provider switch) logs only on the rerun with --approved --to."""
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    catalog = load_catalog_with_local(_opt_str(a, "catalog") or DEF_CATALOG, ap)
    assets = load_file(ap) if os.path.exists(ap) else None
    prefs = (assets or {}).get("preferences") or {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    role, cause, findings = _opt_str(a, "role") or "implementer", _opt_str(a, "cause"), _opt_int(a, "findings")
    approved, to_raw = _opt_bool(a, "approved"), _opt_str(a, "to")

    def fix(problem, what):
        print("error: " + problem)
        _action(f"fix {what} and rerun escalate")
        return 1

    avail = [x.strip() for x in (_opt_str(a, "available") or "").split(",") if x.strip()]
    bad = bad_available_entries(avail, catalog)
    if bad:
        return fix("--available entries are not provider:model: " + ", ".join(_capped([_show_entry(x) for x in bad])),
                   "--available (comma-separated <provider>:<model-id> pairs as discovery lists them)")
    cur_raw = _opt_str(a, "current") or ""
    cur = _parse_pme(cur_raw, allow_slash=True)
    m_cur = _rung_model(catalog, cur[1]) if cur else None
    if not m_cur:
        return fix(f"--current {_show_entry(cur_raw)} is not a catalog provider:model@effort", "--current (provider:model@effort, as log --requested takes it)")
    ref = _opt_str(a, "ref")
    if role not in VALID_ROLES or cause not in VALID_CAUSES or not ref:
        return fix("--role, --cause and --ref are required", "--role implementer|reviewer|orchestrator, --cause rework|findings|status and --ref ITEM")
    if cause == "findings" and findings is None:
        return fix("--findings N is required for --cause findings", "--findings (required for --cause findings)")
    to = _parse_pme(to_raw, allow_slash=True) if to_raw else None
    m_to = _rung_model(catalog, to[1]) if to else None
    if approved != bool(to_raw) or (to_raw and not m_to):
        return fix("--approved and --to (a catalog provider:model@effort) go together", "--approved --to <provider:model@effort> (the rung the user approved)")
    impl = None
    if role == "orchestrator":
        coord = _opt_int(a, "coordination")
        if coord not in (0, 1, 2):
            return fix("--coordination 0|1|2 is required for --role orchestrator", "--coordination (0|1|2 required for --role orchestrator)")
    else:
        complexity, risk = _opt_str(a, "complexity"), _opt_str(a, "risk")
        if complexity not in TIER or risk not in ("low", "medium", "high"):
            return fix(f"--complexity and --risk are required for --role {role}", f"--complexity/--risk (required for --role {role})")
        tier = TIER[complexity][risk]
        if role == "reviewer":
            tier = reviewer_tier(tier, risk)
            impl = resolve_implementer(catalog, _opt_str(a, "implementer") or "")
            if not impl:
                return fix("--implementer (the implementing model) is required for --role reviewer and must be in the catalog",
                           "--implementer (required for --role reviewer; the implementing <model-id>)")
    cur_sc = _rung_score(m_cur, cur[2])
    if m_to:
        to_sc = _rung_score(m_to, to[2])
        if cur_sc is not None and to_sc is not None and to_sc <= cur_sc:
            return fix(f"--to {_show_entry(to_raw)} scores {to_sc}, not above the current {cur_sc}", "--to (a rung scoring above --current)")
        eff_rank = {**EFFORT_ORDER, **{e: len(EFFORT_ORDER) + i for i, e in enumerate(
            dict.fromkeys(list(catalog.get("emergency_efforts") or []) + ["max", "ultra"]))}}
        if m_to["id"] == m_cur["id"] and eff_rank.get(to[2], 99) <= eff_rank.get(cur[2], 99):
            return fix(f"--to {_show_entry(to_raw)} is the current model at the same or a lower effort", "--to (a higher effort or another model)")
        offered = avail + [f"{cur[0]}:{m_cur['id']}"]
        if not any(norm(pr.partition(":")[0]) == norm(to[0]) and (_rung_model(catalog, pr.partition(":")[2]) or {}).get("id") == m_to["id"] for pr in offered):
            return fix(f"--to pair {to[0]}:{m_to['id']} is not in --available", "--to (a provider:model@effort from --available)")
        if impl and m_to["id"] == impl["id"]:
            return fix("--to is the implementer's model", "--to (a model other than the implementer's)")
    cross_role = {"implementer": ("coordination", "implementer"), "reviewer": ("coordination",), "orchestrator": ("complexity", "risk", "implementer")}
    ignored = [f"--{k}" for k in cross_role[role] if (_opt_int(a, k) is not None if k == "coordination" else _opt_str(a, k))]
    if ignored:
        print(f"note: ignored for --role {role}: {', '.join(ignored)}")
    if not any(norm(p.partition(":")[0]) == norm(cur[0]) and (_rung_model(catalog, p.partition(":")[2]) or {}).get("id") == m_cur["id"] for p in avail):
        avail.append(f"{cur[0]}:{m_cur['id']}")
        print("note: --current pair added to --available")

    here = f"{cur[0]}/{m_cur['id']} @ {cur[2]}"
    oprefs, eprefs = _orchestrator_prefs(assets)[0], _escalation_prefs(assets)
    threshold = oprefs["unflagged_findings"] if role == "orchestrator" else eprefs["findings"]
    if cause == "findings" and findings < threshold:
        _action(f"no escalation (findings below threshold: {findings} < {threshold}); continue on {here}")
        return 0
    cap = oprefs["max_escalations"] if role == "orchestrator" else eprefs["max"]
    ref = _clean_ref(ref)
    ledger_path = _opt_str(a, "ledger") or def_ledger(ap)
    done = sum(1 for _, e in _ledger_entries(ledger_path)
               if e.get("event") == "escalate" and isinstance(e.get("ref"), str) and _clean_ref(e["ref"]) == ref and _entry_role(e) == role)
    if done >= cap:
        _action(f"escalation cap reached ({cap} of {cap}) for {ref}: report it to the user with the evidence")
        return 3

    rerun = "; once approved, rerun this escalate with --approved --to "
    if m_to:
        target_ref, target_canon = f"{to[0]}/{m_to['id']} @ {to[2]}", _canon_pme(to_raw, catalog)
        cross = role == "orchestrator" and norm(to[0]) != norm(cur[0])
    else:
        if role == "orchestrator":
            target, cross, notes = _orchestrator_escalation(catalog, assets, avail, floors, coord, now, cur, m_cur, cur_sc)
            if cause == "findings" and cur_sc is not None and (target is None or cross):
                _action(f"no same-provider rung above {here}: continue on it "
                        "(the findings trigger asks for a provider switch only when the user requests one)")
                return 0
            ask = None
        else:
            target, cross = escalation_target(catalog, assets, avail, floors, tier, now, cur, exclude_model=impl), False
            notes = [] if target else candidates(catalog, assets, avail, floors[tier], floors, now)[1]
            ask = None if target or cur_sc is None else _emergency_higher(catalog, assets, avail, now, cur_sc, exclude_model=impl)
        if target:
            target_ref = _cand_ref(target)
            target_canon = _canon_pme(f"{target['provider']}:{target['model']['id']}@{target['effort']}", catalog)
            to_sc = _rung_score(target["model"], target["effort"])
            assert to_sc is not None and (to_sc > cur_sc if cur_sc is not None else target["model"]["id"] == m_cur["id"]
                                          and EFFORT_ORDER.get(target["effort"], 99) > EFFORT_ORDER.get(cur[2], 99)), "escalate never steps down"
            if cross:
                _action(f"ask the user once before switching the orchestrator's provider to {target_ref} (phase boundary only){rerun}{target_canon}")
                return 3
        else:
            _print_notes(notes)
            if cur_sc is None:
                _action(f"ask the user once (current rung {here} has no rated score, so no higher rung can be proven){rerun}<P:M@E>")
            elif ask:
                _action(f"ask the user once (only emergency models or efforts are higher: {ask}){rerun}{ask}")
            else:
                _action("no higher rung: report it to the user with the evidence")
                return 2
            return 3
    print(f"escalate: {role} {here} -> {target_ref} (cause {cause}; escalation {done + 1} of {cap}{'; approved' if approved else ''})")
    if _opt_bool(a, "dry_run"):
        print("note: dry run: nothing logged")
    else:
        repo = os.path.basename(os.path.normpath(_opt_str(a, "repo") or os.getcwd())) or "unknown"
        entry = {"ts": now.isoformat(timespec="seconds"), "event": "escalate", "repo": repo, "role": role, "ref": ref,
                 "from": _canon_pme(cur_raw, catalog), "to": target_canon, "cause": cause}
        if findings is not None:
            entry["findings"] = findings
        if approved:
            entry["approved"] = True
        _append_ledger(ledger_path, entry)
        _detail(f"logged escalate ({repo}) -> {ledger_path}")
    _action(("at this phase boundary " if cross else "") + f"switch the {role} in place to {target_ref} "
            "(references/harnesses.md, Switch model in place) and continue; re-review after the change")
    return 0


_I = re.IGNORECASE
QUOTA_RE = re.compile(
    r"hit your (?:weekly|usage|daily|monthly|session)\b|(?:weekly|usage|daily|monthly|session) limit"
    r"|quota (?:reached|exceeded|limit)|rate[ _-]?limit|too many requests|try again (?:at|in)|resource[_ ]exhausted",
    _I,
)
INFRA_RE = re.compile(
    r"ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|socket hang up|TLS handshake timeout"
    r"|connection (?:reset|refused|timed out)|network is unreachable|(?:HTTP\S*|status(?: code)?|code)[ /:=]*50[234]\b|bad gateway|service unavailable"
    r"|gateway time-?out|runner (?:lost|preempted|shut ?down)|lost (?:communication|connection) with (?:the )?(?:server|runner)",
    _I,
)
CONTEXT_RE = re.compile(
    r"context[ _-]?(?:length|window|limit)|prompt is too long|maximum context|exceeds? the (?:maximum )?context"
    r"|max(?:imum)?[ _-]?turns|turn limit|too many tokens|token limit",
    _I,
)
TOOL_RE = re.compile(
    r"InputValidationError|tool[ _-]?use[ _-]?error|tool (?:error|call failed|execution failed)|invalid tool|unknown tool"
    r"|spawn\b.{0,80}\bENOENT|harness error",
    _I,
)


RERUN_ACTION_TEXT = (
    "rerun the failed job or command once (references/verification.md); "
    "if it fails again, diagnose it as a real failure"
)


def _parse_known(items):
    """`REGEX=>ACTION` strings -> [(compiled, regex_text, action)]; raises ValueError on a bad entry."""
    out = []
    for item in items or []:
        rx, sep, act = item.partition("=>")
        if not sep or not rx.strip() or not act.strip():
            raise ValueError(f"--known needs REGEX=>ACTION, got: {item}")
        act_text = " ".join(act.split())
        if act_text == "rerun":
            act_text = RERUN_ACTION_TEXT
        try:
            out.append((re.compile(rx.strip(), _I), rx.strip(), act_text))
        except re.error as exc:
            raise ValueError(f"--known regex {rx.strip()!r}: {exc}")
    return out


def classify_failure(text, now, known=()):
    """Return `(cls, known_hit, reset)`: class in VALID_FAILURE_CLASSES, the matching `--known` entry or None, reset datetime or None."""
    for entry in known:
        if entry[0].search(text):
            return "infra", entry, None
    reset = parse_reset(text, now) if re.search(r"\bresets?\b", text, _I) else None
    if reset or QUOTA_RE.search(text):
        return "quota", None, reset or parse_reset(text, now)
    for cls, rx in (("context-cap", CONTEXT_RE), ("infra", INFRA_RE), ("tool", TOOL_RE)):
        if rx.search(text):
            return cls, None, None
    return "unknown", None, None


def prior_failures(ledger_path, ref):
    """Retry-counting failure classes logged for `ref`, oldest first (references/supervision.md).

    A completion with `outcome: ok` and no `failure_class` ends the item's cycle and restarts the count.
    """
    seen = []
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("ref") != ref:
                    continue
                if e.get("failure_class") in RETRY_CLASSES:
                    seen.append(e["failure_class"])
                elif e.get("outcome") == "ok" and not e.get("failure_class"):
                    seen = []
    except OSError:
        pass
    return seen


def failure_cmd(a):
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    text = " ".join((_opt_str(a, "message") or "").split())
    if not _opt_str(a, "ref"):
        print("error: --ref <item> is required (a short label, no paths)")
        _action("rerun failure with --ref <item>")
        return 1
    ref = _clean_ref(_opt_str(a, "ref"))
    ap = _opt_str(a, "assets") or def_assets()
    raw_known = [k for k in (getattr(a, "known", None) or []) if isinstance(k, str)]
    repo_notes = []
    if raw_known:
        try:
            known = _parse_known(raw_known)
        except ValueError as exc:
            print(str(exc))
            print("action: fix the --known pattern (REGEX=>ACTION) and rerun failure")
            return 1
    else:
        explicit_repo = _opt_str(a, "repo")
        repo_dir = os.path.abspath(explicit_repo) if explicit_repo else _default_repo_dir()
        known, repo_notes = _load_repo_known_failures(repo_dir, explicit_repo=bool(explicit_repo))
    ledger_path = _opt_str(a, "ledger") or def_ledger(ap)
    cls, hit, reset = classify_failure(text, now, known)
    seen = prior_failures(ledger_path, ref)
    n = len(seen)
    _detail(f"class: {cls}")
    _detail(f"retries: {n}/{MAX_RETRIES}")
    for note in _capped(repo_notes):
        print(f"note: {note}")
    tag = f" --ref {ref}"
    if hit:
        _detail(f"known: {hit[1]}")
        _failure_log_hint("<ok|failed>", cls, tag)
        print(f"action: {hit[2]}")
    elif cls == "quota":
        _detail("resets: " + (reset.isoformat(timespec="seconds") if reset else "unknown"))
        _failure_log_hint("quota", cls, tag)
        print(
            f"action: quota (resets {reset.isoformat(timespec='seconds') if reset else 'unknown'}): "
            "if the limit is the model provider's, run orch.py exhaust <provider> \"<error text>\" "
            "(add --model or --group per references/quota-errors.md) and follow the quota order in references/harnesses.md; "
            "otherwise treat it as infra: rerun once"
        )
    elif cls == "infra":
        _failure_log_hint("<ok|failed>", cls, tag)
        print("action: rerun the failed job or command once (references/verification.md); if it fails again, diagnose it as a real failure")
    else:
        _failure_log_hint("failed", cls, tag)
        if n >= MAX_RETRIES:
            print(f"action: stop retrying {ref} and report it to the user with the evidence (error text, last messages, ledger lines)")
        elif cls == "context-cap":
            print(
                "action: confirm the old child stopped (references/supervision.md), then start a replacement with the partial "
                "results (branch, commit, last completed step) and a narrower scope covering only the remaining work"
            )
        elif cls == "tool" and seen[-1:] == ["tool"]:
            print("action: rerun select with the failing model removed from --available and dispatch its candidate")
        elif cls == "tool":
            print("action: retry once on the same model")
        else:
            print("action: retry once")
    return 0


def _failure_log_hint(outcome, cls, tag):
    print(f"log: --event complete --outcome {outcome} --failure-class {cls}{tag}")


MANIFEST_GLOBS = (
    "package.json", "bun.lock*", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pyproject.toml",
    "requirements*.txt", "uv.lock", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile*", "composer.*",
)
STRAY_GLOBS = ("*.orig", "*.rej", "*.log", ".DS_Store", "*.tmp", "*.bak")
STRAY_DIRS = ("scratch", "tmp")
STRAY_TOP = 5  # largest files named when only the size budget is exceeded


def _glob_re(glob):
    """`*` and `?` stay inside one directory, `**` crosses them; a bare path or trailing `/` means that file or directory."""
    if glob.endswith("/"):
        glob += "**"
    elif not re.search(r"[*?[]", glob):
        return re.compile(re.escape(glob) + r"(?:/.*)?\Z")
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "[":
            end = glob.find("]", i + 1)
            if end > i + 1 and "/" not in glob[i + 1 : end]:
                inner = glob[i + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = end + 1
            else:
                out.append(re.escape("["))
                i += 1
        else:
            c = glob[i]
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _git(root, *argv):
    try:
        p = subprocess.run(["git", *argv], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, p.stdout if p.returncode == 0 else p.stdout + p.stderr


def _scope_fail(msg):
    print(f"error: {msg}")
    _action("fix the --base or --repo argument and rerun scope")
    return 1


def scope_cmd(a):
    _begin(a)
    root = _opt_str(a, "repo") or os.getcwd()
    base = _opt_str(a, "base")
    max_files, max_lines = _opt_int(a, "max_files"), _opt_int(a, "max_lines")
    expect = [e for e in (getattr(a, "expect", None) or []) if isinstance(e, str)]
    rc, out = _git(root, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out.strip() != "true":
        print("error: not a git repository" if rc != 127 else "error: git not found")
        print("action: run scope inside the target repo" if rc != 127 else "action: install git, then rerun scope")
        return 1
    rc, mb = _git(root, "merge-base", base, "HEAD")
    mb = mb.strip()
    if rc != 0 or not mb:
        return _scope_fail(f"cannot find a merge-base with {base} (run: git fetch origin; on a shallow clone run: git fetch --unshallow)")
    rc, num = _git(root, "diff", "--numstat", "-z", "-M", mb)
    if rc != 0:
        return _scope_fail(f"git diff against merge-base {mb[:7]} failed: {num.strip() or 'no output'}")
    rc, new = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0:
        return _scope_fail(f"git ls-files failed: {new.strip() or 'no output'}")
    changed = {}  # path -> changed lines
    toks, i = num.split("\0"), 0
    while i < len(toks):
        added, _, rest = toks[i].partition("\t")
        deleted, _, path = rest.partition("\t")
        i += 1
        if not rest:
            continue
        if not path:  # rename record: "added<TAB>deleted<TAB>" NUL old NUL new
            path, i = toks[i + 1], i + 2
        changed[path] = (int(added) + int(deleted)) if added.isdigit() and deleted.isdigit() else 0  # binary: 0 lines
    for path in (r for r in new.split("\0") if r):
        try:
            with open(os.path.join(root, path), "rb") as fh:
                data = fh.read()
            changed[path] = 0 if b"\0" in data else data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        except OSError:
            changed[path] = 0
    files, lines = len(changed), sum(changed.values())
    manifests = sorted(p for p in changed if any(fnmatch.fnmatchcase(os.path.basename(p), g) for g in MANIFEST_GLOBS))
    patterns = [_glob_re(g) for g in expect]
    outside = sorted(p for p in changed if patterns and not any(r.match(p) for r in patterns))
    stray = sorted(
        p for p in changed
        if any(fnmatch.fnmatchcase(os.path.basename(p), g) for g in STRAY_GLOBS) or set(p.split("/")[:-1]) & set(STRAY_DIRS)
    )
    _detail(f"base: {mb[:7]} (merge-base of {base} and HEAD)")
    _detail(f"files: {files}" + (f"/{max_files}" if max_files is not None else ""))
    _detail(f"lines: {lines}" + (f"/{max_lines}" if max_lines is not None else ""))
    _detail("manifests/lockfiles: " + (", ".join(manifests) or "none"))
    if patterns:
        _detail("outside --expect: " + (", ".join(outside) or "none"))
    _detail("stray: " + (", ".join(stray) or "none"))
    over = []
    if max_files is not None and files > max_files:
        over.append(f"files {files}>{max_files}")
    if max_lines is not None and lines > max_lines:
        over.append(f"lines {lines}>{max_lines}")
    flagged = sorted(set(manifests) | set(outside) | set(stray))
    if over and not flagged:
        flagged = sorted(changed, key=lambda p: (-changed[p], p))[:STRAY_TOP]
    if not flagged and not over:
        _action("ok")
        return 0
    shown = flagged
    if not _FULL:  # compact: say why each file is flagged
        why = {p: [] for p in flagged}
        for label, group in (("manifest", manifests), ("outside", outside), ("stray", stray)):
            for p in group:
                why[p].append(label)
        shown = [f"{p} ({', '.join(why[p])})" if why[p] else f"{p} (largest)" for p in flagged]
    _action(
        "trim: ask the implementer to justify or drop: " + ", ".join(_capped(shown, 5))  # labeled names are long: 5 keep the line short
        + (f"; over budget: {', '.join(over)}" if over else "")
    )
    return 0


def check_install_staleness(now, lockfile_override=None):
    notes = []
    if lockfile_override is not None:
        if not lockfile_override:
            return notes
        candidates_locks = [(lockfile_override, "global", "-g")]
    else:
        candidates_locks = [
            (os.path.expanduser("~/.agents/.skill-lock.json"), "global", "-g"),
            (os.path.join(os.getcwd(), "skills-lock.json"), "project", "-p"),
        ]

    for path, scope_name, flag in candidates_locks:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        skill = ((data.get("skills") or {}) if isinstance(data, dict) else {}).get("agent-orchestration")
        if not isinstance(skill, dict):
            continue
        ts = parse_time(skill.get("updatedAt") or skill.get("installedAt"), now)
        if ts is None:
            try:
                ts = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            except OSError:
                continue
        if (now - ts) > timedelta(days=STALE_INSTALL_DAYS):
            text = (
                f"[note] installed agent-orchestration ({scope_name}) last updated {ts.date().isoformat()} "
                f"(>{STALE_INSTALL_DAYS}d ago); run: npx skills check {flag} && npx skills update agent-orchestration {flag}"
            )
            notes.append((text, "", text))
    return notes


def check_available_notes(catalog, assets_path, loaded_assets, available):
    """`(reason, item, "[note] ...")` for assets, local-catalog, and followup entries the harness's discovery does not offer."""
    names = _catalog_name_map(catalog)

    def canon(x):
        return norm(names.get(norm(x), x))

    notes = []
    avail_models = {}
    pairs = [x.strip() for x in available.split(",") if x.strip()]
    bad = set(bad_available_entries(pairs, catalog))
    for pair in pairs:
        prov, sep, mid = pair.partition(":")
        if pair in bad:
            shown = _show_entry(pair)
            notes.append(("--available entry is not provider:model; ignored", shown, f"[note] --available entry {shown} is not provider:model; ignored"))
            continue
        avail_models.setdefault(norm(prov), set()).add(canon(mid))
    all_models = set().union(*avail_models.values()) if avail_models else set()
    for p in (loaded_assets or {}).get("providers") or []:
        if isinstance(p, dict) and p.get("id") and norm(p["id"]) not in avail_models:
            notes.append(("assets provider has no available model in --available", p["id"], f"[note] assets provider {p['id']} has no available model in --available"))
    local_path = os.path.join(os.path.dirname(os.path.abspath(assets_path)), "catalog.local.yaml")
    loc = load_file(local_path) if os.path.exists(local_path) else None
    for m in (loc.get("models") or []) if isinstance(loc, dict) else []:
        if isinstance(m, dict) and m.get("id") and canon(m["id"]) not in all_models:
            notes.append(("catalog.local.yaml model is not in the available list", m["id"], f"[note] catalog.local.yaml model {m['id']} is not in the available list"))
    for fu in (loaded_assets or {}).get("followups") or []:
        if not isinstance(fu, dict) or not fu.get("needs"):
            continue
        need = str(fu["needs"]).strip()
        if need.lower().startswith("provider:"):
            parts = [x.strip() for x in need[len("provider:"):].split("/") if x.strip()]
            if len(parts) not in (2, 3):
                continue
            models = avail_models.get(norm(parts[0]))
            missing = models is None or (norm(parts[-1]) in names and canon(parts[-1]) not in models)
        else:
            missing = norm(need) in names and canon(need) not in all_models
        if missing:
            notes.append(("followup needs a model that is not available", f"{fu.get('ref', '')} (needs {need})", f"[note] followup {fu.get('ref', '')} needs {need}, which is not available"))
    return notes


def _check_note_lines(items):
    """Lines for check's `(reason, item, text)` notes: grouped by reason (compact) or the original text (--full)."""
    if _FULL:
        return [text for _, _, text in items]
    groups = {}
    for reason, item, text in items:
        groups.setdefault(reason, []).append(item)
    lines = []
    for reason, members in groups.items():
        if members == [""]:  # a note that carries its own advice: keep its text
            lines.append(next(t for r, _, t in items if r == reason))
        else:
            lines.append(f"note: {reason}: " + ", ".join(_capped(members)))
    return _capped(lines)


def check(a):
    _begin(a)
    now = get_now(_opt_str(a, "now"))
    errs = []
    ap = _opt_str(a, "assets") or def_assets()
    c = load_catalog_with_local(_opt_str(a, "catalog") or DEF_CATALOG, ap)
    ids = set()
    for m in c.get("models", []):
        for k in ("id", "name", "family", "cost", "rating"):
            if k not in m:
                errs.append(f"model {m.get('id')}: missing {k}")
        if m.get("id") in ids:
            errs.append(f"duplicate {m['id']}")
        ids.add(m.get("id"))
        if m.get("cost") not in COST:
            errs.append(f"{m.get('id')}: bad cost {m.get('cost')}")
        if m.get("rating") == "rated" and not (
            m.get("scores") and m.get("source") in (c.get("index_versions") or {})
        ):
            errs.append(f"{m['id']}: rated needs scores + known source")
        if m.get("rating") == "estimated":
            sc = m.get("scores")
            if not isinstance(sc, dict) or not sc or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) for v in sc.values()
            ):
                errs.append(f"{m.get('id')}: estimated needs numeric scores (references/research-unrated.md)")
        aliases = m.get("aliases")
        if aliases is not None and (
            not isinstance(aliases, list)
            or not aliases
            or any(not isinstance(x, str) or not x.strip() for x in aliases)
        ):
            errs.append(f"{m.get('id')}: aliases must be a non-empty list of strings")
        coord = m.get("coordinator")
        if coord is not None and not isinstance(coord, bool):
            errs.append(f"{m.get('id')}: coordinator must be true or false")
    loaded_assets = {}
    no_assets = not os.path.exists(ap)
    if no_assets:
        _detail(f"[note] no assets file at {ap}: defaults apply (all discovered providers ok, policy normal)")
    else:
        loaded_assets = load_file(ap) or {}
        prefs = loaded_assets.get("preferences") or {}
        if isinstance(prefs, dict):
            uif = prefs.get("ui_fallback")
            if uif is not None and uif not in VALID_UI_FALLBACK:
                errs.append(f"assets preferences.ui_fallback: bad value {uif} (expected {'|'.join(VALID_UI_FALLBACK)})")
            ppu = prefs.get("pay_per_use")
            if ppu is not None and ppu not in VALID_PAY_PER_USE:
                errs.append(f"assets preferences.pay_per_use: bad value {ppu} (expected {'|'.join(VALID_PAY_PER_USE)})")
            bt = prefs.get("below_tier")
            if bt is not None and bt not in VALID_BELOW_TIER:
                errs.append(f"assets preferences.below_tier: bad value {bt} (expected {'|'.join(VALID_BELOW_TIER)})")
            nc = prefs.get("new_conversations")
            if nc is not None and nc not in VALID_NEW_CONVERSATIONS:
                errs.append(
                    f"assets preferences.new_conversations: bad value {nc} (expected {'|'.join(VALID_NEW_CONVERSATIONS)})"
                )
            dc = prefs.get("design_checkpoints")
            if dc is not None and dc not in VALID_DESIGN_CHECKPOINTS:
                errs.append(
                    f"assets preferences.design_checkpoints: bad value {dc} "
                    f"(expected {'|'.join(VALID_DESIGN_CHECKPOINTS)})"
                )
            ev = prefs.get("evidence")
            if ev is not None and (
                isinstance(ev, bool)
                or not isinstance(ev, (str, list, dict))
                or (isinstance(ev, str) and not ev.strip())
                or (isinstance(ev, (list, dict)) and not ev)
            ):
                errs.append(
                    f"assets preferences.evidence: bad value {ev} (expected non-empty string, list, or map)"
                )
            uct = prefs.get("usage_cache_ttl_seconds")
            if uct is not None and (isinstance(uct, bool) or not isinstance(uct, (int, float)) or uct < 0):
                errs.append(f"assets preferences.usage_cache_ttl_seconds: bad value {uct} (expected number >= 0)")
            lhh = prefs.get("land_hold_hours")
            if lhh is not None and (isinstance(lhh, bool) or not isinstance(lhh, (int, float)) or lhh < 0):
                errs.append(f"assets preferences.land_hold_hours: bad value {lhh} (expected number >= 0)")
            lt = prefs.get("low_threshold")
            if lt is not None and (isinstance(lt, bool) or not isinstance(lt, (int, float)) or not (0 < lt <= 100)):
                errs.append(f"assets preferences.low_threshold: bad value {lt} (expected number in (0, 100])")
            et = prefs.get("exhausted_threshold")
            if et is not None and (isinstance(et, bool) or not isinstance(et, (int, float)) or not (0 < et <= 100)):
                errs.append(f"assets preferences.exhausted_threshold: bad value {et} (expected number in (0, 100])")
            if (
                isinstance(lt, (int, float))
                and not isinstance(lt, bool)
                and isinstance(et, (int, float))
                and not isinstance(et, bool)
                and lt > et
            ):
                errs.append(
                    f"assets preferences: low_threshold ({lt}) must not exceed exhausted_threshold ({et})"
                )
            op = prefs.get("orchestrator")
            if op is not None and not isinstance(op, dict):
                errs.append("assets preferences.orchestrator: must be a map")
            elif isinstance(op, dict):
                ot, ome, od, oe, ol = op.get("tiers"), op.get("max_effort"), op.get("defaults"), op.get("max_escalations"), op.get("ladder")
                if ot is not None and not isinstance(ot, dict):
                    errs.append("assets preferences.orchestrator.tiers: must be a map")
                elif isinstance(ot, dict):
                    for k, v in ot.items():
                        if _parse_orch_tier_key(k) is None:
                            errs.append(f"assets preferences.orchestrator.tiers: bad key {k} (expected 0|1|2)")
                        if not (isinstance(v, str) and v in FLOORS):
                            errs.append(f"assets preferences.orchestrator.tiers[{k}]: bad tier {v} (expected T1|T2|T3|T4)")
                if ome is not None and not (isinstance(ome, str) and ome in ORCH_EFFORTS):
                    errs.append(f"assets preferences.orchestrator.max_effort: bad value {ome} (expected {'|'.join(ORCH_EFFORTS)})")
                if od is not None and not isinstance(od, dict):
                    errs.append("assets preferences.orchestrator.defaults: must be a map")
                elif isinstance(od, dict):
                    for k, v in od.items():
                        if not isinstance(k, str) or not k.strip():
                            errs.append(f"assets preferences.orchestrator.defaults: bad provider key {k!r}")
                        if not (isinstance(v, str) and v in ORCH_EFFORTS):
                            errs.append(f"assets preferences.orchestrator.defaults[{k}]: bad effort {v} (expected {'|'.join(ORCH_EFFORTS)})")
                if oe is not None and (isinstance(oe, bool) or not isinstance(oe, int) or not (0 <= oe <= 3)):
                    errs.append(f"assets preferences.orchestrator.max_escalations: bad value {oe} (expected int 0..3)")
                if ol is not None and ol != "auto":
                    if not isinstance(ol, list) or not ol:
                        errs.append(f"assets preferences.orchestrator.ladder: bad value {ol} (expected auto or list of provider:model@effort)")
                    else:
                        for idx, item in enumerate(ol):
                            if not _parse_pme(item):
                                errs.append(f"assets preferences.orchestrator.ladder[{idx}]: bad value {item} (expected provider:model@effort)")
                ouf, ope = op.get("unflagged_findings"), op.get("phase_effort")
                if ouf is not None and not (_is_int(ouf) and ouf >= 1):
                    errs.append(f"assets preferences.orchestrator.unflagged_findings: bad value {ouf} (expected int >= 1)")
                if ope is not None and ope not in ("on", "off"):
                    errs.append(f"assets preferences.orchestrator.phase_effort: bad value {ope} (expected on|off)")
            esc = prefs.get("escalation")
            if esc is not None and not isinstance(esc, dict):
                errs.append("assets preferences.escalation: must be a map")
            elif isinstance(esc, dict):
                ef, em = esc.get("findings"), esc.get("max")
                if ef is not None and not (_is_int(ef) and ef >= 1):
                    errs.append(f"assets preferences.escalation.findings: bad value {ef} (expected int >= 1)")
                if em is not None and not (_is_int(em) and 0 <= em <= 3):
                    errs.append(f"assets preferences.escalation.max: bad value {em} (expected int 0..3)")
        for p in loaded_assets.get("providers", []) or []:
            if "id" not in p:
                errs.append("assets: provider without id")
            if p.get("policy", "normal") not in list(POLICY) + ["avoid"]:
                errs.append(f"assets {p.get('id')}: bad policy")
            if p.get("status", "ok") not in ("ok", "low", "exhausted"):
                errs.append(f"assets {p.get('id')}: bad status")
            if p.get("effort_key") is not None and (not isinstance(p.get("effort_key"), str) or not p.get("effort_key").strip()):
                errs.append(f"assets {p.get('id')}: effort_key must be a non-empty string")
            groups = p.get("groups")
            if groups is not None:
                if not isinstance(groups, list):
                    errs.append(f"assets {p.get('id')}: groups must be a list")
                else:
                    for g in groups:
                        if not isinstance(g, dict) or not g.get("name"):
                            errs.append(f"assets {p.get('id')}: group missing name")
                            continue
                        if g.get("status", "ok") not in ("ok", "low", "exhausted"):
                            errs.append(f"assets {p.get('id')} group {g.get('name')}: bad status")
                        if g.get("models") is not None and not isinstance(g.get("models"), list):
                            errs.append(f"assets {p.get('id')} group {g.get('name')}: models must be a list")
                        em = g.get("exhausted_models")
                        if em is not None and (not isinstance(em, dict) or any(parse_time(v, now) is None for v in em.values())):
                            errs.append(f"assets {p.get('id')} group {g.get('name')}: exhausted_models must map model ids to timestamps")
        followups = loaded_assets.get("followups")
        if followups is not None:
            if not isinstance(followups, list):
                errs.append("assets followups: must be a list")
            else:
                for idx, fu in enumerate(followups):
                    if not isinstance(fu, dict):
                        errs.append(f"assets followups[{idx}]: must be a map")
                        continue
                    for req_k in ("kind", "ref", "needs"):
                        if not fu.get(req_k):
                            errs.append(f"assets followups[{idx}]: missing {req_k}")
                    if fu.get("kind") and fu.get("kind") not in VALID_FOLLOWUP_KINDS:
                        errs.append(
                            f"assets followups[{idx}].kind: bad value {fu.get('kind')} "
                            f"(expected {'|'.join(VALID_FOLLOWUP_KINDS)})"
                        )
                    for flag in ("on_recovery", "fail_on_low"):
                        if flag in fu and not isinstance(fu.get(flag), bool):
                            errs.append(f"assets followups[{idx}].{flag}: must be true or false")
                    needs_val = str(fu.get("needs") or "").strip()
                    if needs_val.lower().startswith("provider:"):
                        parts = [p.strip() for p in needs_val[len("provider:"):].split("/") if p.strip()]
                        if len(parts) not in (2, 3):
                            errs.append(
                                f"assets followups[{idx}].needs: bad value {needs_val} "
                                "(expected provider:<id>/<tier|model> or provider:<id>/<group>/<tier>)"
                            )
                    elif needs_val.startswith("!"):
                        fam, _, tier = needs_val[1:].lower().partition("/")
                        if fam not in {str(m.get("family") or "").lower() for m in c.get("models", [])} or tier.upper() not in FLOORS:
                            errs.append(
                                f"assets followups[{idx}].needs: bad value {needs_val} (expected !<family>/<tier>: a catalog family and T1-T4)"
                            )
        if "compat" in loaded_assets:
            errs.append("assets compat: removed in 0.14.0 (legacy repo policy is no longer supported); delete the key")
    if errs:
        for line in _capped(["error: " + e for e in errs]):
            print(line)
        print(f"error: {len(errs)} problem(s); action: fix the entries above in catalog.yaml or assets.yaml and rerun check")
        return 1
    items = []
    avail_str = _opt_str(a, "available")
    if avail_str:
        items += check_available_notes(c, ap, loaded_assets, avail_str)
    items += check_install_staleness(now, lockfile_override=_opt_str(a, "lockfile"))
    for line in _check_note_lines(items):
        print(line)
    print("ok: catalog and assets valid" + (" (no assets file: defaults apply)" if no_assets else ""))
    return 0


def status_cmd(a):
    _begin(a)
    now = get_now(a.now)
    ap = a.assets or def_assets()
    clear = ("exhausted_until", "status_note", "unparsed") if a.state == "ok" else ()
    edit_provider(ap, a.provider, {"status": a.state}, now=now, clear_keys=clear)
    print(f"{a.provider}: {a.state}")
    return 0


# ---------- repo orchestration policy ----------
POLICY_KEYS = (
    "Implement",
    "Review",
    "PR",
    "Land",
    "High risk",
    "Models",
    "Conversations",
    "Evidence",
    "Checkpoints",
    "Known failures",
)
POLICY_KEY_MAP = {k.lower(): k for k in POLICY_KEYS}
POLICY_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
POLICY_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _default_repo_dir():
    cwd = os.getcwd()
    rc, out = _git(cwd, "rev-parse", "--show-toplevel")
    top = out.strip() if rc == 0 else ""
    return top if top and os.path.isdir(top) else cwd


def _read_text_lossy(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines(), None
    except OSError as exc:
        return None, str(exc)


def _extract_agents_orchestration(agents_path):
    """Return `(found, sec_lines, read_err)` for `AGENTS.md` `## Orchestration`."""
    if not os.path.isfile(agents_path):
        return False, [], None
    all_lines, err = _read_text_lossy(agents_path)
    if err is not None:
        return True, [], f"cannot read {agents_path}: {err}"
    fence = None
    in_sec = False
    sec_lines = []
    for line in all_lines:
        if fence:
            m = re.match(r"^\s{0,3}(`{3,}|~{3,})\s*$", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            if in_sec:
                sec_lines.append(line)
            continue
        fm = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fm:
            fence = fm.group(1)
            if in_sec:
                sec_lines.append(line)
            continue
        if in_sec and re.match(r"^#{1,2}\s+", line):
            break
        if not in_sec and re.match(r"^##\s+Orchestration(?:\s+#*)?\s*$", line, _I):
            in_sec = True
            continue
        if in_sec:
            sec_lines.append(line)
    return in_sec, sec_lines, None


def _has_policy_bullets(lines):
    return any(re.match(r"^[-*]\s+[^:\n]+?:", l) for l in lines)


def _locate_repo_policy(repo_dir):
    """Return `(rel_label, file_path, lines, warnings, read_err)` or None."""
    dot_path = os.path.join(repo_dir, ".agents", "orchestration.md")
    agents_path = os.path.join(repo_dir, "AGENTS.md")
    if os.path.isfile(dot_path):
        lines, err = _read_text_lossy(dot_path)
        if err is not None:
            return ".agents/orchestration.md", dot_path, [], [], f"cannot read .agents/orchestration.md: {err}"
        warnings = []
        ag_found, ag_lines, _ = _extract_agents_orchestration(agents_path)
        if ag_found and _has_policy_bullets(ag_lines):
            warnings.append("both .agents/orchestration.md and AGENTS.md ## Orchestration exist; ignoring AGENTS.md section")
        return ".agents/orchestration.md", dot_path, lines, warnings, None
    ag_found, ag_lines, ag_err = _extract_agents_orchestration(agents_path)
    if ag_err is not None:
        return "AGENTS.md", agents_path, [], [], ag_err
    return ("AGENTS.md", agents_path, ag_lines, [], None) if ag_found else None


def _strip_key_markup(raw_key):
    k = raw_key.strip()
    for wrap in ("**", "__", "`"):
        if len(k) > 2 * len(wrap) and k.startswith(wrap) and k.endswith(wrap):
            k = k[len(wrap):-len(wrap)].strip()
    return k


def _parse_policy_bullets(lines):
    """Extract ordered `{key: value}` pairs, warnings, and structural errors from policy lines."""
    fields = {}
    warnings = []
    errors = []
    fence = None
    last_key = None
    seen_bullet = False
    for line in lines:
        if fence:
            m = re.match(r"^\s{0,3}(`{3,}|~{3,})\s*$", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        fm = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fm:
            fence = fm.group(1)
            last_key = None
            continue
        if not line.strip():
            last_key = None
            continue
        if re.match(r"^#{1,6}\s+", line):
            last_key = None
            continue
        if re.match(r"^[ \t]+[-*+]\s+", line):
            last_key = None
            errors.append(f"nested bullets are not supported in policy: {line.strip()}")
            continue
        if re.match(r"^\+\s+", line):
            last_key = None
            errors.append(f"unsupported '+' bullet (use '- Key: value'): {line.strip()}")
            continue
        if re.match(r"^[ \t]+\S", line):
            if last_key is not None:
                fields[last_key] = fields[last_key] + " " + line.strip()
            else:
                errors.append(f"unexpected indented line in policy: {line.strip()}")
            continue
        if re.match(r"^[-*]\s+", line):
            seen_bullet = True
            last_key = None
            m = re.match(r"^[-*]\s+([^:\n]+?):\s*(.*)$", line)
            if not m:
                errors.append(f"malformed policy line (expected '- Key: value'): {line.strip()}")
                continue
            raw_key = _strip_key_markup(m.group(1))
            val = m.group(2).strip()
            key = POLICY_KEY_MAP.get(raw_key.lower())
            if not key:
                errors.append(f"unknown policy key {raw_key!r} (expected one of: {', '.join(POLICY_KEYS)})")
                continue
            if key in fields:
                errors.append(f"duplicate policy key {key!r}")
                continue
            if not val:
                errors.append(f"{key}: empty value")
                continue
            fields[key] = val
            last_key = key
            continue
        last_key = None
        if not seen_bullet:
            continue
        errors.append(f"non-bullet line in policy section (expected '- Key: value'): {line.strip()}")
    return fields, warnings, errors


def _split_policy_items(text, regex_mode=False):
    """Split `;`-separated items outside `(...)`, `[...]`, backticks, and (when `regex_mode`) `/.../`."""
    items, buf = [], []
    paren, bracket, in_tick, in_rx = 0, 0, False, False
    i, n = 0, len(text)
    at_start = True
    while i < n:
        c = text[i]
        if in_rx:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == "/":
                in_rx = False
            i += 1
            continue
        if in_tick:
            buf.append(c)
            if c == "`":
                in_tick = False
            i += 1
            continue
        if regex_mode and at_start and c == "/":
            in_rx = True
            at_start = False
            buf.append(c)
            i += 1
            continue
        if c == "`":
            in_tick = True
        elif c == "(":
            paren += 1
        elif c == ")" and paren > 0:
            paren -= 1
        elif c == "[":
            bracket += 1
        elif c == "]" and bracket > 0:
            bracket -= 1
        elif c == ";" and paren == 0 and bracket == 0:
            item = "".join(buf).strip()
            if item:
                items.append(item)
            buf = []
            at_start = True
            i += 1
            continue
        if not c.isspace():
            at_start = False
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _balanced_parens(text):
    depth = 0
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _parse_models_field(val, known_families):
    items = _split_policy_items(val)
    parsed, warnings, errors = [], [], []
    free_text = False
    for item in items:
        m = re.match(r"^(.*?)\s*(?:→|->)\s*(.+)$", item)
        if not m:
            free_text = True
            continue
        task, rhs = m.group(1).strip(), m.group(2).strip()
        if not task:
            errors.append(f"Models: missing task type before '→' in {item!r} (example: Models: ui → family:anthropic)")
            continue
        fm = re.match(r"^family:([A-Za-z0-9_-]+)(?:\s*(.*))?$", rhs)
        if not fm:
            if rhs.lower().startswith("family:"):
                errors.append(f"Models: invalid family spec in {item!r} (example: Models: ui → family:anthropic)")
            else:
                free_text = True
            continue
        fam = fm.group(1).lower()
        if known_families and fam not in known_families:
            errors.append(
                f"Models: unknown family {fam!r} (known: {', '.join(sorted(known_families))}; "
                "example: Models: ui → family:anthropic)"
            )
            continue
        rest = (fm.group(2) or "").strip()
        note = None
        if rest:
            if rest.startswith("(") and rest.endswith(")") and _balanced_parens(rest[1:-1]):
                note = rest[1:-1].strip()
            else:
                errors.append(
                    f"Models: unexpected trailing text {rest!r} in {item!r} "
                    "(wrap notes in parentheses; example: Models: ui → family:anthropic (note))"
                )
                continue
        parsed.append({"task": task, "family": fam, "note": note})

    if free_text:
        errors.append("Models: free-text format is not supported (example: Models: ui → family:anthropic)")
        return [], warnings, errors
    return parsed, warnings, errors


def _parse_high_risk_field(val, repo_dir, check_fs=True):
    items = _split_policy_items(val)
    parsed, warnings, errors = [], [], []
    free_text = False
    real_repo = os.path.realpath(repo_dir) if repo_dir else None
    ex = "example: High risk: db/migrations/**; source:<path>; class:<phrase>"
    for item in items:
        if item.startswith("source:"):
            path = item[len("source:"):].strip()
            if not path or POLICY_LINK_RE.search(path):
                errors.append(f"High risk: source: takes a plain repo-relative path: {item!r} ({ex})")
            elif path.startswith(("/", "~")) or "\\" in path or any(c.isspace() for c in path) or "*" in path or "?" in path or ".." in path.split("/"):
                errors.append(f"High risk: invalid repo-relative path {path!r} in source: ({ex})")
            elif check_fs and real_repo:
                target = os.path.join(repo_dir, path)
                real_target = os.path.realpath(target)
                if os.path.relpath(real_target, real_repo).startswith(".."):
                    errors.append(f"High risk: source path resolves outside repo: {path} ({ex})")
                elif not os.path.isfile(real_target):
                    errors.append(f"High risk: source file does not exist: {path} ({ex})")
                else:
                    parsed.append({"kind": "source", "value": path})
            else:
                parsed.append({"kind": "source", "value": path})
        elif item.startswith("class:"):
            phrase = item[len("class:"):].strip()
            if not phrase:
                errors.append(f"High risk: empty phrase after 'class:' ({ex})")
            else:
                parsed.append({"kind": "class", "value": phrase})
        elif "{" in item or "}" in item:
            errors.append(f"High risk: brace expansion unsupported in {item!r}; list each glob with ';' ({ex})")
        elif POLICY_LINK_RE.search(item) or "," in re.sub(r"\{[^}]*\}", "", item) or any(c.isspace() for c in item):
            free_text = True
        else:
            if item.startswith(("/", "~")):
                errors.append(f"High risk: glob must be repo-relative: {item!r} ({ex})")
            elif ".." in item.split("/"):
                errors.append(f"High risk: glob must not contain '..': {item!r} ({ex})")
            elif "***" in item:
                errors.append(f"High risk: invalid glob '***' in {item!r} ({ex})")
            elif ":" in item:
                errors.append(f"High risk: unknown prefix in {item!r} ({ex})")
            elif item.count("[") != item.count("]") or "[]" in item:
                errors.append(f"High risk: invalid character class in glob {item!r} ({ex})")
            else:
                parsed.append({"kind": "glob", "value": item})

    if free_text:
        errors.append(f"High risk: free-text format is not supported ({ex})")
        return [], warnings, errors
    return parsed, warnings, errors


def _split_known_failure_entry(item):
    """If `item` starts with `/`, scan past the closing unescaped `/` and parse `flags` + `=> action`."""
    s = item.strip()
    if not s.startswith("/"):
        if "=>" not in s:
            return None, None, None, True
        lhs, _, rhs = s.partition("=>")
        return lhs.strip(), "", rhs.strip(), False
    i, n = 1, len(s)
    escaped = False
    close = -1
    while i < n:
        c = s[i]
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == "/":
            close = i
            break
        i += 1
    if close < 0:
        if "=>" not in s:
            return None, None, None, True
        lhs, _, rhs = s.partition("=>")
        return lhs.strip(), "", rhs.strip(), False
    rest = s[close + 1:].strip()
    if "=>" not in rest:
        return None, None, None, True
    flags, _, rhs = rest.partition("=>")
    return s[: close + 1], flags.strip(), rhs.strip(), False


def _parse_known_failures_field(val):
    items = _split_policy_items(val, regex_mode=True)
    parsed, warnings, errors = [], [], []
    free_text = False
    ex = "example: Known failures: /ETIMEDOUT/ => rerun"
    for item in items:
        lhs, flags, rhs, is_free_text = _split_known_failure_entry(item)
        if is_free_text:
            free_text = True
            continue
        if flags:
            errors.append(f"Known failures: regex flags unsupported; matching is case-insensitive ({ex})")
            continue
        if not (lhs.startswith("/") and lhs.endswith("/") and len(lhs) >= 3):
            errors.append(f"Known failures: pattern {lhs!r} must be wrapped in /.../ ({ex})")
            continue
        display_rx = lhs[1:-1]
        raw_rx = display_rx.replace("\\/", "/")
        if not raw_rx:
            errors.append(f"Known failures: empty regex in {item!r} ({ex})")
            continue
        try:
            compiled = re.compile(raw_rx, _I)
        except re.error as exc:
            errors.append(f"Known failures: invalid regex {lhs!r}: {exc} ({ex})")
            continue
        if rhs == "rerun":
            parsed.append({
                "regex": raw_rx,
                "display_regex": display_rx,
                "compiled": compiled,
                "action_kind": "rerun",
                "action": RERUN_ACTION_TEXT,
            })
        elif rhs.startswith("`") and rhs.endswith("`") and len(rhs) >= 2 and "`" not in rhs[1:-1]:
            cmd = rhs[1:-1].strip()
            if not cmd:
                errors.append(f"Known failures: empty command in {item!r} ({ex})")
                continue
            parsed.append({
                "regex": raw_rx,
                "display_regex": display_rx,
                "compiled": compiled,
                "action_kind": "command",
                "action": cmd,
            })
        else:
            errors.append(f"Known failures: action {rhs!r} must be 'rerun' or a backticked command ({ex})")

    if free_text:
        errors.append(f"Known failures: free-text format is not supported ({ex})")
        return [], warnings, errors
    return parsed, warnings, errors


def _check_policy_links(key, text, policy_path, repo_dir):
    errs = []
    stripped = re.sub(r"`[^`\n]*`", "", text)
    base_file = os.path.dirname(os.path.abspath(policy_path))
    real_repo = os.path.realpath(repo_dir)
    for target in POLICY_LINK_RE.findall(stripped):
        if POLICY_SCHEME_RE.match(target) or target.startswith("//"):
            continue
        path, _, _ = target.partition("#")
        if not path:
            continue
        rel_p = unquote(path)
        if rel_p.startswith("/"):
            cands = [os.path.normpath(os.path.join(real_repo, rel_p.lstrip("/")))]
        else:
            cands = [
                os.path.normpath(os.path.join(base_file, rel_p)),
                os.path.normpath(os.path.join(real_repo, rel_p)),
            ]
        inside = [c for c in cands if not os.path.relpath(os.path.realpath(c), real_repo).startswith("..")]
        if not any(os.path.exists(c) for c in inside):
            errs.append(f"{key}: broken link {target!r} (not found in repo)")
            continue
        if os.path.basename(rel_p) == "SKILL.md" and not any(os.path.isfile(c) for c in inside):
            errs.append(f"{key}: linked SKILL.md does not exist: {target!r}")
    return errs


def _load_repo_policy(repo_dir, catalog=None, check_links=False, check_fs=True):
    located = _locate_repo_policy(repo_dir)
    if located is None:
        return {"found": False, "fields": {}, "parsed": {}, "warnings": [], "errors": []}
    source_rel, source_path, sec_lines, loc_warnings, read_err = located
    if read_err is not None:
        return {
            "found": True,
            "source_rel": source_rel,
            "source_path": source_path,
            "fields": {},
            "parsed": {},
            "warnings": loc_warnings,
            "errors": [read_err],
        }
    fields, bullet_warns, errors = _parse_policy_bullets(sec_lines)
    families = {str(m.get("family")).lower() for m in (catalog or {}).get("models", []) if m.get("family")}
    parsed, warnings = {}, list(loc_warnings) + bullet_warns

    for key, val in fields.items():
        if key == "Models":
            items, w, e = _parse_models_field(val, families)
            parsed[key] = items
            warnings.extend(w)
            errors.extend(e)
            if check_links:
                for it in items:
                    if it.get("note"):
                        errors.extend(_check_policy_links(key, it["note"], source_path, repo_dir))
        elif key == "High risk":
            items, w, e = _parse_high_risk_field(val, repo_dir, check_fs=check_fs)
            parsed[key] = items
            warnings.extend(w)
            errors.extend(e)
        elif key == "Known failures":
            items, w, e = _parse_known_failures_field(val)
            parsed[key] = items
            warnings.extend(w)
            errors.extend(e)
        else:
            parsed[key] = val
            if check_links:
                errors.extend(_check_policy_links(key, val, source_path, repo_dir))

    return {
        "found": True,
        "source_rel": source_rel,
        "source_path": source_path,
        "fields": fields,
        "parsed": parsed,
        "warnings": warnings,
        "errors": errors,
    }


def _load_repo_known_failures(repo_dir, explicit_repo=False):
    """Load typed `Known failures` from repo policy; keep valid entries even if others fail."""
    notes = []
    if not repo_dir or not os.path.isdir(repo_dir):
        if explicit_repo:
            notes.append(f"repo directory not found: {repo_dir}")
        return [], notes
    located = _locate_repo_policy(repo_dir)
    if located is None:
        if explicit_repo:
            notes.append(f"no repo policy found in {repo_dir}")
        return [], notes
    _, _, sec_lines, _, read_err = located
    if read_err is not None:
        if explicit_repo:
            notes.append(read_err)
        return [], notes
    fields, _, _ = _parse_policy_bullets(sec_lines)
    val = fields.get("Known failures")
    if not val:
        return [], notes
    items, _, errs = _parse_known_failures_field(val)
    if errs:
        n = len(errs)
        noun = "entry" if n == 1 else "entries"
        notes.append(f"repo Known failures has {n} invalid {noun}; run orch.py policy")
    known = [(it["compiled"], it["regex"], it["action"]) for it in items]
    return known, notes


def _cap_inline_list(key, rendered_items):
    """Render `; `-joined list within CAP items and ~240 chars in compact mode."""
    shown, budget = [], 240
    for it in rendered_items[:CAP]:
        next_len = sum(len(x) + 2 for x in shown) + len(it) + len(key) + 2
        if shown and next_len > budget:
            break
        shown.append(it)
    rem = len(rendered_items) - len(shown)
    line = f"{key}: " + "; ".join(shown)
    if rem > 0:
        line += f"; {_more(rem)}"
    return line


def _fmt_known_failure_item(it):
    act = "rerun" if it["action_kind"] == "rerun" else f"`{it['action']}`"
    return f"/{it['display_regex']}/ => {act}"


def policy_cmd(a):
    _begin(a)
    explicit_repo = _opt_str(a, "repo")
    repo_dir = os.path.abspath(explicit_repo) if explicit_repo else _default_repo_dir()
    if not os.path.isdir(repo_dir):
        print(f"error: repo directory not found: {repo_dir}")
        print("error: 1 problem(s); action: proceed with skill defaults and pass an existing path with --repo DIR")
        return 1
    ap = _opt_str(a, "assets") or def_assets()
    cat_path = _opt_str(a, "catalog") or DEF_CATALOG
    catalog = load_catalog_with_local(cat_path, ap) if os.path.exists(cat_path) else {"models": []}
    res = _load_repo_policy(
        repo_dir,
        catalog=catalog,
        check_links=_opt_bool(a, "check"),
        check_fs=True,
    )
    if not res["found"]:
        print("ok: no policy found (defaults apply)")
        return 0

    print(f"policy: {res['source_rel']}")
    fields = res["fields"]
    parsed = res["parsed"]
    for key, raw_val in fields.items():
        val = parsed.get(key)
        if key == "Models" and val:
            if _FULL:
                print(f"Models ({len(val)}):")
                for it in val:
                    note = f" ({it['note']})" if it.get("note") else ""
                    print(f"  {it['task']} → family:{it['family']}{note}")
            else:
                rendered = [f"{it['task']} → family:{it['family']}" for it in val]
                print(_cap_inline_list("Models", rendered))
        elif key == "High risk" and val:
            if _FULL:
                print(f"High risk ({len(val)}):")
                for it in val:
                    print(f"  {it['kind']}: {it['value']}")
            else:
                rendered = [
                    it["value"] if it["kind"] == "glob" else f"{it['kind']}:{it['value']}"
                    for it in val
                ]
                print(_cap_inline_list("High risk", rendered))
        elif key == "Known failures" and val:
            if _FULL:
                print(f"Known failures ({len(val)}):")
                for it in val:
                    print(f"  {_fmt_known_failure_item(it)}")
            else:
                rendered = [_fmt_known_failure_item(it) for it in val]
                print(_cap_inline_list("Known failures", rendered))
        else:
            txt = raw_val
            if not _FULL and len(txt) > 220:
                txt = txt[:200].rstrip() + " … (rerun with --full)"
            print(f"{key}: {txt}")

    for line in _capped([f"warn: {w}" for w in res["warnings"]]):
        print(line)
    errs = res["errors"]
    for line in _capped([f"error: {e}" for e in errs]):
        print(line)
    if errs:
        print(
            f"error: {len(errs)} problem(s); action: proceed with skill defaults for the invalid field(s) "
            "and tell the user once (converting is their call; references/repo-policy.md)"
        )
        return 1
    n = len(fields)
    print(f"ok: policy valid ({n} field{'s' if n != 1 else ''})")
    return 0


EXAMPLES = {  # one example per subcommand, shown at the end of `<command> --help`
    "check": "check --available claude:claude-opus-5-5",
    "probe": "probe --available claude:claude-opus-5-5",
    "classify": "classify --size 2 --uncertainty 1 --deps 2 --consequences 1 --verification 1 --coordination 2",
    "select": "select --complexity standard --risk low --available claude:claude-opus-5-5,codex:gpt-6-2-sol",
    "launch": "launch task.launch.yaml --available codex:gpt-6.2-sol,claudeAgent:claude-opus-5-5",
    "defer": "defer --ref PR-42 --role implementer --complexity standard --risk low --available claude:claude-opus-5-5,codex:gpt-6-2-sol",
    "followups": "followups --due --available claude:claude-opus-5-5",
    "log": "log --complexity standard --risk low --provider claude --model claude-opus-5-5 --effort high",
    "history": "history --last 5",
    "escalate": "escalate --role implementer --ref PR-42 --current codex:gpt-6-2-sol@medium --cause rework --available codex:gpt-6-2-sol --complexity standard --risk low",
    "exhaust": 'exhaust claude "You\'ve hit your weekly limit · resets Nov 23, 3am"',
    "status": "status claude low",
    "failure": 'failure "context length exceeded" --ref item-1',
    "scope": "scope --base origin/main --max-files 20 --expect 'src/**'",
    "land-check": "land-check --ref PR-42 --risk high --available claude:claude-opus-5-5,antigravity:gemini-4-neon",
    "policy": "policy --check",
}


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("check", "probe", "classify", "select", "launch", "defer", "followups", "log", "history", "escalate", "exhaust", "status", "failure", "scope", "land-check", "policy"):
        usage = (
            "orch.py log [options] [--event select|complete] [--ui] [--surface subagent|conversation] [--repo NAME] [--assets F] [--ledger F] [--now ISO] [--full]"
            if n == "log"
            else "orch.py launch <spec> [options]"
            if n == "launch"
            else "orch.py defer [options] [--assets F] [--catalog F] [--now ISO]"
            if n == "defer"
            else "orch.py exhaust [options] [--assets F] [--catalog F] [--now ISO]"
            if n == "exhaust"
            else f"orch.py {n} [options]"
        )
        p = sub.add_parser(
            n,
            usage=usage,  # the options below list everything; a wrapped usage block would repeat them
            formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, max_help_position=50, width=154),
            epilog="example: python3 scripts/orch.py " + EXAMPLES[n],
        )
        p.add_argument(
            "--full",
            action="store_true",
            help=argparse.SUPPRESS if n == "log" else "print every detail line and remove the list caps",
        )
        if n not in ("scope", "policy", "classify"):
            p.add_argument("--assets", help=argparse.SUPPRESS if n in ("log", "defer", "exhaust") else None)
            p.add_argument("--now", help=argparse.SUPPRESS if n in ("log", "defer", "exhaust") else None)
        elif n in ("policy", "classify"):
            p.add_argument("--assets")
        if n not in ("log", "failure", "scope", "classify"):
            p.add_argument("--catalog", help=argparse.SUPPRESS if n in ("defer", "exhaust") else None)
        if n == "check":
            p.add_argument("--lockfile")
            p.add_argument("--available")
        if n == "probe":
            p.add_argument("--init", action="store_true", help="create assets.yaml when it is missing")
            p.add_argument("--provider")
            p.add_argument("--low-threshold", type=float)
            p.add_argument("--exhausted-threshold", type=float)
            p.add_argument("--available")
            p.add_argument("--fresh", action="store_true", help="bypass the usage cache TTL (429 backoff still applies)")
        if n == "classify":
            for k in RUBRIC_KEYS:
                p.add_argument(f"--{k}", type=int, required=True, choices=[0, 1, 2])
        if n == "select":
            p.add_argument("--role", choices=list(VALID_ROLES), default=None, help="implementer (default), reviewer, or orchestrator")
            p.add_argument("--complexity", choices=list(TIER))
            p.add_argument("--risk", choices=["low", "medium", "high"])
            p.add_argument("--coordination", type=int, choices=[0, 1, 2])
            p.add_argument("--available", required=True)
            p.add_argument("--ui", action="store_true")
            p.add_argument("--prefer-family")
            p.add_argument("--fallback", choices=list(VALID_UI_FALLBACK))
            p.add_argument("--below-tier", choices=list(VALID_BELOW_TIER))
            p.add_argument("--ref")
            p.add_argument("--fail-on-low", action="store_true")
            p.add_argument("--implementer")
            p.add_argument("--phase", choices=list(VALID_PHASES), help="orchestrator: effort for this phase (with --current)")
            p.add_argument("--current", metavar="P:M@E", help="orchestrator: its base rung (with --phase)")
        if n == "launch":
            p.add_argument("spec", help="path to <name>.launch.yaml")
            p.add_argument("--available", help="comma-separated provider:model pairs (overrides spec available)")
            p.add_argument("--no-probe", action="store_true", help="skip quota refresh before resolving the model")
            p.add_argument("--out", metavar="FILE", help="output JSON path (default: <spec without .launch.yaml>.payload.json)")
        if n == "defer":
            p.add_argument("--ref", required=True, help="item or batch label")
            p.add_argument("--role", choices=list(VALID_ROLES))
            p.add_argument("--complexity", choices=list(TIER))
            p.add_argument("--risk", choices=["low", "medium", "high"])
            p.add_argument("--coordination", type=int, choices=[0, 1, 2])
            p.add_argument("--available")
            p.add_argument("--implementer")
            p.add_argument("--ui", action="store_true")
            p.add_argument("--prefer-family")
            p.add_argument("--below-tier", choices=list(VALID_BELOW_TIER))
            p.add_argument("--user-hold", action="store_true", help="the user explicitly asked to wait")
            p.add_argument("--needs", help="with --user-hold (required): follow-up target (e.g. provider:<id>/<tier>)")
            p.add_argument("--after", help="with --user-hold: ISO resume time")
            p.add_argument("--on-recovery", action="store_true", help="with --user-hold: due as soon as the pool recovers")
            p.add_argument("--pool-busy", metavar="POOL=N[,...]", help="running children per pool (e.g. codex=2,antigravity/Gemini=1)")
            p.add_argument("--pool-cap", type=int, default=2, metavar="N", help="max parallel children per pool (default: 2)")
        if n == "followups":
            p.add_argument("--due", action="store_true")
            p.add_argument("--add", action="store_true")
            p.add_argument("--kind", default="ui-review", choices=list(VALID_FOLLOWUP_KINDS))
            p.add_argument("--ref")
            p.add_argument("--needs")
            p.add_argument("--after")
            p.add_argument(
                "--on-recovery",
                action="store_true",
                help="resume: also due as soon as the needed pool is usable again (quota waits)",
            )
            p.add_argument("--done")
            p.add_argument("--available")
        if n == "log":
            p.add_argument("--ledger", help=argparse.SUPPRESS)
            p.add_argument("--event", choices=["select", "complete"], help=argparse.SUPPRESS)
            p.add_argument("--complexity", required=True, choices=list(TIER))
            p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
            p.add_argument("--ui", action="store_true", help=argparse.SUPPRESS)
            p.add_argument("--surface", choices=list(VALID_SURFACES), default="subagent", help=argparse.SUPPRESS)
            p.add_argument("--provider", required=True)
            p.add_argument("--model", required=True)
            p.add_argument("--effort", required=True)
            p.add_argument("--reviewer")
            p.add_argument("--outcome", choices=list(VALID_OUTCOMES))
            p.add_argument("--findings", type=int)
            p.add_argument("--unflagged", type=int, metavar="N", help="orchestrator: findings exposing a gap in its brief or plan")
            p.add_argument("--repo", help=argparse.SUPPRESS)
            p.add_argument("--provisional", action="store_true",
                           help="work ran below the required tier (below_tier or ui_fallback proceed); needs a tier-meeting re-review")
            p.add_argument("--ref", help="short item label (no paths); makes retries countable")
            p.add_argument("--failure-class", choices=list(VALID_FAILURE_CLASSES))
            p.add_argument("--role", choices=list(VALID_ROLES))
            p.add_argument("--rubric", metavar="K=N,...")
            p.add_argument("--requested", metavar="P:M@E")
            p.add_argument("--effective", metavar="P:M@E")
        if n == "history":
            p.add_argument("--ledger")
            p.add_argument("--ref", help="one item label (no paths)")
            p.add_argument("--last", type=int, metavar="N", help="only the N most recent refs")
            p.add_argument("--role", choices=list(VALID_ROLES), help="filter to refs with this role")
            p.add_argument("--by", metavar="model,effort", help="with --role: one line per provider:model@effort (rework, findings, escalations, cost)")
        if n == "escalate":
            p.add_argument("--role", required=True, choices=list(VALID_ROLES))
            p.add_argument("--ref", required=True, help="short item label (no paths)")
            p.add_argument("--current", required=True, metavar="P:M@E", help="the rung that fell short")
            p.add_argument("--cause", required=True, choices=list(VALID_CAUSES), help="rework, findings (with --findings), or status (STATUS: ESCALATE)")
            p.add_argument("--available", required=True)
            p.add_argument("--findings", type=int, metavar="N", help="important review findings (orchestrator: unflagged ones)")
            p.add_argument("--complexity", choices=list(TIER))
            p.add_argument("--risk", choices=["low", "medium", "high"])
            p.add_argument("--coordination", type=int, choices=[0, 1, 2])
            p.add_argument("--implementer", metavar="P:M", help="reviewer: the implementing model (never escalated to)")
            p.add_argument("--dry-run", action="store_true", help="print the decision without logging it")
            p.add_argument("--approved", action="store_true", help="the user approved an ask: log the switch to --to")
            p.add_argument("--to", metavar="P:M@E", help="with --approved: the approved rung")
            p.add_argument("--ledger")
        if n == "scope":
            p.add_argument("--base", required=True, help="git ref to diff against (the merge-base with HEAD is used)")
            p.add_argument("--max-files", type=int)
            p.add_argument("--max-lines", type=int)
            p.add_argument("--expect", action="extend", nargs="+", metavar="GLOB", help="paths the task should touch")
            p.add_argument("--repo", help="repository directory (default: the current directory)")
        if n == "land-check":
            p.add_argument("--ref", required=True, help="item or PR label")
            p.add_argument("--risk", required=True, choices=["low", "medium", "high"], help="task risk level")
            p.add_argument("--available", help="comma-separated provider:model pairs (or @file)")
        if n == "failure":
            p.add_argument("message", help="the child's error text")
            p.add_argument("--ref", required=True, help="short item label (no paths)")
            p.add_argument("--known", action="append", metavar="REGEX=>ACTION", help="repo `Known failures:` pattern, checked first")
            p.add_argument("--repo", help="repository directory to load Known failures from (default: cwd)")
            p.add_argument("--ledger")
        if n == "policy":
            p.add_argument("--repo", help="repository directory (default: the current directory)")
            p.add_argument("--check", action="store_true", help="verify relative links and linked SKILL.md files exist")
        if n in ("exhaust", "status"):
            p.add_argument("provider")
        if n == "exhaust":
            p.add_argument("message")
            p.add_argument("--group")
            p.add_argument("--model")
            p.add_argument("--self", dest="is_self", action="store_true", help="orchestrator's own conversation hit quota (uses --coordination)")
            p.add_argument("--coordination", type=int, choices=[0, 1, 2])
            p.add_argument("--complexity", choices=list(TIER))
            p.add_argument("--risk", choices=["low", "medium", "high"])
            p.add_argument("--available")
            p.add_argument("--role", choices=list(VALID_ROLES))
            p.add_argument("--implementer")
            p.add_argument("--ui", action="store_true")
            p.add_argument("--below-tier", choices=list(VALID_BELOW_TIER))
            p.add_argument("--ref")
        if n == "status":
            p.add_argument("state", choices=["ok", "low"])
        for act in p._actions:
            if act.option_strings and act.required:
                act.help = "(required) " + (act.help or "")
    return ap


def _expand_available(a):
    """`--available @path`: read the pairs from a file (commas and/or newlines; `#` comments and blank lines ignored) into
    `a.available` for every command. Returns the unreadable path, else None."""
    raw = getattr(a, "available", None)
    if not isinstance(raw, str) or not raw.startswith("@"):
        return None
    path = raw[1:].lstrip()
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return path or raw
    a.available = ",".join(x.strip() for ln in text.splitlines() for x in ln.partition("#")[0].split(",") if x.strip())
    return None


def main():
    a = build_parser().parse_args()
    bad = _expand_available(a)
    if bad:
        print(f"error: --available file {_show_entry(bad)} is not readable")
        print(f"error: 1 problem(s); action: fix --available (comma-separated <provider>:<model-id> pairs, or @<readable file>) and rerun {a.cmd}")
        return 1
    return {
        "check": check,
        "probe": probe,
        "classify": classify_cmd,
        "select": select,
        "launch": launch_cmd,
        "defer": defer_cmd,
        "followups": followups_cmd,
        "log": log_cmd,
        "history": history_cmd,
        "escalate": escalate_cmd,
        "exhaust": exhaust,
        "status": status_cmd,
        "failure": failure_cmd,
        "scope": scope_cmd,
        "land-check": land_check_cmd,
        "policy": policy_cmd,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
