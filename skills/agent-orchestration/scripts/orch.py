#!/usr/bin/env python3
"""Optional, dependency-free helper for the agent-orchestration skill (Python 3.9+).

  orch.py check     [--available provider:model[,...]] [--catalog F] [--assets F] [--now ISO] [--lockfile F]
  orch.py probe     [--fresh] [--provider P] [--low-threshold N] [--exhausted-threshold N] [--available ...] [--assets F] [--now ISO]
  orch.py select    --complexity trivial|standard|hard|frontier --risk low|medium|high
                    --available provider:model[,provider:model...] [--ui] [--prefer-family F]
                    [--fallback proceed|defer|ask] [--below-tier stage|wait|proceed]
                    [--ref TEXT] [--fail-on-low] [--implementer M]
                    [--catalog F] [--assets F] [--now ISO]
  orch.py followups [--due] [--add --kind K --ref R --needs N [--after ISO]] [--done R]
                    [--available ...] [--catalog F] [--assets F] [--now ISO]
  orch.py log       --complexity C --risk R --provider P --model M --effort E
                    [--ui] [--reviewer R] [--surface subagent|conversation]
                    [--event select|complete] [--outcome ok|failed|quota|stall|rework]
                    [--findings N] [--repo NAME] [--provisional]
                    [--assets F] [--ledger F] [--now ISO]
  orch.py exhaust   <provider> "<quota error message>" [--group G | --model M] [--catalog F] [--assets F] [--now ISO]
  orch.py status    <provider> ok|low [--assets F] [--now ISO]

Exit codes:
  0  ok
  1  validation or invocation error
  2  no candidate meets the required tier (select), or no reviewer is usable (select --implementer)
  3  blocked by pre-flight quota threshold (probe >= low_threshold, or select blocked on low/gated/fallback policy)
  4  quota hit (probe >= exhausted_threshold, or select found only exhausted providers)

YAML support is a small subset: block maps, '- ' lists of maps, one-line {..} / [..], # comments.
"""
import argparse, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone, date
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CATALOG = os.path.join(HERE, "..", "catalog.yaml")
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_LOW_THRESHOLD = 85
DEFAULT_EXHAUSTED_THRESHOLD = 98
VALID_UI_FALLBACK = ("proceed", "defer", "ask")
VALID_BELOW_TIER = ("stage", "wait", "proceed")
VALID_PAY_PER_USE = ("never", "ask")
VALID_NEW_CONVERSATIONS = ("ask", "allow")
VALID_DESIGN_CHECKPOINTS = ("ask", "delegate")
VALID_OUTCOMES = ("ok", "failed", "quota", "stall", "rework")
VALID_SURFACES = ("subagent", "conversation")
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


def _value(s):
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        d = {}
        for p in _split(s[1:-1]):
            k, _, v = p.partition(":")
            d[k.strip()] = _value(v)
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
            k, _, v = lines[i][1].partition(":")
            k, v = k.strip(), v.strip()
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


def model_group(p, mid, model_obj=None):
    groups = (p or {}).get("groups")
    if not isinstance(groups, list) or not groups:
        return None
    keys = {norm(mid)}
    if isinstance(model_obj, dict):
        if model_obj.get("id"):
            keys.add(norm(model_obj["id"]))
        if model_obj.get("name"):
            keys.add(norm(model_obj["name"]))
        for alias in (model_obj.get("aliases") or []):
            if alias:
                keys.add(norm(alias))
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


def best_effort(m, floor, emerg):
    scores = m.get("scores") or {}
    rated = [
        (e, v)
        for e, v in scores.items()
        if isinstance(v, (int, float)) and e not in emerg
    ]
    rated.sort(key=lambda kv: (EFFORT_ORDER.get(kv[0], 99), kv[1]))
    for idx, (e, v) in enumerate(rated):
        if v >= floor:
            # Effort knee: if stepping down one rated effort drops <3 points and still meets the floor, take the lower effort.
            if idx > 0 and (v - rated[idx - 1][1]) < 3 and rated[idx - 1][1] >= floor:
                return rated[idx - 1]
            return e, v
    return None


def top_effort(m, emerg):
    rated = [
        (e, v)
        for e, v in (m.get("scores") or {}).items()
        if isinstance(v, (int, float)) and e not in emerg
    ]
    return max(rated, key=lambda kv: (kv[1], -EFFORT_ORDER.get(kv[0], 99))) if rated else None


