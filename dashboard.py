"""
Cohort-sales LIVE dashboard.

Pulls today vs yesterday metrics per active rep from HubSpot, writes a
self-contained dashboard.html (and dashboard.json) on every refresh.

15 columns per rep:
  1.  Rep
  2-3 Short calls (<2 min): count, improvement % (positive = fewer = good)
  4-5 Long  calls (>=5 min): count, improvement % (positive = more  = good)
  6-7 Follow-ups: count, Pass/Fail (Pass = communications >= calls)
  8-9 Meetings: count, improvement %
 10-11 DNP hygiene: total DNP-marked contacts, median calls logged AFTER DNP
 12-13 NI today, DQ today (stage changed to NI/DQ in today's IST window)
 14    Top NI/DQ reasons (chips: top 3, with counts)
 15    Payment prospects today (stage changed to payment_prospect today)

Usage:
  python dashboard.py            # loop forever, refresh every 180s
  python dashboard.py --once     # single run, exit
  python dashboard.py --interval 300

dashboard.html is self-contained (HTML+CSS+data inline). Drop it on OneDrive
or open directly in any browser. Meta-refresh keeps it live.
"""

import argparse
import datetime as dt
import html as _html
import json
import os
import statistics
import sys
import time
from collections import Counter

import requests

# ---------- config -------------------------------------------------------

B = "https://api.hubapi.com"
IST_OFFSET = dt.timedelta(hours=5, minutes=30)
SHORT_MAX_MS = 120_000      # < 2 min  -> short
LONG_MIN_MS = 300_000       # >= 5 min -> long
THROTTLE = 0.12             # sleep between API calls (sec)
SLOW_TTL_SEC = 30 * 60      # DNP median + reasons cache (30 min, in-memory)
ACTIVE_WINDOW_DAYS = 14     # owner counts as "active rep" if calls in last 14d
SLOW_SNAPSHOT_FILE = "slow_metrics.json"  # produced by the daily workflow,
                                          # consumed by the 5-min workflow

# ---------- ROSTER (41 sales reps confirmed by Kamar) -------------------
# This is the canonical list. The dashboard will fetch metrics for exactly
# these owner IDs, regardless of recent activity. Add/remove rows here as
# the team changes.
REPS = {
    # Senior reps (churn_job.py)
    "162063143": "Toshan Dahiya",
    "164373378": "Mridusmita Bhadra",
    "162063126": "Fathima Nazrin",
    # batch_reps.py "11 new Ayush reps"
    "164253075": "Siddharth Dubey",
    "162063130": "Manasa Mudda Lakshmi",
    "164253072": "Hadika Khan",
    "162134440": "Akhilesh Yadav",
    "164504512": "Atharva Lotlikar",
    "164375864": "Shruti Shudhanshu",
    "164196512": "Shagufta Parveen",
    "162245914": "Harshita Guha",
    "164253071": "Madhaya Praveen",
    "162063135": "Raj Chaudhary",
    "162063136": "Saksham Singhal",
    # Confirmed from 21-May call analysis
    "162134439": "Ayush Tiwari",
    "162882125": "Ankita Gogoi",
    "164253078": "Yashkiran Kaur",
    "162882560": "Kola Keerthi Kumar",
    "163652233": "Hemanth Chanda",
    "164253082": "Stuti Vats",
    "164144991": "Manjusha Mannuru",
    "164253079": "Ishita Vijay",
    "164253074": "Khushi Kumari",
    "164253081": "Pallavi Singh",
    "164253080": "Rishika Amtey",
    # Additional active reps per Kamar's roster
    "162063127": "Khushi Bansal",
    "162063128": "Kushal Singla",
    "162063129": "Lavanya A R",
    "162063141": "Tharaneetharan M",
    "162113583": "Aman Kumar Singh",
    "163540261": "Vikas Naidu Dhulipudi",
    "163983265": "Sidharth Nair",
    "164055446": "S Harisankar",
    "164253063": "Kumari Khushi",
    "164253068": "Hritika Jain",
    "164253073": "Jay Naidu",
    "164253076": "Arivick Chattaraj",
    "164373372": "Vanshika Laheja",
    "164373377": "Abhay Kumar Gupta",
    "164377719": "Vignesh Narayanan",
    "164757276": "Athin Pillai",
}

