#!/usr/bin/env python3
"""Optional, dependency-free helper for the agent-orchestration skill (Python 3.9+).

  orch.py check   [--catalog F] [--assets F]
  orch.py select  --complexity trivial|standard|hard|frontier --risk low|medium|high
                  --available provider:model[,provider:model...] [--ui] [--assets F] [--now ISO]
  orch.py exhaust <provider> "<quota error message>" [--assets F]
  orch.py status  <provider> ok|low [--assets F]

The skill works without this script; it only automates table lookups and assets edits.
YAML support is a small subset: block maps, '- ' lists of maps, one-line {..} / [..], # comments.
"""
import argparse, os, re, sys
from datetime import datetime, timedelta, timezone, date
HERE = os.path.dirname(os.path.abspath(__file__))
DEF_CATALOG = os.path.join(HERE, "..", "catalog.yaml")
def def_assets():
    return os.environ.get("AGENT_ORCHESTRATION_ASSETS") or os.path.expanduser("~/.config/agent-orchestration/assets.yaml")

# ---------- mini YAML ----------
def _strip(line):
    out, q = [], None
    for i, c in enumerate(line):
        if q:
            if c == q: q = None
        elif c in "\"'" and (i == 0 or line[i-1] in " [{,:"): q = c
        elif c == "#" and (i == 0 or line[i-1] == " "): break
        out.append(c)
    return "".join(out).rstrip()

def _scalar(s):
    s = s.strip()
    if s == "" or s in ("null", "~"): return None
    if s[0] in "\"'" and s[-1] == s[0] and len(s) > 1: return s[1:-1]
    if s in ("true", "false"): return s == "true"
    if re.fullmatch(r"-?\d+", s): return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s): return float(s)
    return s

def _split(s):
    parts, depth, cur, q = [], 0, "", None
    for c in s:
        if q:
            cur += c
            if c == q: q = None
        elif c in "\"'": q = c; cur += c
        elif c in "[{": depth += 1; cur += c
        elif c in "]}": depth -= 1; cur += c
        elif c == "," and depth == 0: parts.append(cur); cur = ""
        else: cur += c
    if cur.strip(): parts.append(cur)
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
        if l.strip(): lines.append((len(l) - len(l.lstrip(" ")), l.strip()))
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
                else: d[k] = None
            else: d[k] = _value(v)
        return d, i
    return block(0, lines[0][0])[0] if lines else {}

def load_file(path):
    with open(path, encoding="utf-8") as f: return load_yaml(f.read())

# ---------- model data ----------
COST = {"cheap": 0, "normal": 1, "expensive": 2, "emergency": 3}
POLICY = {"ending": 0, "spend-first": 1, "normal": 2, "preserve": 3}
FLOORS = {"T1": 25, "T2": 40, "T3": 50, "T4": 57}
TIER = {"trivial": {"low": "T1", "medium": "T1", "high": "T2"},
        "standard": {"low": "T2", "medium": "T2", "high": "T3"},
        "hard": {"low": "T3", "medium": "T3", "high": "T4"},
        "frontier": {"low": "T4", "medium": "T4", "high": "T4"}}
norm = lambda s: re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")

def parse_time(s, now):
    if isinstance(s, (date, datetime)): s = str(s)
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=now.tzinfo)

def provider_state(assets, pid, now):
    p = next((x for x in (assets or {}).get("providers", []) or [] if x.get("id") == pid), {})
    st = p.get("status", "ok")
    until = parse_time(p.get("exhausted_until"), now) if p.get("exhausted_until") else None
    if st == "exhausted" and until and until <= now: st = "ok"
    return p, st

def best_effort(m, floor, emerg):
    sc = {e: v for e, v in (m.get("scores") or {}).items() if isinstance(v, (int, float)) and e not in emerg}
    for e, v in sorted(sc.items(), key=lambda kv: kv[1]):
        if v >= floor: return e, v
    return None

