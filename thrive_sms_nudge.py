#!/usr/bin/env python3
"""
Thrive — tutor SMS nudge (the "day 3" rung of the reminder ladder).

WHAT IT DOES, once per weekday morning:
  1. Reads every unconfirmed lesson from TutorCruncher (READ-ONLY).
  2. Groups them by tutor and counts how many days overdue each is.
  3. Texts a tutor ONE short nudge (+ approve link) only if ALL are true:
       - they have a mobile number on file, AND
       - they are >= SMS_MIN_DAYS overdue, AND
       - we have NOT already texted them in the last COOLDOWN_DAYS.
  4. Tutors who approve simply drop off the list on the next run — no off-switch
     to remember. No-mobile tutors stay on email. 7+ days is a human's job.

STATELESS BY DESIGN (no database):
  "Did we already text this person?" is answered by querying Twilio's own
  message history — not a local file — so it runs the same on GitHub Actions
  as it does on a laptop, with nothing to persist or corrupt.

SAFE BY DEFAULT:
  - Touches NOTHING in TutorCruncher (only GET requests).
  - DRY_RUN is the default: it prints exactly who would be texted and the exact
    message, and sends nothing.
  - Even with --send, if TEST_TO is set it sends ONLY to that one number, so you
    can prove the whole pipe end-to-end against your own phone before any tutor
    is ever messaged.

CONFIG (environment variables / GitHub Actions secrets):
  TC_API_KEY            TutorCruncher API token            (required)
  TWILIO_ACCOUNT_SID    Twilio account SID                 (required for --send)
  TWILIO_AUTH_TOKEN     Twilio auth token                  (required for --send)
  TWILIO_FROM           Twilio number, e.g. +16155551234   (required for --send)
  TC_SUBMIT_URL         link the text points tutors to     (default: TC login)
  TEST_TO               if set, ALL texts go here instead  (e.g. your own phone)

USAGE:
  python3 thrive_sms_nudge.py            # dry-run: prints, sends nothing
  python3 thrive_sms_nudge.py --send     # live (honors TEST_TO if set)
"""
import os, sys, time, argparse, datetime as dt
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("python3 -m pip install requests")

# ---- config ----------------------------------------------------------------
BASE = os.environ.get("TC_API_BASE", "https://secure.tutorcruncher.com/api").rstrip("/")
TODAY = dt.date.today()
SMS_MIN_DAYS = int(os.environ.get("SMS_MIN_DAYS", "3"))   # text at day 3+
ESCALATE_DAYS = int(os.environ.get("ESCALATE_DAYS", "5")) # 5+ = human (flagged, not texted)
COOLDOWN_DAYS = int(os.environ.get("COOLDOWN_DAYS", "7")) # never re-text within this
SUBMIT_URL = os.environ.get("TC_SUBMIT_URL", "https://secure.tutorcruncher.com/")
TEST_TO = (os.environ.get("TEST_TO") or "").strip()
DELAY = 0.35  # be gentle with the TC API


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


# ---- TutorCruncher (read-only) ---------------------------------------------
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
            wait = 4 * (attempt + 1)
            print(f"   throttled ({r.status_code}); waiting {wait}s...")
            time.sleep(wait)
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


def behind_by_tutor():
    """{contractor_id: {name, max_days_overdue, count}} for unconfirmed lessons."""
    stubs = paged("appointments", status="awaiting-report")
    stubs += paged("appointments", status="planned",
                   start_lte=TODAY.isoformat(),
                   start_gte=(TODAY - dt.timedelta(days=120)).isoformat())
    by_tutor = {}
    for s in {a["id"]: a for a in stubs}.values():
        d = get(f"appointments/{s['id']}")
        cj = (d.get("cjas") or [{}])[0]
        cid = cj.get("contractor")
        if not cid:
            continue
        try:
            days = (TODAY - dt.date.fromisoformat((d.get("start") or "")[:10])).days
        except ValueError:
            continue
        if days < 0:
            continue
        t = by_tutor.setdefault(cid, {"name": cj.get("name"), "max_days": 0, "count": 0})
        t["count"] += 1
        t["max_days"] = max(t["max_days"], days)
    return by_tutor