# Rep ID -> Manager. Best-guess seed based on existing scripts + the
# 21-May call analysis. Anything I'm not confident about lands in
# "Unassigned" so you can see clearly which need verification.
MANAGER_MAP = {
    # ----- Anand Mehta (Techno Managers / Ayush Singh / Wanderess Priyanka) ----
    "162063135": "Anand",          # Raj Chaudhary    (ayush_singh13)
    "162134439": "Anand",          # Ayush Tiwari     (ayush_singh13)
    "164253075": "Anand",          # Siddharth Dubey  (ayush_singh13)
    "162063136": "Anand",          # Saksham Singhal  (ayush_singh13)
    "164253081": "Anand",          # Pallavi Singh    (technomanagers)
    "164253080": "Anand",          # Rishika Amtey    (wanderess_priyanka)

    # ----- Puja Mishra (Manas Bichoo) -----------------------------------
    "162882125": "Puja",           # Ankita Gogoi
    "164253078": "Puja",           # Yashkiran Kaur
    "162882560": "Puja",           # Kola Keerthi Kumar
    "162063130": "Puja",           # Manasa Mudda Lakshmi (per call analysis)
    "164253072": "Puja",           # Hadika Khan
    "164253071": "Puja",           # Madhaya Praveen
    "162134440": "Puja",           # Akhilesh Yadav
    "164196512": "Puja",           # Shagufta Parveen
    "162245914": "Puja",           # Harshita Guha
    "164375864": "Puja",           # Shruti Shudhanshu
    "164504512": "Puja",           # Atharva Lotlikar

    # ----- Harshitha PS (Payal in Europe) -------------------------------
    "163652233": "Harshitha PS",   # Hemanth Chanda
    "164253082": "Harshitha PS",   # Stuti Vats

    # The remaining 22 reps stay in "Unassigned" because dashboard rows
    # are NOT grouped by manager — they render as one flat table sorted
    # by today's call volume. Manager field is kept in the data only so
    # you can re-introduce grouping/filtering later if you want.
}

# ---------- helpers ------------------------------------------------------

HEADERS = None
SSL_VERIFY = True
_SLOW_CACHE = {}  # owner_id -> (timestamp, {"median_calls_after_dnp", "top_reasons"})


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def find_env():
    """Walk up from the script + CWD looking for .env. Worktrees nest 3
    levels deep under the project root."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [os.getcwd(), here]
    for base in bases:
        cur = base
        for _ in range(6):
            cand = os.path.join(cur, ".env")
            if os.path.exists(cand):
                return os.path.abspath(cand)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    raise SystemExit(f"Could not find .env in or above {here}")


def load_token():
    """1) HUBSPOT_ACCESS_TOKEN env var if present (used in GitHub Actions
    via the repo Secret of the same name). 2) Otherwise look in a .env
    file alongside the script or up the directory tree."""
    env_tok = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if env_tok:
        log("Token from HUBSPOT_ACCESS_TOKEN env var.")
        return env_tok.strip()
    path = find_env()
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("HUBSPOT_ACCESS_TOKEN"):
                if "\t" in line:
                    return line.split("\t", 1)[1].strip()
                if "=" in line:
                    return line.split("=", 1)[1].strip()
    raise SystemExit(f"HUBSPOT_ACCESS_TOKEN missing in {path}")


def api(method, path, **kw):
    for attempt in range(8):
        try:
            r = requests.request(method, B + path, headers=HEADERS, timeout=60,
                                 verify=SSL_VERIFY, **kw)
        except requests.RequestException as e:
            log(f"  net error: {e}, retry"); time.sleep(2 ** attempt); continue
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 2)) + 1
            time.sleep(wait); continue
        if 500 <= r.status_code < 600:
            time.sleep(2 ** attempt); continue
        if r.status_code >= 400:
            raise RuntimeError(f"{path} {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"retries exhausted {path}")


def ist_bounds():
    """Return (today_start_utc, today_end_utc, yesterday_start_utc) as
    timezone-aware UTC datetimes. 'Today' = IST calendar day."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_ist = now_utc + IST_OFFSET
    today_ist_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    today_start_utc = (today_ist_midnight - IST_OFFSET).replace(tzinfo=dt.timezone.utc)
    today_end_utc = today_start_utc + dt.timedelta(days=1)
    yest_start_utc = today_start_utc - dt.timedelta(days=1)
    return today_start_utc, today_end_utc, yest_start_utc