def unrated_higher_efforts(m, emerg):
    scores = m.get("scores") or {}
    rated_ranks = [
        EFFORT_ORDER[e]
        for e, v in scores.items()
        if isinstance(v, (int, float)) and e in EFFORT_ORDER and e not in emerg
    ]
    max_rated = max(rated_ranks) if rated_ranks else -1
    return [
        e
        for e, rank in sorted(EFFORT_ORDER.items(), key=lambda kv: kv[1])
        if rank > max_rated and e not in emerg and e not in scores
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
            if not group_name or norm(group_name) != norm(parts[1]):
                return False
            return _model_matches_need(m, parts[2], floors=floors, emerg=emerg)
        return False
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
        gname = g.get("name") if isinstance(g, dict) else None
        if not _model_matches_need(m, need, floors=floors, emerg=emerg, provider_id=prov, group_name=gname):
            continue
        if g is not None:
            gst, _ = group_state(g, now)
            if gst in blocked:
                continue
            _add(f"{prov} ({g.get('name') or 'group'})")
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
        if isinstance(groups, list) and groups and partitions_quota(p):
            for g in groups:
                if not isinstance(g, dict):
                    continue
                gst, _ = group_state(g, now)
                if gst in blocked:
                    continue
                for mid in g.get("models") or []:
                    m = cat.get(norm(mid))
                    if (
                        m
                        and m.get("cost") != "emergency"
                        and m.get("rating") != "unrated"
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
                    if cap is not None and group_state(cap, now)[0] in blocked:
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
                            group_name=cap.get("name") if cap is not None else None,
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


def print_followups_status(catalog, assets, now, available=None, due_only=True):
    all_fus = [x for x in (assets or {}).get("followups") or [] if isinstance(x, dict)]
    if available:
        due = due_followups(catalog, assets, now, available=available)
        due_ids = {id(fu) for fu, _ in due}
        for fu, usable in due:
            print(
                f"followup due: [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
                f"(needs {fu.get('needs')}; usable via {', '.join(usable)})"
            )
        if not due_only:
            for fu in all_fus:
                if id(fu) not in due_ids:
                    after = fu.get("after") or "next reset"
                    print(
                        f"followup pending: needs {fu.get('needs')} — [{fu.get('kind', 'review')}] "
                        f"{fu.get('ref', '')} (after {after})"
                    )
            if not all_fus:
                print("no followups")
        return

    ready = pending_ready_followups(catalog, assets, now)
    ready_map = {id(fu): usable for fu, usable in ready}
    if due_only:
        for fu, usable in ready:
            print(
                f"followup pending: needs {fu.get('needs')} — [{fu.get('kind', 'review')}] "
                f"{fu.get('ref', '')} (usable in assets via {', '.join(usable)}; pass --available to check this harness)"
            )
        return

    for fu in all_fus:
        usable = ready_map.get(id(fu))
        if usable:
            print(
                f"followup pending: needs {fu.get('needs')} — [{fu.get('kind', 'review')}] "
                f"{fu.get('ref', '')} (usable in assets via {', '.join(usable)}; pass --available to check this harness)"
            )
        else:
            after = fu.get("after") or "next reset"
            print(
                f"followup pending: needs {fu.get('needs')} — [{fu.get('kind', 'review')}] "
                f"{fu.get('ref', '')} (after {after})"
            )
    if not all_fus:
        print("no followups")


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
        if prov_st == "exhausted":
            st = "exhausted"
        if st == "exhausted" or (fail_on_low and st == "low"):
            offered_exhausted = True
            if g is not None and group_state(g, now)[0] == "exhausted":
                _collect(parse_time(g.get("exhausted_until") or g.get("resets_at"), now) or candidate_next_reset(p, g, now))
            elif prov_st == "exhausted":
                _collect(parse_time(p.get("exhausted_until") or p.get("resets_at"), now) or provider_next_reset(p, now))
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
        if not m or m.get("cost") == "emergency" or m.get("rating") == "unrated":
            continue
        be = best_effort(m, floor, emerg)
        if be is None:
            continue
        p, prov_st, _ = provider_state(assets, prov, now)
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            continue
        g = model_group(p, mid, m)
        gst = group_state(g, now)[0] if g is not None else "ok"
        if prov_st != "exhausted" and gst != "exhausted":
            continue

        dts = []
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
                "group": g.get("name") if isinstance(g, dict) else None,
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
    if ex_cands and len(ex_scopes) == 1 and ex_cands[0]["group"]:
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
):
    cat = {norm(m["id"]): m for m in catalog["models"]}
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    out, notes = [], []
    reasons = {"exhausted": 0, "threshold": 0, "gated": 0, "tier": 0}
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
            continue
        if m.get("rating") == "unrated":
            notes.append(f"{pair}: unrated -> research once (references/research-unrated.md)")
            reasons["tier"] += 1
            continue
        if exclude_family and m["family"] == exclude_family:
            continue

        be = top_effort(m, emerg) if pick_top else best_effort(m, floor, emerg)
        if not be:
            missing = unrated_higher_efforts(m, emerg)
            if missing:
                notes.append(
                    f"{pair}: no rated non-emergency effort reaches {floor} (unrated efforts: {','.join(missing)} -> research once)"
                )
            else:
                notes.append(f"{pair}: no non-emergency effort reaches {floor}")
            reasons["tier"] += 1
            continue

        policy = p.get("policy", "normal")
        pol_rank = POLICY.get(policy, 2)
        low_pen = 1 if (st == "low" and eff_floor >= t3_floor) else 0
        nxt = candidate_next_reset(p, g, now)
        reset_ts = nxt.timestamp() if nxt else float("inf")
        ui_rank = (0 if "ui-ux-review" in (m.get("strengths") or []) else 1) if ui_primary else 0
        fam_rank = (0 if _model_matches_need(m, pref_fam) else 1) if pref_fam else 0
        key = (
            ui_rank,
            fam_rank,
            low_pen,
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


def reviewer_tier(tier, risk):
    return tier if risk != "high" else "T" + str(min(4, int(tier[1]) + 1))


def pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, tier_only=False):
    """Return `(stage, candidate, below_tier)` for the first usable reviewer, or None.

    Capability before independence (references/selection.md): different family meeting `rt` ->
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


def print_reviewer(rt, pick, impl_model, floor):
    stage, c, below = pick
    fam = impl_model["family"]
    labels = {
        1: "different family",
        2: "same family, different model, fresh context",
        3: "different family, below tier",
        4: "same family, different model, fresh context, below tier",
        5: "same model, fresh conversation",
    }
    print(f"reviewer ({rt}, {labels[stage]}{', below tier' if below and stage == 5 else ''}): {_fmt_cand(c)}")
    if stage == 1:
        return
    if stage == 2:
        why = f"no different-family candidate meets {rt}; using same family {fam}, different model, fresh context"
    elif stage == 3:
        why = f"no different-family or same-family candidate meets {rt}; using the best different-family score"
    elif stage == 4:
        why = f"no different-family candidate usable; using same family {fam}, different model, fresh context"
    else:
        why = "no other model usable; using the implementer's model in a fresh conversation"
    if below:
        why += f" (below {rt}: score {c['score']} < {floor})"
    print(f"[note] reviewer degraded: {why}. State this in the report.")


def _resume_ref(ref_arg, assets_exists):
    return ref_arg or ("current-task" if assets_exists else None)


def _record_resume(ap, ref, needs, after_iso, now, on_recovery=True, fail_on_low=False):
    """Record one `kind: resume` entry per ref; refresh it when its fields changed."""
    if not ref:
        print("[note] no assets file and no --ref; pass --ref to record the resume follow-up")
        return
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
        print(f"followup already scheduled: [resume] {ref} (needs {needs}{after_txt})")
        return
    record_followup(ap, entry, now=now)
    if existing:
        print(f"followup updated: [resume] {ref} (needs {needs}{after_txt}); reschedule its wakeup")
    else:
        print(f"followup recorded: [resume] {ref} (needs {needs}{after_txt})")


def _schedule_resume(label, earliest_dt):
    """Print the SCHEDULE_RESUME line; return the resume time (earliest reset + margin) or None."""
    if not earliest_dt:
        print(f"SCHEDULE_RESUME: all usable {label} exhausted (reset time unknown).")
        return None
    earliest_iso = earliest_dt.isoformat(timespec="seconds")
    resume_iso = (earliest_dt + RESUME_MARGIN).isoformat(timespec="seconds")
    print(f"SCHEDULE_RESUME: all usable {label} exhausted (earliest reset: {earliest_iso}; schedule resume at {resume_iso}).")
    return resume_iso


def choose_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, risk, below_tier_mode):
    """Return `(pick, wait)`; `wait` is `(earliest_reset, needs)` when a high-risk `stage` review waits for `rt`."""
    if risk == "high" and below_tier_mode == "stage":
        pick = pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low, tier_only=True)
        if pick:
            return pick, None
        saw, earliest_dt, needs, _ = exhausted_tier_candidates(catalog, assets, avail, rt, floors[rt], now)
        if saw:
            return None, (earliest_dt, needs)
    return pick_reviewer(catalog, assets, avail, floors, rt, now, impl_model, fail_on_low), None


def select_reviewer_only(a, catalog, assets, avail, floors, tier, impl_id, now, ap=None, below_tier_mode="stage", assets_exists=False):
    """Reviewer step: the implementer already ran, so rank the reviewer against the model that did."""
    m = next((x for x in catalog["models"] if norm(x["id"]) == norm(impl_id)), None)
    if not m:
        print(f"implementer {impl_id}: not in catalog")
        return 1
    rt = reviewer_tier(tier, a.risk)
    fail_on_low = bool(a.fail_on_low)
    ref = _resume_ref(_opt_str(a, "ref"), assets_exists)
    print(f"tier {tier} (>= {floors[tier]}); reviewer tier {rt} (>= {floors[rt]})")
    print(f"implementer (given): {m['family']} / {m['id']}")
    pick, wait = choose_reviewer(catalog, assets, avail, floors, rt, now, m, fail_on_low, a.risk, below_tier_mode)
    if pick:
        print_reviewer(rt, pick, m, floors[rt])
        return 0
    if wait:
        earliest_dt, needs = wait
        print(
            f"NO REVIEWER: high-risk work under below_tier=stage takes a reviewer meeting {rt} (>= {floors[rt]}); "
            "wait for one to renew"
        )
        _record_resume(ap, ref, needs, _schedule_resume(f"{rt} reviewers", earliest_dt), now, fail_on_low=fail_on_low)
        return 4
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
    print("NO REVIEWER: no non-excluded model remains (quota recovery order in references/harnesses.md)")
    for n in notes:
        print("  note:", n)
    if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["exhausted"] == 0):
        return 3
    if reasons["exhausted"] > 0:
        saw, earliest_dt, needs, _ = exhausted_tier_candidates(catalog, assets, avail, rt, floors[rt], now)
        label = f"{rt} reviewers"
        if not saw:
            _, earliest_dt, _, _ = exhausted_tier_candidates(catalog, assets, avail, "any", 0, now)
            needs, label = "any", "reviewers"
        _record_resume(ap, ref, needs, _schedule_resume(label, earliest_dt), now, fail_on_low=fail_on_low)
        return 4
    return 2


def select(a):
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    catalog = load_catalog_with_local(DEF_CATALOG if not _opt_str(a, "catalog") else _opt_str(a, "catalog"), ap)
    assets_exists = os.path.exists(ap)
    assets = load_file(ap) if assets_exists else None
    if assets is None:
        print(f"[note] no assets file at {ap}: defaults (all providers ok, policy normal)")
    prefs = (assets or {}).get("preferences") or {}
    floors = {**FLOORS, **(prefs.get("tier_floors") or {})}
    tier = TIER[a.complexity][a.risk]
    avail = [x.strip() for x in a.available.split(",") if x.strip()]
    prefer_family = _opt_str(a, "prefer_family")
    fallback_mode = _opt_str(a, "fallback") or prefs.get("ui_fallback") or "proceed"
    below_tier_mode = _opt_str(a, "below_tier") or prefs.get("below_tier") or "stage"
    ref_arg = _opt_str(a, "ref")

    impl_id = _opt_str(a, "implementer")
    if impl_id:
        return select_reviewer_only(
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
        )

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
    print(f"tier {tier} (>= {floors[tier]})")
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
            print(
                f"STAGE (below_tier=stage): no usable candidate meets {tier} (>= {floors[tier]}); "
                f"starting phase 1 below tier (earliest {tier} reset: {reset_txt}{resume_sched_txt})."
            )
            print(f"implementer (phase 1, low-risk only — read, plan, failing tests/red; below {tier}): {fmt(impl)}")
            print(
                f"phase 2 (risky change, destructive steps, review; requires {tier} >= {floors[tier]}): "
                f"at the phase 1 boundary switch same child in place to {target_txt} immediately. "
                "Send one continue message only for a verified or observed (harness, provider) pair that resumes "
                f"own-provider quota stops (references/harnesses.md); otherwise wait for the scheduled resume at {resume_txt}, "
                "a double check that re-reads the child and acts only while it is idle. "
                f"A harness that cannot switch in place should defer the new Phase 2 child until {resume_txt}; "
                f"on a harness without a scheduler, report the reset time ({reset_txt}) to the user."
            )
            _record_resume(ap, ref_val, resume_needs, resume_iso, now, fail_on_low=bool(a.fail_on_low))
            if ref_val:
                assets = load_file(ap)
            if a.risk == "high":
                print(f"reviewer ({rt}): high-risk work under stage takes a reviewer meeting {rt} (>= {floors[rt]}); select it when the review step starts in phase 2")
            else:
                print(f"reviewer ({rt}): select when the review step starts in phase 2 (fallback order)")
            for n in notes:
                print("  note:", n)
            for fu, usable in due_followups(catalog, assets, now, available=avail):
                print(
                    f"followup due: [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
                    f"(needs {fu.get('needs')}; usable via {', '.join(usable)})"
                )
            return 0

        if can_stage and below_tier_mode == "proceed":
            impl = below_cands[0]
            reset_txt = earliest_iso or "unknown"
            print(
                f"[provisional] (below_tier=proceed): no usable candidate meets {tier} (>= {floors[tier]}, "
                f"earliest reset: {reset_txt}); proceeding with best available below-tier model for all phases "
                f"— tier-meeting ({tier}) re-review follow-up required"
            )
            _record_resume(ap, ref_val, resume_needs, resume_iso, now, fail_on_low=bool(a.fail_on_low))
            if ref_val:
                assets = load_file(ap)
            print(f"implementer: {fmt(impl)}")
            pick = pick_reviewer(catalog, assets, avail, floors, rt, now, impl["model"], bool(a.fail_on_low))
            if pick:
                print_reviewer(rt, pick, impl["model"], floors[rt])
            for n in notes:
                print("  note:", n)
            for fu, usable in due_followups(catalog, assets, now, available=avail):
                print(
                    f"followup due: [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
                    f"(needs {fu.get('needs')}; usable via {', '.join(usable)})"
                )
            return 0

        print("NO CANDIDATE:", *notes, sep="\n  ")
        if (saw_ex_tier and reasons["threshold"] == 0) or (
            reasons["exhausted"] > 0 and reasons["tier"] == 0 and reasons["threshold"] == 0
        ):
            _record_resume(
                ap, ref_val, resume_needs, _schedule_resume(f"{tier} candidates", earliest_dt), now,
                fail_on_low=bool(a.fail_on_low),
            )
            return 4
        if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["tier"] == 0 and reasons["exhausted"] == 0):
            return 3
        if reasons["exhausted"] > 0:
            return 4
        return 2

    impl = cands[0]
    fmt = _fmt_cand

    pref_need = None
    pref_kind = None
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
            print(
                f"[note] preferred {pref_need} not offered by this harness; "
                f"using best available model ({fmt(impl)})"
            )
        else:
            reset_txt = earliest or "unknown"
            if fallback_mode == "defer":
                print(
                    f"DEFER: preferred {pref_need} exhausted (earliest reset: {reset_txt}). "
                    "Recommend waiting for reset (non-urgent work)."
                )
                print(f"  fallback if urgent: {fmt(impl)}")
                for n in notes:
                    print("  note:", n)
                return 3
            if fallback_mode == "ask":
                print(
                    f"ASK: preferred {pref_need} exhausted (earliest reset: {reset_txt}). "
                    f"Ask the user once: proceed provisionally with {fmt(impl)} (and queue follow-up review) or wait for reset?"
                )
                for n in notes:
                    print("  note:", n)
                return 3
            # proceed (default)
            print(
                f"[provisional] preferred {pref_need} exhausted (earliest reset: {reset_txt}); "
                "proceeding with fallback model — follow-up review required"
            )
            if assets_exists or ref_arg:
                fu_entry = {
                    "kind": pref_kind,
                    "ref": ref_arg or "current-task",
                    "needs": pref_need,
                }
                if earliest:
                    fu_entry["after"] = earliest
                record_followup(ap, fu_entry, now=now)
                print(f"followup recorded: [{fu_entry['kind']}] {fu_entry['ref']} (needs {pref_need}, after {reset_txt})")
                assets = load_file(ap)
            else:
                print("[note] no assets file; skipped recording follow-up")

    print(f"implementer: {fmt(impl)}")

    rt = reviewer_tier(tier, a.risk)
    pick, wait = choose_reviewer(
        catalog, assets, avail, floors, rt, now, impl["model"], bool(a.fail_on_low), a.risk, below_tier_mode
    )
    if pick:
        print_reviewer(rt, pick, impl["model"], floors[rt])
    elif wait:
        print(
            f"reviewer ({rt}): high-risk work under stage takes a reviewer meeting {rt} (>= {floors[rt]}); "
            f"wait for one to renew (needs {wait[1]})"
        )
    for n in notes:
        print("  note:", n)
    for fu, usable in due_followups(catalog, assets, now, available=avail):
        print(
            f"followup due: [{fu.get('kind', 'review')}] {fu.get('ref', '')} "
            f"(needs {fu.get('needs')}; usable via {', '.join(usable)})"
        )
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
        raw_mid = model_arg.partition(":")[2] if ":" in model_arg else model_arg
        cid = name_map.get(norm(raw_mid)) or raw_mid
        return model_group(p, cid, cat_by_id.get(norm(cid)))
    return None


def exhaust(a):
    now = get_now(_opt_str(a, "now"))
    t = parse_reset(a.message, now)
    raw_note = a.message.replace('"', "'")[:200]
    quoted_note = '"' + raw_note + '"'
    until = (t or now + timedelta(hours=1)).isoformat(timespec="seconds")
    ap = _opt_str(a, "assets") or def_assets()
    group_arg = _opt_str(a, "group")
    model_arg = _opt_str(a, "model")

    if not group_arg and not model_arg and os.path.exists(ap):
        cur = load_file(ap) or {}
        cur_p = next((x for x in (cur.get("providers") or []) if isinstance(x, dict) and x.get("id") == a.provider), None)
        cur_groups = [g for g in ((cur_p or {}).get("groups") or []) if isinstance(g, dict)]
        if cur_groups and partitions_quota(cur_p):
            names = ", ".join(str(g.get("name")) for g in cur_groups)
            print(
                f"{a.provider}: quota is split into groups ({names}); pass --model <id> for the model that hit "
                "the limit (or --group <name>) so the other groups stay usable"
            )
            return 1

    if group_arg or model_arg:
        ensure_assets(ap, now)
        assets = load_file(ap) or {}
        p = next((x for x in (assets.get("providers") or []) if isinstance(x, dict) and x.get("id") == a.provider), None)
        catalog_path = _opt_str(a, "catalog") or DEF_CATALOG
        catalog = load_catalog_with_local(catalog_path, ap) if os.path.exists(catalog_path) else {"models": []}
        groups = [g for g in ((p or {}).get("groups") or []) if isinstance(g, dict)]
        if groups:
            target_g = _find_provider_group(p, group_arg=group_arg, model_arg=model_arg, catalog=catalog)
            if target_g is None and model_arg and not group_arg and not partitions_quota(p):
                # Another per-model cap on an ungrouped account: add its own one-model group.
                name_map = _catalog_name_map(catalog)
                raw_mid = model_arg.partition(":")[2] if ":" in model_arg else model_arg
                cid = name_map.get(norm(raw_mid)) or raw_mid
                target_g = {"name": cid, "models": [cid]}
                groups.append(target_g)
            if target_g is None:
                print(f"{a.provider}: no matching quota group for {group_arg or model_arg}")
                return 1
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
            return 0

        # Provider has no groups block yet; seed the targeted group entry so subsequent probe merges it.
        name_map = _catalog_name_map(catalog)
        raw_mid = (model_arg.partition(":")[2] if ":" in model_arg else model_arg) if model_arg else None
        cid = (name_map.get(norm(raw_mid)) or raw_mid) if raw_mid else None
        gname = group_arg or cid or "default"
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
        print(f"{a.provider} ({gname}): exhausted until {until}" + ("" if t else " (unparsed, +1h default)"))
        return 0

    f = {"status": "exhausted", "exhausted_until": until, "status_note": quoted_note}
    clear = [] if not t else ["unparsed"]
    if not t:
        f["unparsed"] = "true"
    edit_provider(ap, a.provider, f, now=now, clear_keys=clear)
    print(f"{a.provider}: exhausted until {until}" + ("" if t else " (unparsed, +1h default)"))
    return 0


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

    return min(groups, key=_sort_key)


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
    ap = a.assets or def_assets()
    if os.path.exists(ap) or _opt_bool(a, "init"):
        return _probe(a, ap)
    print(f"[note] no assets file at {ap}: probing with defaults without saving (pass --init to create it)")
    tmp_dir = tempfile.mkdtemp(prefix="orch-probe-")
    try:
        return _probe(a, os.path.join(tmp_dir, "assets.yaml"), cache_home=ap)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _probe(a, ap, cache_home=None):
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
                    print(f"{pid}: expired lockout cleared -> {st_after}")
                for gname in cleared_names:
                    print(f"{pid} ({gname}): expired lockout cleared -> ok")
            continue

        # Ungrouped account, or per-model cap groups layered on the account-level status.
        if cleared_names:
            edit_provider(ap, pid, {"groups": groups}, now=now)
            for gname in cleared_names:
                print(f"{pid} ({gname}): expired lockout cleared -> ok")
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
                print(f"{pid}: expired lockout cleared -> ok")

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
            print(f"{pid}: unverified (unknown probe kind {kind})")
            continue
        if kind == "claude":
            ttl = prefs.get("usage_cache_ttl_seconds")
            ttl = ttl if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0 else USAGE_CACHE_TTL
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(cache_home or ap)), "usage-cache")
            cfg = {**cfg, "_cache_dir": cache_dir, "_fresh": _opt_bool(a, "fresh"), "_cache_ttl": ttl}
        result, err = fn(cfg, now, catalog) if kind == "antigravity" else fn(cfg, now)
        if err:
            print(f"{pid}: {err}")
            continue

        if "groups" in result:
            existing_groups = [eg for eg in (p.get("groups") or []) if isinstance(eg, dict)]
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
                "groups": persisted_groups,
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
            print(f"{pid}: {st} ({usable}/{len(persisted_groups)} groups usable{reset_msg})")
            for g in persisted_groups:
                g_eff_reset = g.get("exhausted_until") or g.get("resets_at")
                gr = f", resets {g_eff_reset}" if g_eff_reset else ""
                ms = ", ".join(g.get("models") or [])
                print(f"  group {g['name']}: {g['status']} ({g['used_percent']}% of {g['window']}{gr}) [{ms}]")
            unmatched = result.get("unmatched") or []
            if unmatched:
                print(f"  note: unmatched model names kept as-is: {', '.join(unmatched)}")
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
        print(f"{pid}: {st} ({pct}% of {win}{reset_msg})")

    updated_assets = load_file(ap) or {}
    avail_str = _opt_str(a, "available")
    avail = [x.strip() for x in avail_str.split(",") if x.strip()] if avail_str else None
    print_followups_status(catalog, updated_assets, now, available=avail, due_only=True)

    return worst


def followups_cmd(a):
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
            print(f"invalid followup kind: {kind} (expected {'|'.join(VALID_FOLLOWUP_KINDS)})")
            return 1
        ref_val = _opt_str(a, "ref")
        needs = _opt_str(a, "needs") or ("any" if kind == "resume" else "ui-ux-review")
        if not ref_val:
            print("followups --add requires --ref")
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
    print_followups_status(catalog, assets, now, available=avail, due_only=_opt_bool(a, "due"))
    return 0


def log_cmd(a):
    now = get_now(_opt_str(a, "now"))
    ap = _opt_str(a, "assets") or def_assets()
    ledger_path = _opt_str(a, "ledger") or def_ledger(ap)

    outcome = _opt_str(a, "outcome")
    event = _opt_str(a, "event") or ("complete" if outcome else "select")
    if event not in ("select", "complete"):
        print(f"invalid event: {event}")
        return 1
    if outcome is not None and outcome not in VALID_OUTCOMES:
        print(f"invalid outcome: {outcome}")
        return 1
    surface = _opt_str(a, "surface") or "subagent"
    if surface not in VALID_SURFACES:
        print(f"invalid surface: {surface}")
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

    parent = os.path.dirname(os.path.abspath(ledger_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    print(f"logged {event} ({repo}) -> {ledger_path}")
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
            notes.append(
                f"[note] installed agent-orchestration ({scope_name}) last updated {ts.date().isoformat()} "
                f"(>{STALE_INSTALL_DAYS}d ago); run: npx skills check {flag} && npx skills update agent-orchestration {flag}"
            )
    return notes


def check_available_notes(catalog, assets_path, loaded_assets, available):
    """[note] lines for assets, local-catalog, and followup entries the harness's discovery does not offer."""
    names = _catalog_name_map(catalog)

    def canon(x):
        return norm(names.get(norm(x), x))

    notes = []
    avail_models = {}
    for pair in (x.strip() for x in available.split(",") if x.strip()):
        prov, sep, mid = pair.partition(":")
        if not sep or not prov.strip() or not mid.strip():
            notes.append(f"[note] --available entry {pair} is not provider:model; ignored")
            continue
        avail_models.setdefault(norm(prov), set()).add(canon(mid))
    all_models = set().union(*avail_models.values()) if avail_models else set()
    for p in (loaded_assets or {}).get("providers") or []:
        if isinstance(p, dict) and p.get("id") and norm(p["id"]) not in avail_models:
            notes.append(f"[note] assets provider {p['id']} has no available model in --available")
    local_path = os.path.join(os.path.dirname(os.path.abspath(assets_path)), "catalog.local.yaml")
    loc = load_file(local_path) if os.path.exists(local_path) else None
    for m in (loc.get("models") or []) if isinstance(loc, dict) else []:
        if isinstance(m, dict) and m.get("id") and canon(m["id"]) not in all_models:
            notes.append(f"[note] catalog.local.yaml model {m['id']} is not in the available list")
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
            notes.append(f"[note] followup {fu.get('ref', '')} needs {need}, which is not available")
    return notes


def check(a):
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
    loaded_assets = {}
    if not os.path.exists(ap):
        print(f"[note] no assets file at {ap}: defaults apply (all discovered providers ok, policy normal)")
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
        for p in loaded_assets.get("providers", []) or []:
            if "id" not in p:
                errs.append("assets: provider without id")
            if p.get("policy", "normal") not in list(POLICY) + ["avoid"]:
                errs.append(f"assets {p.get('id')}: bad policy")
            if p.get("status", "ok") not in ("ok", "low", "exhausted"):
                errs.append(f"assets {p.get('id')}: bad status")
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
    print("OK" if not errs else "\n".join(errs))
    if not errs:
        avail_str = _opt_str(a, "available")
        if avail_str:
            for note in check_available_notes(c, ap, loaded_assets, avail_str):
                print(note)
        for note in check_install_staleness(now, lockfile_override=_opt_str(a, "lockfile")):
            print(note)
    return 1 if errs else 0


def status_cmd(a):
    now = get_now(a.now)
    ap = a.assets or def_assets()
    clear = ("exhausted_until", "status_note", "unparsed") if a.state == "ok" else ()
    edit_provider(ap, a.provider, {"status": a.state}, now=now, clear_keys=clear)
    print(f"{a.provider}: {a.state}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("check", "probe", "select", "followups", "log", "exhaust", "status"):
        p = sub.add_parser(n)
        p.add_argument("--assets")
        if n != "log":
            p.add_argument("--catalog")
        p.add_argument("--now")
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
        if n == "select":
            p.add_argument("--complexity", required=True, choices=list(TIER))
            p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
            p.add_argument("--available", required=True)
            p.add_argument("--ui", action="store_true")
            p.add_argument("--prefer-family")
            p.add_argument("--fallback", choices=list(VALID_UI_FALLBACK))
            p.add_argument("--below-tier", choices=list(VALID_BELOW_TIER))
            p.add_argument("--ref")
            p.add_argument("--fail-on-low", action="store_true")
            p.add_argument("--implementer")
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
            p.add_argument("--ledger")
            p.add_argument("--event", choices=["select", "complete"])
            p.add_argument("--complexity", required=True, choices=list(TIER))
            p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
            p.add_argument("--ui", action="store_true")
            p.add_argument("--surface", choices=list(VALID_SURFACES), default="subagent")
            p.add_argument("--provider", required=True)
            p.add_argument("--model", required=True)
            p.add_argument("--effort", required=True)
            p.add_argument("--reviewer")
            p.add_argument("--outcome", choices=list(VALID_OUTCOMES))
            p.add_argument("--findings", type=int)
            p.add_argument("--repo")
            p.add_argument("--provisional", action="store_true")
        if n in ("exhaust", "status"):
            p.add_argument("provider")
        if n == "exhaust":
            p.add_argument("message")
            p.add_argument("--group")
            p.add_argument("--model")
        if n == "status":
            p.add_argument("state", choices=["ok", "low"])
    a = ap.parse_args()
    return {
        "check": check,
        "probe": probe,
        "select": select,
        "followups": followups_cmd,
        "log": log_cmd,
        "exhaust": exhaust,
        "status": status_cmd,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