def candidates(catalog, assets, available, floor, now, exclude_family=None):
    cat = {norm(m["id"]): m for m in catalog["models"]}
    emerg = catalog.get("emergency_efforts", ["max", "ultra"])
    out, notes = [], []
    for pair in available:
        prov, _, mid = pair.partition(":")
        m = cat.get(norm(mid))
        if not m: notes.append(f"{pair}: not in catalog -> research once (references/research-unrated.md)"); continue
        if m.get("rating") == "unrated": notes.append(f"{pair}: unrated -> research once"); continue
        if m.get("cost") == "emergency": notes.append(f"{pair}: emergency, skipped"); continue
        p, st = provider_state(assets, prov, now)
        if st == "exhausted": notes.append(f"{pair}: provider exhausted"); continue
        if p.get("pay_per_use") or p.get("policy") == "avoid": notes.append(f"{pair}: pay-per-use/avoid, skipped"); continue
        if exclude_family and m["family"] == exclude_family: continue
        be = best_effort(m, floor, emerg)
        isprov = False
        if not be:
            # rated efforts all fall short: propose an effort that has no data as provisional (research it once)
            gap = next((e for e in (["high", "medium"] if floor >= 50 else ["medium", "high"]) if e not in (m.get("scores") or {}) and e not in emerg), None)
            if not gap: notes.append(f"{pair}: no non-emergency effort reaches {floor}"); continue
            be, isprov = (gap, None), True
        pol = POLICY.get(p.get("policy", "normal"), 2) + (10 if st == "low" and floor >= 50 else 0)
        out.append({"pair": pair, "provider": prov, "model": m, "effort": be[0], "score": be[1], "policy": p.get("policy", "normal"), "prov": isprov, "key": (pol + (20 if isprov else 0), (-COST[m["cost"]] if isprov and floor >= 50 else COST[m["cost"]]), be[1] or 0)})
    return out, notes

def select(a):
    now = parse_time(a.now, datetime.now(timezone.utc)) if a.now else datetime.now(timezone.utc).astimezone()
    catalog = load_file(DEF_CATALOG if not a.catalog else a.catalog)
    ap = a.assets or def_assets()
    assets = load_file(ap) if os.path.exists(ap) else None
    if assets is None: print(f"[note] no assets file at {ap}: defaults (all providers ok, policy normal)")
    floors = {**FLOORS, **((assets or {}).get("preferences", {}) or {}).get("tier_floors", {})}
    tier = TIER[a.complexity][a.risk]
    avail = a.available.split(",")
    cands, notes = candidates(catalog, assets, avail, floors[tier], now)
    cands.sort(key=lambda c: c["key"])
    print(f"tier {tier} (>= {floors[tier]})")
    if not cands: print("NO CANDIDATE:", *notes, sep="\n  "); return 2
    impl = cands[0]
    fmt = lambda c: f"{c['provider']} / {c['model']['id']} @ {c['effort']} " + ("(PROVISIONAL: effort unrated, research it once per references/research-unrated.md)" if c["prov"] else f"(score {c['score']}, policy {c['policy']})")
    print(f"implementer: {fmt(impl)}")
    rt = tier if a.risk != "high" else "T" + str(min(4, int(tier[1]) + 1))
    if a.risk == "low" and rt == "T1": rt = "T2"
    rc, rn = candidates(catalog, assets, avail, floors[rt], now, exclude_family=impl["model"]["family"])
    rc.sort(key=lambda c: ((0 if "ui-ux-review" in (c["model"].get("strengths") or []) else 1) if a.ui else 0,) + c["key"])
    if rc:
        r = rc[0]; print(f"reviewer ({rt}, different family): {fmt(r)}")
    else:
        print("reviewer: no different-family candidate; use a different model with fresh context and say so")
    for n in notes: print("  skipped:", n)
    return 0

# ---------- assets edits ----------
def parse_reset(msg, now):
    tzm = re.search(r"\(([A-Za-z_]+/[A-Za-z_]+)\)", msg)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzm.group(1)) if tzm else now.tzinfo
    except Exception: tz = now.tzinfo
    m = re.search(r"[Rr]esets? in\s+(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", msg)
    if m and any(m.groups()):
        d, h, mi, s = (int(x or 0) for x in m.groups())
        return now + timedelta(days=d, hours=h, minutes=mi, seconds=s)
    m = re.search(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?,?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])", msg)
    if not m: return None
    mon, day, yr, hh, mm, ap = m.groups()
    h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    try: month = datetime.strptime(mon, "%b").month
    except ValueError: return None
    year = int(yr) if yr else now.year
    t = datetime(year, month, int(day), h, int(mm or 0), tzinfo=tz)
    if not yr and t <= now: t = t.replace(year=year + 1)
    return t