def epoch_ms(d):
    return int(d.timestamp() * 1000)


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def search_count(obj, filters):
    body = {"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1}
    out = api("POST", f"/crm/v3/objects/{obj}/search", json=body).get("total", 0)
    time.sleep(THROTTLE)
    return out


# ---------- owner discovery ---------------------------------------------

def get_owners():
    out, after = [], None
    while True:
        path = "/crm/v3/owners?limit=100" + (f"&after={after}" if after else "")
        d = api("GET", path)
        for o in d.get("results", []):
            if o.get("archived"):
                continue
            name = ((o.get("firstName") or "") + " " + (o.get("lastName") or "")).strip()
            out.append({
                "id": str(o["id"]),
                "email": o.get("email") or "",
                "name": name or o.get("email") or str(o["id"]),
            })
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def filter_active_reps(owners, today_start_utc):
    """Keep owners with at least one call in the last ACTIVE_WINDOW_DAYS days."""
    cutoff = epoch_ms(today_start_utc - dt.timedelta(days=ACTIVE_WINDOW_DAYS))
    keep = []
    for o in owners:
        n = search_count("calls", [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": o["id"]},
            {"propertyName": "hs_timestamp", "operator": "GTE", "value": str(cutoff)},
        ])
        if n > 0:
            o["recent_calls_14d"] = n
            keep.append(o)
    return keep


# ---------- per-rep metrics --------------------------------------------

def call_count(owner_id, ts_start, ts_end, dur_op):
    f = [
        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
        {"propertyName": "hs_timestamp", "operator": "BETWEEN",
         "value": str(epoch_ms(ts_start)), "highValue": str(epoch_ms(ts_end))},
    ]
    if dur_op[0] == "BETWEEN":
        f.append({"propertyName": "hs_call_duration", "operator": "BETWEEN",
                  "value": dur_op[1], "highValue": dur_op[2]})
    else:
        f.append({"propertyName": "hs_call_duration", "operator": dur_op[0], "value": dur_op[1]})
    return search_count("calls", f)


def event_count(obj, owner_id, ts_start, ts_end, ts_prop="hs_timestamp"):
    return search_count(obj, [
        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
        {"propertyName": ts_prop, "operator": "BETWEEN",
         "value": str(epoch_ms(ts_start)), "highValue": str(epoch_ms(ts_end))},
    ])


def stage_marked_today(owner_id, stage, ts_start, ts_end):
    return search_count("contacts", [
        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
        {"propertyName": "contact_engagement_stage", "operator": "EQ", "value": stage},
        {"propertyName": "engagement_stage_last_changed_at", "operator": "BETWEEN",
         "value": str(epoch_ms(ts_start)), "highValue": str(epoch_ms(ts_end))},
    ])


def stage_total(owner_id, stage):
    return search_count("contacts", [
        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
        {"propertyName": "contact_engagement_stage", "operator": "EQ", "value": stage},
    ])


def fast_metrics(owner_id, ts):
    ts_start, ts_end, yest_start = ts
    m = {}
    m["short_today"] = call_count(owner_id, ts_start, ts_end, ("LT", str(SHORT_MAX_MS)))
    m["mid_today"]   = call_count(owner_id, ts_start, ts_end, ("BETWEEN", str(SHORT_MAX_MS), str(LONG_MIN_MS)))
    m["long_today"]  = call_count(owner_id, ts_start, ts_end, ("GTE", str(LONG_MIN_MS)))
    m["short_yest"]  = call_count(owner_id, yest_start, ts_start, ("LT", str(SHORT_MAX_MS)))
    m["mid_yest"]    = call_count(owner_id, yest_start, ts_start, ("BETWEEN", str(SHORT_MAX_MS), str(LONG_MIN_MS)))
    m["long_yest"]   = call_count(owner_id, yest_start, ts_start, ("GTE", str(LONG_MIN_MS)))
    m["followups_today"] = event_count("communications", owner_id, ts_start, ts_end)
    m["followups_yest"]  = event_count("communications", owner_id, yest_start, ts_start)
    m["meetings_today"]  = event_count("meetings", owner_id, ts_start, ts_end, ts_prop="hs_meeting_start_time")
    m["meetings_yest"]   = event_count("meetings", owner_id, yest_start, ts_start, ts_prop="hs_meeting_start_time")
    m["ni_today"]   = stage_marked_today(owner_id, "ni_not_interested", ts_start, ts_end)
    m["dq_today"]   = stage_marked_today(owner_id, "disqualified", ts_start, ts_end)
    m["pp_today"]   = stage_marked_today(owner_id, "payment_prospect", ts_start, ts_end)
    m["dnp_total"]  = stage_total(owner_id, "dnp_did_not_pick")
    return m


def slow_metrics(owner_id):
    cached = _SLOW_CACHE.get(owner_id)
    if cached and time.time() - cached[0] < SLOW_TTL_SEC:
        return cached[1]
    val = {
        "median_calls_after_dnp": compute_dnp_median(owner_id),
        "top_reasons": compute_top_reasons(owner_id),
    }
    _SLOW_CACHE[owner_id] = (time.time(), val)
    return val


def load_slow_snapshot():
    """Load slow_metrics.json sidecar if present (produced by the daily
    slow-snapshot workflow). Returns dict keyed by owner_id."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        SLOW_SNAPSHOT_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        log(f"Loaded {SLOW_SNAPSHOT_FILE} computed {d.get('computed_at_ist','?')}")
        return d.get("by_owner", {})
    except Exception as e:
        log(f"Could not read {path}: {e}")
        return {}


def write_slow_snapshot(rows):
    """Persist slow metrics to disk so the 5-min main workflow can read
    them without recomputing every cycle."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        SLOW_SNAPSHOT_FILE)
    by_owner = {r["owner_id"]: {
        "median_calls_after_dnp": r.get("median_calls_after_dnp", 0),
        "top_reasons": r.get("top_reasons", []),
    } for r in rows}
    now_utc = dt.datetime.now(dt.timezone.utc)
    data = {
        "computed_at_utc": now_utc.isoformat(),
        "computed_at_ist": (now_utc + IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S IST"),
        "by_owner": by_owner,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log(f"Wrote {SLOW_SNAPSHOT_FILE} ({len(by_owner)} reps)")


def compute_dnp_median(owner_id):
    """Median number of calls logged on a contact AFTER it was marked DNP.
    Reuses the history-dive pattern from batch_reps.py. Capped at 30 pages
    of calls (3000) per rep to keep latency bounded."""
    ct, cur, pages = {}, "0", 0
    while pages < 30:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
            {"propertyName": "hs_object_id", "operator": "GT", "value": cur}]}],
            "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
            "properties": ["hs_object_id", "hs_timestamp"], "limit": 100}
        d = api("POST", "/crm/v3/objects/calls/search", json=body)
        res = d.get("results", [])
        if not res:
            break
        for c in res:
            ct[c["id"]] = parse_iso(c["properties"].get("hs_timestamp", ""))
        cur = res[-1]["id"]
        if len(res) < 100:
            break
        pages += 1
        time.sleep(THROTTLE)
    if not ct:
        return 0
    cc = {}
    cids = list(ct)
    for i in range(0, len(cids), 100):
        d = api("POST", "/crm/v4/associations/calls/contacts/batch/read",
                json={"inputs": [{"id": x} for x in cids[i:i + 100]]})
        for r in d.get("results", []):
            fid = r.get("from", {}).get("id"); tt = ct.get(fid)
            for t in r.get("to", []):
                cid = str(t.get("toObjectId"))
                cc.setdefault(cid, [])
                if tt:
                    cc[cid].append(tt)
        time.sleep(THROTTLE)
    if not cc:
        return 0
    afters = []
    ids = list(cc)
    for i in range(0, len(ids), 50):
        d = api("POST", "/crm/v3/objects/contacts/batch/read",
                json={"propertiesWithHistory": ["contact_engagement_stage"],
                      "properties": ["contact_engagement_stage"],
                      "inputs": [{"id": x} for x in ids[i:i + 50]]})
        for c in d.get("results", []):
            hist = c.get("propertiesWithHistory", {}).get("contact_engagement_stage", [])
            dnp_ts = None
            for h in hist:
                if h.get("value") == "dnp_did_not_pick":
                    t = parse_iso(h.get("timestamp", ""))
                    if t and (dnp_ts is None or t < dnp_ts):
                        dnp_ts = t
            if dnp_ts:
                after = sum(1 for tt in cc.get(c["id"], []) if tt and tt > dnp_ts)
                afters.append(after)
        time.sleep(THROTTLE)
    return round(statistics.median(afters), 1) if afters else 0


