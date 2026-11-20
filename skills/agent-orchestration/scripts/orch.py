#!/usr/bin/env python3
"""Optional, dependency-free helper for the agent-orchestration skill (Python 3.9+).

  orch.py check   [--catalog F] [--assets F]
  orch.py probe   [--provider P] [--low-threshold N] [--assets F] [--now ISO]
  orch.py select  --complexity trivial|standard|hard|frontier --risk low|medium|high
                  --available provider:model[,provider:model...] [--ui] [--fail-on-low]
                  [--catalog F] [--assets F] [--now ISO]
  orch.py exhaust <provider> "<quota error message>" [--assets F] [--now ISO]
  orch.py status  <provider> ok|low [--assets F] [--now ISO]

Exit codes:
  0  ok
  1  validation or invocation error
  2  no candidate meets the required tier (select)
  3  blocked by pre-flight quota threshold (probe >= low_threshold, or select blocked on low/gated providers)
  4  quota hit (probe at 100% exhausted, or select found only exhausted providers)

YAML support is a small subset: block maps, '- ' lists of maps, one-line {..} / [..], # comments.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CATALOG = os.path.join(HERE, "..", "catalog.yaml")
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_LOW_THRESHOLD = 85


def def_assets():
    return os.environ.get("AGENT_ORCHESTRATION_ASSETS") or os.path.expanduser(
        "~/.config/agent-orchestration/assets.yaml"
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
FLOORS = {"T1": 20, "T2": 40, "T3": 50, "T4": 57}
EFFORT_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4}
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


def candidates(catalog, assets, available, floor, floors, now, exclude_family=None, ui_primary=False, fail_on_low=False):
    cat = {norm(m["id"]): m for m in catalog["models"]}
    emerg = set(catalog.get("emergency_efforts", ["max", "ultra"]))
    out, notes = [], []
    reasons = {"exhausted": 0, "threshold": 0, "gated": 0, "tier": 0}
    t3_floor = floors.get("T3", FLOORS["T3"])

    for pair in available:
        prov, _, mid = pair.partition(":")
        p, st, expired = provider_state(assets, prov, now)
        if expired:
            notes.append(f"{pair}: previous exhaustion expired -> treating provider as ok")

        # Filter provider gates BEFORE catalog/unrated checks.
        if p.get("pay_per_use") or p.get("policy") == "avoid":
            notes.append(f"{pair}: pay-per-use/avoid, skipped")
            reasons["gated"] += 1
            continue
        if st == "exhausted":
            notes.append(f"{pair}: provider exhausted")
            reasons["exhausted"] += 1
            continue
        if fail_on_low and st == "low":
            notes.append(f"{pair}: provider low (blocked by pre-flight threshold)")
            reasons["threshold"] += 1
            continue

        m = cat.get(norm(mid))
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

        be = best_effort(m, floor, emerg)
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
        low_pen = 1 if (st == "low" and floor >= t3_floor) else 0
        nxt = provider_next_reset(p, now)
        reset_ts = nxt.timestamp() if nxt else float("inf")
        ui_rank = (0 if "ui-ux-review" in (m.get("strengths") or []) else 1) if ui_primary else 0
        key = (
            ui_rank,
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


def select(a):
    now = get_now(a.now)
    ap = a.assets or def_assets()
    catalog = load_catalog_with_local(DEF_CATALOG if not a.catalog else a.catalog, ap)
    assets = load_file(ap) if os.path.exists(ap) else None
    if assets is None:
        print(f"[note] no assets file at {ap}: defaults (all providers ok, policy normal)")
    floors = {**FLOORS, **((assets or {}).get("preferences", {}) or {}).get("tier_floors", {})}
    tier = TIER[a.complexity][a.risk]
    avail = [x.strip() for x in a.available.split(",") if x.strip()]

    cands, notes, reasons = candidates(
        catalog,
        assets,
        avail,
        floors[tier],
        floors,
        now,
        ui_primary=a.ui,
        fail_on_low=a.fail_on_low,
    )
    cands.sort(key=lambda c: c["key"])
    print(f"tier {tier} (>= {floors[tier]})")
    if not cands:
        print("NO CANDIDATE:", *notes, sep="\n  ")
        if reasons["exhausted"] > 0 and reasons["tier"] == 0 and reasons["threshold"] == 0:
            return 4
        if reasons["threshold"] > 0 or (reasons["gated"] > 0 and reasons["tier"] == 0 and reasons["exhausted"] == 0):
            return 3
        if reasons["exhausted"] > 0:
            return 4
        return 2

    impl = cands[0]
    if a.ui and "ui-ux-review" not in (impl["model"].get("strengths") or []):
        print("[note] no ui-ux-review candidate available; falling back to best available model")

    fmt = lambda c: (
        f"{c['provider']} / {c['model']['id']} @ {c['effort']} "
        f"(score {c['score']}, policy {c['policy']}"
        + (f", status {c['status']}" if c["status"] != "ok" else "")
        + ")"
    )
    print(f"implementer: {fmt(impl)}")

    rt = tier if a.risk != "high" else "T" + str(min(4, int(tier[1]) + 1))
    rc, _, _ = candidates(
        catalog,
        assets,
        avail,
        floors[rt],
        floors,
        now,
        exclude_family=impl["model"]["family"],
        ui_primary=False,
        fail_on_low=a.fail_on_low,
    )
    rc.sort(key=lambda c: c["key"])
    if rc:
        print(f"reviewer ({rt}, different family): {fmt(rc[0])}")
    else:
        print("reviewer: no different-family candidate; use a different model with fresh context and state it")
    for n in notes:
        print("  note:", n)
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
    text = open(path, encoding="utf-8").read()
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


def edit_provider(path, pid, fields, now=None, clear_keys=()):
    now = now or get_now()
    ensure_assets(path, now)
    lines = open(path, encoding="utf-8").read().split("\n")
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

    new = [" " * ind + f"{k}: {v}" for k, v in fields.items() if v is not None]
    lines[start + 1 : end] = new + keep
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def exhaust(a):
    now = get_now(a.now)
    t = parse_reset(a.message, now)
    note = '"' + a.message.replace('"', "'")[:200] + '"'
    until = (t or now + timedelta(hours=1)).isoformat(timespec="seconds")
    f = {"status": "exhausted", "exhausted_until": until, "status_note": note}
    clear = [] if not t else ["unparsed"]
    if not t:
        f["unparsed"] = "true"
    ap = a.assets or def_assets()
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
        data = json.loads(open(cred_path, encoding="utf-8").read())
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


def probe_claude(cfg, now):
    config_dir = os.path.expanduser(
        cfg.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    )
    token = _read_claude_token(config_dir)
    if not token:
        return None, f"unverified (no OAuth credentials in {config_dir})"

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
        return None, f"unverified (HTTP {e.code})"
    except Exception:
        return None, "unverified (usage request failed)"

    now_ts = now.timestamp()
    active = []
    for win_name, key in (("5h", "five_hour"), ("7d", "seven_day")):
        w = body.get(key)
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
    return {
        "used_percent": _fmt_pct(chosen["used_percent"]),
        "window": chosen["window"],
        "resets_at": chosen["resets_at"],
    }, None


def probe_antigravity(cfg, now):
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
    except Exception:
        return None, "unverified (agy invocation failed)"
    if res.returncode != 0 or not res.stdout.strip():
        return None, f"unverified (agy exited {res.returncode})"
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None, "unverified (agy output is not JSON)"

    # Parse quota entries if agy returns structured windows or text summary.
    group_filter = (cfg.get("group") or "").lower()
    windows = []
    items = data if isinstance(data, list) else data.get("quotas") or data.get("groups") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("group") or "").lower()
            if group_filter and group_filter not in name:
                continue
            for wkey, wlabel in (("five_hour", "5h"), ("weekly", "7d"), ("5h", "5h"), ("7d", "7d")):
                w = item.get(wkey)
                if isinstance(w, dict) and w.get("used_percent") is not None:
                    r_dt = parse_time(w.get("resets_at"), now)
                    if r_dt and r_dt <= now:
                        continue
                    windows.append(
                        {
                            "used_percent": float(w["used_percent"]),
                            "window": wlabel,
                            "resets_at": r_dt.astimezone(timezone.utc).isoformat(timespec="seconds") if r_dt else None,
                            "resets_ts": r_dt.timestamp() if r_dt else 0.0,
                        }
                    )
    if not windows:
        return None, "unverified (agy /usage JSON format unparsed)"
    chosen = max(windows, key=lambda w: (w["used_percent"], w["resets_ts"]))
    return {
        "used_percent": _fmt_pct(chosen["used_percent"]),
        "window": chosen["window"],
        "resets_at": chosen["resets_at"],
    }, None


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
    now = get_now(a.now)
    ap = a.assets or def_assets()
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
    threshold = a.low_threshold if a.low_threshold is not None else prefs.get("low_threshold", DEFAULT_LOW_THRESHOLD)

    # 1. Auto-clear expired exhausted entries and persist them.
    for p in providers:
        pid = p.get("id")
        if not pid:
            continue
        if p.get("status") == "exhausted":
            until = parse_time(p.get("exhausted_until") or p.get("resets_at"), now)
            if until and until <= now:
                edit_provider(
                    ap,
                    pid,
                    {"status": "ok"},
                    now=now,
                    clear_keys=("exhausted_until", "status_note", "unparsed"),
                )
                p["status"] = "ok"
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
        fn = probers.get(cfg.get("kind"))
        if not fn:
            print(f"{pid}: unverified (unknown probe kind {cfg.get('kind')})")
            continue
        result, err = fn(cfg, now)
        if err:
            print(f"{pid}: {err}")
            continue

        pct = result["used_percent"]
        win = result["window"]
        r_at = result.get("resets_at")
        if pct >= 100:
            st = "exhausted"
            worst = max(worst, 4)
        elif pct >= threshold:
            st = "low"
            worst = max(worst, 3)
        else:
            st = "ok"

        fields = {"status": st, "used_percent": pct, "window": win}
        clear = []
        if r_at:
            fields["resets_at"] = r_at
        else:
            clear.append("resets_at")
        if st == "exhausted" and r_at:
            fields["exhausted_until"] = r_at
        else:
            clear.extend(["exhausted_until", "status_note", "unparsed"])

        edit_provider(ap, pid, fields, now=now, clear_keys=clear)
        reset_msg = f", resets {r_at}" if r_at else ""
        print(f"{pid}: {st} ({pct}% of {win}{reset_msg})")

    return worst


def check(a):
    errs = []
    ap = a.assets or def_assets()
    c = load_catalog_with_local(a.catalog or DEF_CATALOG, ap)
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
    if a.assets or os.path.exists(ap):
        for p in (load_file(ap) or {}).get("providers", []) or []:
            if "id" not in p:
                errs.append("assets: provider without id")
            if p.get("policy", "normal") not in list(POLICY) + ["avoid"]:
                errs.append(f"assets {p.get('id')}: bad policy")
            if p.get("status", "ok") not in ("ok", "low", "exhausted"):
                errs.append(f"assets {p.get('id')}: bad status")
    print("OK" if not errs else "\n".join(errs))
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
    for n in ("check", "probe", "select", "exhaust", "status"):
        p = sub.add_parser(n)
        p.add_argument("--assets")
        p.add_argument("--catalog")
        if n in ("probe", "select", "exhaust", "status"):
            p.add_argument("--now")
        if n == "probe":
            p.add_argument("--provider")
            p.add_argument("--low-threshold", type=float)
        if n == "select":
            p.add_argument("--complexity", required=True, choices=list(TIER))
            p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
            p.add_argument("--available", required=True)
            p.add_argument("--ui", action="store_true")
            p.add_argument("--fail-on-low", action="store_true")
        if n in ("exhaust", "status"):
            p.add_argument("provider")
        if n == "exhaust":
            p.add_argument("message")
        if n == "status":
            p.add_argument("state", choices=["ok", "low"])
    a = ap.parse_args()
    return {
        "check": check,
        "probe": probe,
        "select": select,
        "exhaust": exhaust,
        "status": status_cmd,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
