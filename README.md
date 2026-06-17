# Thrive SMS nudge

A small automation that texts tutors who haven't approved their TutorCruncher
sessions, so billing stays clean and nobody has to chase them by hand. It is
**one rung** in the reminder ladder:

| Day | What happens | Who runs it |
|-----|--------------|-------------|
| 1–2 | TutorCruncher's own reminder emails | TutorCruncher |
| **3** | **One SMS nudge + approve link** | **this automation** |
| 4+ | TutorCruncher emails continue | TutorCruncher |
| 5+ | Still unconfirmed → flag a human (Mallory/Evan) | manual, for now |

The moment a tutor approves, they drop off the list and stop getting texted.
No per-person off switch to remember.

## How it works (plain version)
Once each weekday morning, GitHub Actions runs `thrive_sms_nudge.py`. The script:
1. Reads the unconfirmed lessons from TutorCruncher — **read-only, it never
   changes anything in TutorCruncher.**
2. Groups them by tutor and keeps only those who (a) have a mobile number,
   (b) are 3+ days overdue, and (c) haven't already been texted in the last 7 days.
3. Sends each one short text via Twilio with a link to approve.

**No database.** The "did we already text them?" check asks Twilio's own message
history, so there's nothing to store or maintain.

## Where the keys live
Never in the code. They're stored in **Settings → Secrets and variables →
Actions** on this repo:

| Secret | What it is |
|--------|-----------|
| `TC_API_KEY` | TutorCruncher API token (read-only use) |
| `TWILIO_ACCOUNT_SID` | from the Twilio console |
| `TWILIO_AUTH_TOKEN` | from the Twilio console |
| `TWILIO_FROM` | the Twilio phone number, e.g. `+16155551234` |
| `TC_SUBMIT_URL` | the link the text sends tutors to |
| `TEST_TO` | **while testing:** your own phone — every text routes here instead of to tutors. **Delete this secret to go live.** |

## The rollout ladder (do these in order)
1. **Dry-run.** Run it with no `--send` (or read an Actions log) — it prints
   exactly who *would* be texted and the message, and sends nothing.
2. **Test to yourself.** Set the `TEST_TO` secret to your own number, run it —
   every message comes to you. No tutor is touched.
3. **Small group.** Once it reads right, test against a couple of real tutors.
4. **Go live.** Delete the `TEST_TO` secret. It now texts the real list.

## How to watch / run / stop it
- **Watch:** the **Actions** tab shows every run — when it fired, what it did,
  green check or red X. Click any run to read the full log.
- **Run it now:** Actions tab → "Thrive SMS nudge" → **Run workflow** button.
- **Turn it off:** Actions tab → "Thrive SMS nudge" → **⋯ → Disable workflow.**

## Good to know
- **Schedule:** weekday mornings, ~9–10am US Central (`cron` in the workflow is
  UTC and doesn't shift for daylight saving; adjust the hour if you care to the minute).
- **GitHub pauses scheduled jobs after ~60 days with no commits** to the repo.
  If it ever goes quiet, GitHub emails the repo owner with a one-click re-enable.
- **Opt-outs (STOP)** are handled automatically by Twilio once the number is
  registered; the message includes the STOP line.
- Local testing: drop your TutorCruncher key in a file named `.tc_key` in this
  folder (it's git-ignored) and run `python3 thrive_sms_nudge.py`.