def compute_top_reasons(owner_id):
    OW = {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id}
    body = {
        "filterGroups": [{"filters": [OW,
            {"propertyName": "contact_engagement_stage", "operator": "IN",
             "values": ["ni_not_interested", "disqualified"]}]}],
        "properties": ["not_interested_reason", "disqualification_reason",
                       "reason_for_notinteresteddisqualifiedghosted"],
        "limit": 100,
    }
    reasons = Counter()
    after = None
    pages = 0
    while pages < 5:  # cap at 500 contacts for perf
        if after:
            body["after"] = after
        d = api("POST", "/crm/v3/objects/contacts/search", json=body)
        for c in d.get("results", []):
            p = c.get("properties", {})
            for r in (p.get("not_interested_reason"),
                      p.get("disqualification_reason"),
                      p.get("reason_for_notinteresteddisqualifiedghosted")):
                if r and str(r).strip():
                    reasons[str(r).strip()] += 1
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        pages += 1
        time.sleep(THROTTLE)
    return [{"reason": r, "n": n} for r, n in reasons.most_common(3)]


def improvement_pct(today, yest, higher_better=True):
    """Return signed % where POSITIVE = improvement (always). Returns None
    if both are zero, so the UI can show '—'."""
    if today == 0 and yest == 0:
        return None
    if yest == 0:
        return 100.0 if higher_better else -100.0
    raw = (today - yest) / yest * 100
    return round(raw if higher_better else -raw, 1)