# ---- Twilio ----------------------------------------------------------------
def twilio_creds():
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    return sid, tok, frm


def recently_texted_numbers():
    """STATELESS dedup: numbers Twilio shows we texted within COOLDOWN_DAYS.
    Returns a set of destination numbers, or None if Twilio isn't configured."""
    sid, tok, _ = twilio_creds()
    if not (sid and tok):
        return None
    since = (dt.datetime.utcnow() - dt.timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    seen, page_url = set(), f"{url}?DateSent%3E={since}&PageSize=200"
    while page_url:
        r = requests.get(page_url, auth=(sid, tok), timeout=30)
        r.raise_for_status()
        data = r.json()
        for m in data.get("messages", []):
            if m.get("to"):
                seen.add(m["to"])
        nxt = data.get("next_page_uri")
        page_url = f"https://api.twilio.com{nxt}" if nxt else None
    return seen


def send_sms(to, body, live):
    sid, tok, frm = twilio_creds()
    dest = TEST_TO or to                      # TEST_TO overrides every recipient
    if not live:
        tag = f" (would route to {dest})" if TEST_TO else ""
        print(f"  [DRY-RUN] -> {to}{tag}\n             {body}")
        return False
    if not (sid and tok and frm):
        sys.exit("Set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM to --send.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    r = requests.post(url, auth=(sid, tok),
                      data={"To": dest, "From": frm, "Body": body}, timeout=30)
    if r.status_code >= 300:
        print(f"  ! Twilio error to {dest}: {r.status_code} {r.text[:160]}")
        return False
    print(f"  SENT -> {dest}")
    return True


# ---- message ---------------------------------------------------------------
def message_for(first_name, n_sessions):
    first = first_name or "there"
    s = "s" if n_sessions != 1 else ""
    return (f"Hi {first}, it's Thrive. You have {n_sessions} session{s} waiting on "
            f"your approval — please approve so payroll and family billing can go "
            f"through. Takes a minute: {SUBMIT_URL}  Reply STOP to opt out.")


# ---- run --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (else dry-run)")
    args = ap.parse_args()

    by_tutor = behind_by_tutor()
    already = recently_texted_numbers()           # set, or None in pure dry-run
    if already is None:
        print("(Twilio not configured — dedup check skipped; this is fine for dry-run.)\n")

    texted = held = no_mobile = escalate = skipped_dupe = 0
    print(f"Scanning {len(by_tutor)} tutors with unconfirmed lessons "
          f"(text at {SMS_MIN_DAYS}+ days, escalate at {ESCALATE_DAYS}+):\n")

    for cid, info in by_tutor.items():
        days = info["max_days"]
        if days < SMS_MIN_DAYS:
            held += 1                              # TC emails still cover days 1-2
            continue
        c = get(f"contractors/{cid}")
        mobile = (c.get("mobile") or "").strip()
        if not mobile:
            no_mobile += 1                         # stays on email + human chase
            continue
        if already is not None and mobile in already:
            skipped_dupe += 1                      # texted within cooldown -> skip
            continue
        if days >= ESCALATE_DAYS:
            escalate += 1                          # flagged for a human (still texts once)
        body = message_for(c.get("first_name") or info["name"], info["count"])
        if send_sms(mobile, body, live=args.send):
            texted += 1

    print(f"\n{'SENT' if args.send else 'DRY-RUN'} summary:")
    print(f"  texted/queued                    : {texted}")
    print(f"  held (<{SMS_MIN_DAYS}d, TC email covers)  : {held}")
    print(f"  no mobile (email + human)        : {no_mobile}")
    print(f"  skipped (texted within {COOLDOWN_DAYS}d)   : {skipped_dupe}")
    print(f"  at escalation ({ESCALATE_DAYS}+ days)        : {escalate}  <- for Mallory/Evan (parked)")


if __name__ == "__main__":
    main()