def edit_provider(path, pid, fields):
    lines = open(path, encoding="utf-8").read().split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(rf"^\s*- id:\s*{re.escape(pid)}\s*$", l)), None)
    if start is None:
        lines += [f"  - id: {pid}"]; start = len(lines) - 1
    ind = len(lines[start]) - len(lines[start].lstrip()) + 2
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or (len(lines[end]) - len(lines[end].lstrip()) >= ind and not lines[end].lstrip().startswith("- "))): end += 1
    keep = [l for l in lines[start + 1:end] if not re.match(rf"^\s*({'|'.join(['status','exhausted_until','status_note','unparsed'])}):", l)]
    new = [" " * ind + f"{k}: {v}" for k, v in fields.items() if v is not None]
    lines[start + 1:end] = new + keep
    open(path, "w", encoding="utf-8").write("\n".join(lines).rstrip("\n") + "\n")

def exhaust(a):
    now = datetime.now(timezone.utc).astimezone()
    t = parse_reset(a.message, now)
    note = '"' + a.message.replace('"', "'")[:200] + '"'
    f = {"status": "exhausted", "exhausted_until": (t or now + timedelta(hours=1)).isoformat(timespec="seconds"), "status_note": note}
    if not t: f["unparsed"] = "true"
    ap = a.assets or def_assets()
    if not os.path.exists(ap): os.makedirs(os.path.dirname(ap), exist_ok=True); open(ap, "w").write("version: 1\nproviders:\n")
    edit_provider(ap, a.provider, f); print(f"{a.provider}: exhausted until {f['exhausted_until']}" + ("" if t else " (unparsed, +1h default)"))

def check(a):
    errs = []
    c = load_file(a.catalog or DEF_CATALOG)
    ids = set()
    for m in c.get("models", []):
        for k in ("id", "name", "family", "cost", "rating"):
            if k not in m: errs.append(f"model {m.get('id')}: missing {k}")
        if m.get("id") in ids: errs.append(f"duplicate {m['id']}")
        ids.add(m.get("id"))
        if m.get("cost") not in COST: errs.append(f"{m.get('id')}: bad cost {m.get('cost')}")
        if m.get("rating") == "rated" and not (m.get("scores") and m.get("source") in c.get("index_versions", {})): errs.append(f"{m['id']}: rated needs scores + known source")
    ap = a.assets or def_assets()
    if a.assets or os.path.exists(ap):
        for p in load_file(ap).get("providers", []) or []:
            if "id" not in p: errs.append("assets: provider without id")
            if p.get("policy", "normal") not in list(POLICY) + ["avoid"]: errs.append(f"assets {p.get('id')}: bad policy")
            if p.get("status", "ok") not in ("ok", "low", "exhausted"): errs.append(f"assets {p.get('id')}: bad status")
    print("OK" if not errs else "\n".join(errs)); return 1 if errs else 0

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("check", "select", "exhaust", "status"):
        p = sub.add_parser(n); p.add_argument("--assets"); p.add_argument("--catalog")
        if n == "select":
            p.add_argument("--complexity", required=True, choices=list(TIER)); p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
            p.add_argument("--available", required=True); p.add_argument("--ui", action="store_true"); p.add_argument("--now")
        if n in ("exhaust", "status"): p.add_argument("provider")
        if n == "exhaust": p.add_argument("message")
        if n == "status": p.add_argument("state", choices=["ok", "low"])
    a = ap.parse_args()
    if a.cmd == "status":
        edit_provider(a.assets or def_assets(), a.provider, {"status": a.state}); print(f"{a.provider}: {a.state}"); return 0
    return {"check": check, "select": select, "exhaust": exhaust}[a.cmd](a)

if __name__ == "__main__": sys.exit(main())