# ---------- main collect ------------------------------------------------

def collect_all(limit=0, skip_slow=False):
    ts = ist_bounds()
    owners = [{"id": oid, "name": name, "email": ""} for oid, name in REPS.items()]
    log(f"Roster: {len(owners)} reps (explicit list, see REPS dict in dashboard.py).")
    if limit and len(owners) > limit:
        owners = owners[:limit]
        log(f"  -> --limit {limit}: testing first {limit} only.")
    snapshot = load_slow_snapshot() if skip_slow else {}
    rows = []
    for i, o in enumerate(owners):
        log(f"  [{i+1}/{len(owners)}] {o['name']} ({o['id']})")
        try:
            fm = fast_metrics(o["id"], ts)
            if skip_slow:
                sm = snapshot.get(o["id"], {"median_calls_after_dnp": 0, "top_reasons": []})
            else:
                sm = slow_metrics(o["id"])
            row = {**fm, **sm,
                   "name": o["name"], "email": o["email"], "owner_id": o["id"],
                   "manager": MANAGER_MAP.get(o["id"], "Unassigned")}
            row["calls_today"] = row["short_today"] + row["mid_today"] + row["long_today"]
            row["followup_pass"] = row["followups_today"] >= row["calls_today"] if row["calls_today"] > 0 else True
            row["short_pct"] = improvement_pct(row["short_today"], row["short_yest"], higher_better=False)
            row["long_pct"]  = improvement_pct(row["long_today"], row["long_yest"], higher_better=True)
            row["meet_pct"]  = improvement_pct(row["meetings_today"], row["meetings_yest"], higher_better=True)
            rows.append(row)
        except Exception as e:
            log(f"     FAILED: {e}")
    rows.sort(key=lambda r: (-r["calls_today"], r["name"]))
    now_utc = dt.datetime.now(dt.timezone.utc)
    return {
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_ist": (now_utc + IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S IST"),
        "today_window_ist": (ts[0] + IST_OFFSET).strftime("%Y-%m-%d 00:00 IST"),
        "rows": rows,
    }


