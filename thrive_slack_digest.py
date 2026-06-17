#!/usr/bin/env python3
"""
Thrive — weekly Slack digest of unconfirmed lessons (READ-ONLY).

Posts one message to a Slack channel bucketing every tutor with unconfirmed
lessons into three tiers, so a human can see the whole field at a glance:

  In sequence   3-6 days   the SMS nudge is working these
  Needs a human 7-29 days  someone should reach out
  Stale         30+ days   old backlog (likely void)

Reads TutorCruncher only with GET. Writes nothing there. Posts to Slack via an
Incoming Webhook. If SLACK_WEBHOOK_URL isn't set, it prints the digest instead
(so you can test it in an Actions log without touching Slack).

CONFIG (env / GitHub Actions secrets):
  TC_API_KEY          TutorCruncher token             (required)
  SLACK_WEBHOOK_URL   Slack Incoming Webhook URL      (optional; prints if absent)

USAGE:
  python3 thrive_slack_digest.py
"""
import os, sys, time, datetime as dt
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("python3 -m pip install requests")

BASE = os.environ.get("TC_API_BASE", "https://secure.tutorcruncher.com/api").rstrip("/")
TODAY = dt.date.today()
WEBHOOK = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
DELAY = 0.35

# tier edges (days overdue, by newest session)
SEQ_MIN, SEQ_MAX = 0, 5      # in follow-up sequence: 0–5 days
HUMAN_MIN, HUMAN_MAX = 6, 29  # needs escalation: 5+ days late
STALE_MIN = 30               # stale: 30+ days


def tc_key():
    k = os.environ.get("TC_API_KEY")
    if not k:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tc_key")
        if os.path.exists(p):
            k = open(p).read()
    k = (k or "").strip()
    if not k or " " in k or "<" in k:
        sys.exit("No valid TC_API_KEY (env var or .tc_key file).")
    return k


H = {"Authorization": f"token {tc_key()}", "Accept": "application/json"}


def get(path, **p):
    url = f"{BASE}/{path.lstrip('/')}"
    if p:
        url += "?" + urlencode({k: v for k, v in p.items() if v is not None})
    for attempt in range(5):
        r = requests.get(url, headers=H, timeout=30)
        if r.status_code == 200:
            time.sleep(DELAY)
            return r.json()
        if r.status_code in (429, 403, 503) or "Just a moment" in r.text[:120]:
            time.sleep(4 * (attempt + 1))
            continue
        sys.exit(f"HTTP {r.status_code} on {path}: {r.text[:160]}")
    sys.exit(f"Gave up on {path}.")


def paged(path, **p):
    out, page = [], 1
    while True:
        d = get(path, page=page, **p)
        rows = d.get("results", d if isinstance(d, list) else [])
        out += rows
        if not d.get("next") or not rows:
            return out
        page += 1


def gather():
    """{cid: {name, lessons:[{date,days,student,family,est}], count, est, max_days}}."""
    stubs = paged("appointments", status="awaiting-report")
    stubs += paged("appointments", status="planned", start_lte=TODAY.isoformat())
    by_tutor = {}
    for s in {a["id"]: a for a in stubs}.values():
        d = get(f"appointments/{s['id']}")
        cj = (d.get("cjas") or [{}])[0]
        cid = cj.get("contractor") or "none"   # keep tutor-less lessons visible
        try:
            days = (TODAY - dt.date.fromisoformat((d.get("start") or "")[:10])).days
        except ValueError:
            continue
        if days < 0:
            continue
        rcra = (d.get("rcras") or [{}])[0]
        try:
            units = float(d.get("units") or 0) or 1   # match report: blank units -> 1
            est = float(rcra.get("charge_rate") or 0) * units
        except (TypeError, ValueError):
            est = 0.0
        t = by_tutor.setdefault(cid, {"name": cj.get("name") or "(no tutor on file)",
                                      "lessons": []})
        t["lessons"].append({"date": (d.get("start") or "")[:10], "days": days,
                             "student": rcra.get("recipient_name") or "?",
                             "family": rcra.get("paying_client_name") or rcra.get("recipient_name") or "?",
                             "est": est, "status": d.get("status") or "?"})
    for t in by_tutor.values():
        t["count"] = len(t["lessons"])
        t["est"] = sum(l["est"] for l in t["lessons"])
        t["max_days"] = max((l["days"] for l in t["lessons"]), default=0)  # oldest
        t["min_days"] = min((l["days"] for l in t["lessons"]), default=0)  # newest
        t["lessons"].sort(key=lambda l: l["days"])
    return by_tutor


def fmt_date(s):
    try:
        d = dt.date.fromisoformat(s)
        return d.strftime("%b %-d") if d.year == TODAY.year else d.strftime("%b %-d, %Y")
    except ValueError:
        return s or "?"