# ---------- HTML render -------------------------------------------------

def esc(s):
    return _html.escape(str(s or ""))


def pct_html(p):
    if p is None:
        return "<span class='pct na'>—</span>"
    cls = "pct " + ("up" if p > 0 else ("down" if p < 0 else "flat"))
    arrow = "▲" if p > 0 else ("▼" if p < 0 else "•")
    return f"<span class='{cls}'>{arrow} {abs(p):.0f}%</span>"


def render_html(data):
    rows = data["rows"]

    # Flat table, no manager grouping. Already sorted by calls_today desc.
    row_html = []
    for r in rows:
        pass_html = ("<span class='badge ok'>PASS</span>" if r["followup_pass"]
                     else "<span class='badge bad'>FAIL</span>")
        reasons = r.get("top_reasons") or []
        reasons_html = (" ".join(
            f"<span class='chip'>{esc(x['reason'])} <em>({x['n']})</em></span>"
            for x in reasons) or "<span class='muted'>—</span>")
        row_html.append(
            "<tr>"
            f"<td class='rep'>{esc(r['name'])}<div class='oid'>{esc(r['owner_id'])}</div></td>"
            f"<td class='num bad-if-high'>{r['short_today']}</td><td>{pct_html(r['short_pct'])}</td>"
            f"<td class='num good'>{r['long_today']}</td><td>{pct_html(r['long_pct'])}</td>"
            f"<td class='num'>{r['followups_today']}</td><td>{pass_html}</td>"
            f"<td class='num'>{r['meetings_today']}</td><td>{pct_html(r['meet_pct'])}</td>"
            f"<td class='num'>{r['dnp_total']}</td>"
            f"<td class='num'>{r.get('median_calls_after_dnp', 0)}</td>"
            f"<td class='num bad'>{r['ni_today']}</td><td class='num bad'>{r['dq_today']}</td>"
            f"<td class='reasons'>{reasons_html}</td>"
            f"<td class='num good'>{r['pp_today']}</td>"
            "</tr>"
        )

    totals = {k: sum(r[k] for r in rows) for k in
              ("short_today", "long_today", "followups_today", "meetings_today",
               "ni_today", "dq_today", "pp_today", "dnp_total")}

    body_html = "\n".join(row_html) if row_html else (
        "<tr><td colspan='15' style='text-align:center;padding:30px;color:#94a3b8'>"
        "No active reps found. Check HubSpot token + last 14d activity.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Cohort-Sales Live Dashboard · {esc(data['generated_at_ist'])}</title>
<style>
  *{{box-sizing:border-box}}
  body{{font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
       margin:0;padding:18px;background:#0f172a;color:#e2e8f0}}
  h1{{font-size:19px;margin:0 0 4px;color:#f1f5f9;letter-spacing:-.2px}}
  .sub{{color:#94a3b8;font-size:12px;margin-bottom:14px}}
  .summary{{display:flex;gap:10px;margin:0 0 16px;flex-wrap:wrap}}
  .kpi{{background:#1e293b;padding:8px 14px;border-radius:6px;border:1px solid #334155;min-width:120px}}
  .kpi b{{display:block;font-size:20px;color:#f8fafc;font-variant-numeric:tabular-nums}}
  .kpi span{{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px}}
  .wrap{{overflow:auto;border-radius:6px;border:1px solid #334155;background:#1e293b}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th,td{{padding:7px 9px;text-align:left;border-bottom:1px solid #334155;vertical-align:middle}}
  thead th{{background:#0b1220;color:#94a3b8;text-transform:uppercase;font-size:10px;
            letter-spacing:.6px;font-weight:600;position:sticky;top:0;z-index:1;
            border-bottom:1px solid #334155;white-space:nowrap}}
  thead tr:first-child th{{border-bottom:1px solid #1e293b}}
  tr.mgr-row td{{background:#293548;color:#cbd5e1;padding:8px 12px;font-size:11.5px}}
  tr.mgr-row b{{color:#f1f5f9;font-size:13px}}
  tr:hover td{{background:#243044}}
  tr.mgr-row:hover td{{background:#293548}}
  td.rep{{font-weight:600;color:#f1f5f9;white-space:nowrap;line-height:1.2}}
  td.rep .oid{{font-size:10px;color:#64748b;font-weight:400;margin-top:2px}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  td.num.good{{color:#86efac}}
  td.num.bad{{color:#fca5a5}}
  .pct{{padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600;white-space:nowrap}}
  .pct.up{{background:#14532d;color:#86efac}}
  .pct.down{{background:#7f1d1d;color:#fca5a5}}
  .pct.flat{{background:#334155;color:#94a3b8}}
  .pct.na{{color:#475569}}
  .badge{{padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700;letter-spacing:.4px}}
  .badge.ok{{background:#14532d;color:#86efac}}
  .badge.bad{{background:#7f1d1d;color:#fca5a5}}
  .chip{{display:inline-block;background:#334155;color:#cbd5e1;padding:2px 7px;
        border-radius:3px;font-size:10.5px;margin:1px;white-space:nowrap}}
  .chip em{{color:#94a3b8;font-style:normal;font-size:9.5px}}
  .muted{{color:#475569}}
  td.reasons{{max-width:240px}}
  .footer{{margin-top:14px;color:#64748b;font-size:11px;line-height:1.6}}
  .footer b{{color:#94a3b8}}
</style></head><body>

<h1>Cohort-Sales · Live Dashboard</h1>
<div class="sub">
  Generated <b style="color:#cbd5e1">{esc(data['generated_at_ist'])}</b> ·
  today window since {esc(data['today_window_ist'])} ·
  auto-refresh 30s · data refresh ~5 min · <b style="color:#cbd5e1">{len(rows)} reps</b>
</div>

<div class="summary">
  <div class="kpi"><span>Short calls today</span><b>{totals['short_today']}</b></div>
  <div class="kpi"><span>Long calls today</span><b>{totals['long_today']}</b></div>
  <div class="kpi"><span>Follow-ups today</span><b>{totals['followups_today']}</b></div>
  <div class="kpi"><span>Meetings today</span><b>{totals['meetings_today']}</b></div>
  <div class="kpi"><span>NI marked today</span><b>{totals['ni_today']}</b></div>
  <div class="kpi"><span>DQ marked today</span><b>{totals['dq_today']}</b></div>
  <div class="kpi"><span>Payment prospects today</span><b>{totals['pp_today']}</b></div>
  <div class="kpi"><span>DNP backlog</span><b>{totals['dnp_total']}</b></div>
</div>

<div class="wrap"><table>
<thead>
<tr>
  <th rowspan="2">Rep</th>
  <th colspan="2">Short calls (&lt;2 min)</th>
  <th colspan="2">Long calls (&ge;5 min)</th>
  <th colspan="2">Follow-ups (WA / email)</th>
  <th colspan="2">Meetings</th>
  <th colspan="2">DNP hygiene</th>
  <th colspan="2">NI / DQ today</th>
  <th rowspan="2">NI · DQ top reasons</th>
  <th rowspan="2">Payment prospects today</th>
</tr>
<tr>
  <th>Count</th><th>Δ vs y'day</th>
  <th>Count</th><th>Δ vs y'day</th>
  <th>Count</th><th>Pass / Fail</th>
  <th>Count</th><th>Δ vs y'day</th>
  <th>DNP total</th><th>Median calls after DNP</th>
  <th>NI</th><th>DQ</th>
</tr>
</thead>
<tbody>
{body_html}
</tbody>
</table></div>

<div class="footer">
<b>Definitions:</b><br>
• <b>Short</b> = call &lt; 2 min · <b>Long</b> = call &ge; 5 min · mid (2–5m) not shown but counted in follow-up pass/fail.<br>
• <b>Δ (improvement %)</b> is sign-flipped where appropriate: <b>positive always means improvement</b> — fewer short calls, more long calls, more meetings, vs yesterday full-day.<br>
• <b>Follow-up Pass</b> = `count(communications logged today) ≥ count(short+mid+long calls today)`. Every call should have a follow-up message.<br>
• <b>DNP median</b> = median number of calls logged on a contact AFTER it was first marked DNP. Lower is cleaner hygiene (i.e. you stop calling people who already said don't pick).<br>
• <b>NI / DQ today</b> = contacts whose `contact_engagement_stage` flipped to NI / Disqualified within today's IST window. Watch these grow vs payment prospects.<br>
• <b>Top reasons</b> aggregated from current NI/DQ contacts under each rep (top 3 with counts).<br>
• <b>Payment prospects today</b> = contacts moved INTO the payment_prospect stage today. Conversion signal.<br>
</div>

</body></html>"""


# ---------- output ------------------------------------------------------

def write_outputs(data):
    out_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(out_dir, "dashboard.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    with open(os.path.join(out_dir, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(render_html(data))
    unmapped = sorted({(r["name"], r["owner_id"]) for r in data["rows"] if r["manager"] == "Unassigned"})
    if unmapped:
        log(f"!! {len(unmapped)} unmapped reps — paste these into MANAGER_MAP in dashboard.py:")
        for n, oid in unmapped:
            log(f'     "{oid}": "<Anand|Puja|Harshitha PS>",   # {n}')
    log(f"Wrote dashboard.html + dashboard.json — {len(data['rows'])} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single run then exit")
    ap.add_argument("--interval", type=int, default=180, help="seconds between refreshes")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap rep count for smoke testing (0 = all)")
    ap.add_argument("--skip-slow", action="store_true",
                    help="skip DNP median + top reasons (faster smoke test)")
    ap.add_argument("--insecure", action="store_true",
                    help="disable SSL verify (use only when behind a TLS-inspecting proxy)")
    ap.add_argument("--write-slow-snapshot", action="store_true",
                    help="compute slow metrics (DNP median + top reasons) and "
                         "write slow_metrics.json. Used by the daily workflow.")
    args = ap.parse_args()
    # --write-slow-snapshot forces slow metrics to be computed.
    if args.write_slow_snapshot:
        args.skip_slow = False
    global HEADERS, SSL_VERIFY
    if args.insecure:
        SSL_VERIFY = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log("SSL verification DISABLED (--insecure).")
    tok = load_token()
    HEADERS = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    log(f"Token loaded ({tok[:12]}…). Dashboard starting...")
    while True:
        t0 = time.time()
        try:
            data = collect_all(limit=args.limit, skip_slow=args.skip_slow)
            write_outputs(data)
            if args.write_slow_snapshot:
                write_slow_snapshot(data["rows"])
        except Exception as e:
            log(f"FATAL collect error: {e}")
            import traceback; traceback.print_exc()
        if args.once:
            return
        elapsed = time.time() - t0
        sleep_for = max(args.interval - elapsed, 5)
        log(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