def get_contact(cid):
    """Tutor email + mobile, for the tiers a human acts on. ('' if unavailable.)"""
    if cid == "none":
        return "", ""
    c = get(f"contractors/{cid}")
    return (c.get("email") or "").strip(), (c.get("mobile") or "").strip()


def tier(newest_days):
    # Tier by the NEWEST unconfirmed session (matches the board), so a tutor with
    # a recent lesson surfaces as actionable even if they also have old ones.
    if newest_days <= SEQ_MAX:
        return "seq"
    if newest_days <= HUMAN_MAX:
        return "human"
    return "stale"


STALE_SHOW = int(os.environ.get("STALE_SHOW", "8"))  # how many legacy tutors to name


def day_range(t):
    return f"{t['min_days']}d" if t["min_days"] == t["max_days"] else f"{t['min_days']}–{t['max_days']}d"


def session_line(l):
    # column order: Date | Days | Student | Family | Cost | Status
    return f"    {l['date']} | {l['days']}d | {l['student']} | {l['family']} | ${l['est']:,.0f} | {l['status']}"


def tutor_block(t):
    """Name + rollup (sessions | $) on the SAME line, contact below, then EVERY session."""
    email, mobile = get_contact(t["cid"])
    contact = " · ".join([x for x in [email, mobile or "no mobile"] if x])
    n = t["count"]
    out = [f"*{t['name']}* — {n} session{'s' if n != 1 else ''} | ${t['est']:,.0f}"]
    if contact:
        out.append(f"    {contact}")
    for l in t["lessons"]:             # list ALL sessions, never truncate
        out.append(session_line(l))
    return out


def stale_line(t):
    """One line per stale tutor: name — sessions | $ — contact (no lesson rows)."""
    email, mobile = get_contact(t["cid"])
    contact = " · ".join([x for x in [email, mobile or "no mobile"] if x])
    n = t["count"]
    base = f"*{t['name']}* — {n} session{'s' if n != 1 else ''} | ${t['est']:,.0f}"
    return base + (f" — {contact}" if contact else "")


def build_text(by_tutor):
    buckets = {"seq": [], "human": [], "stale": []}
    total = lessons = tutors = 0
    for cid, t in by_tutor.items():
        t["cid"] = cid
        buckets[tier(t["min_days"])].append(t)
        total += t["est"]; lessons += t["count"]; tutors += 1
    buckets["seq"].sort(key=lambda x: x["min_days"])
    buckets["human"].sort(key=lambda x: x["min_days"])
    buckets["stale"].sort(key=lambda x: x["est"], reverse=True)
    need, ontrack = len(buckets["human"]), len(buckets["seq"])

    L = ["*Tutoring sessions awaiting confirmation*",
         f"_Academic tutoring branch · {TODAY:%A, %B %-d, %Y}_",
         "",
         f"*{lessons} unconfirmed lessons | {tutors} tutors | ${total:,.0f} unbilled | Action required {need}*",
         f"{ontrack} currently in follow-up sequence — flagged next week if they don't convert."]

    def section(key, emoji, title, sub):
        b = buckets[key]
        if not b:
            return
        st = sum(t["est"] for t in b)
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━")
        L.append(f"{emoji} *{title}* | {len(b)} tutor{'s' if len(b) != 1 else ''} | ${st:,.0f}")
        L.append(f"_{sub}_")
        L.append("_Date | Days | Student | Family | Cost | Status_")
        L.append("")
        for t in b:
            L.extend(tutor_block(t))
            L.append("")

    section("seq", "🟢", "In follow-up sequence", "0–5 days")
    section("human", "🟠", "Needs escalation", "5+ days late")

    stale = buckets["stale"]
    if stale:
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━")
        L.append(f"⚪ *Stale* | {len(stale)} tutor{'s' if len(stale) != 1 else ''} | ${sum(t['est'] for t in stale):,.0f}")
        L.append("_30+ days unconfirmed — review for cleanup_")
        L.append("")
        for t in stale:                # every person, one line each
            L.append(stale_line(t))

    L.append("")
    L.append("_Mark lessons complete in TutorCruncher before invoices can be sent._")
    return "\n".join(L)


def main():
    text = build_text(gather())
    if not WEBHOOK:
        print("(No SLACK_WEBHOOK_URL — printing digest instead of posting)\n")
        print(text)
        return
    r = requests.post(WEBHOOK, json={"text": text}, timeout=30)
    if r.status_code >= 300:
        sys.exit(f"Slack error: {r.status_code} {r.text[:160]}")
    print("Posted to Slack.")


if __name__ == "__main__":
    main()
