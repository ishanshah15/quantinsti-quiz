# QuantInsti Quiz

A live, multiplayer quiz for internal sessions. One screen hosts and projects; everyone
else joins from their phone. Questions are authored in Excel, uploaded through the browser,
and every answer is logged to Google Sheets.

## Running it

```bash
pip install -r requirements.txt
python3 server.py            # http://localhost:8000
```

- `/` — participant join screen (this is the link/QR you share)
- `/host` — Host Panel (sign in with `ADMIN_USER` / `ADMIN_PASS`, default `admin` / `pass@123`)
- `/health` — status, build version, and whether Sheets is connected

Change the password before any real deployment: set `ADMIN_PASS` in `.env` or in the host's
environment variables.

## Google Sheets

Everything persists to one spreadsheet, in three tabs the app creates on demand:

| Tab | Contents |
|---|---|
| `Quiz Library` | Every uploaded quiz's questions. Deletes are soft — a `Deleted` flag, so a mistake is recoverable by clearing it. |
| `Results` | One row per participant per question: timestamp, subject, topic, host, question, name, email, answer given, correct answer, result, points. |
| `session_state` | A single snapshot so a server restart can resume a quiz that is mid-session. |

**Setup, one time:** share the spreadsheet with the service account as **Editor**.
Without this, the app still runs perfectly — it just keeps everything in memory and loses
it on restart. The Host Panel says so plainly under the quiz list.

Credentials come from either:
- `service_account.json` next to `server.py` (picked up automatically — this is the local setup), or
- `GOOGLE_SERVICE_ACCOUNT_JSON` holding the raw key JSON (this is how Render/Railway do it).

Point a second deployment at its own spreadsheet with `QUIZ_SHEET_ID`.

## Authoring questions

In the Host Panel, switch to **Manage Quizzes**:

1. Download the Excel template. It ships with a worked example plus two reference sheets:
   **Guidelines** (how to write a question that diagnoses understanding rather than recall)
   and **Values** (the approved skill levels, time-limit guidance, and distractor patterns).
2. Optionally copy the on-screen AI prompt, attach the template, and have an assistant draft
   the questions. Review the draft against the Guidelines sheet before uploading — the
   correct-answer distribution and distractor quality are the two things that most often
   need fixing.
3. Enter Subject and Topic, drop the `.xlsx` in, upload.

Only `Question`, `Option A`–`D` and `Correct Answer` are required. `Concept / Thing Tested`
and `Time Limit (sec)` are used if present; the concept appears in the results report, which
is what makes the report useful for spotting which ideas the room actually missed.

## Running a session

**Host a Quiz** mode drives the live session. **Manage Quizzes** mode never opens the game
socket, so someone uploading a quiz physically cannot disturb a session in progress. If two
people are in Host mode at once, both get a banner naming the other.

Answer options stay hidden for the first 5 seconds of each question so people read the
question before racing. Scoring is 500 points for a correct answer plus up to 500 more for
speed, measured from when the options appear.

After each question the session waits on the host — there is no auto-advance, so discussing
an answer never runs into a timer. At the end, **Download Results** produces a workbook with
a question-by-participant matrix and each person's total.

## Participants

Name and email. Email is the identity, so someone who drops off, refreshes, or switches
devices rejoins with their score intact. Late joins are allowed — they start at zero and
drop into the current question. The host can remove someone from the lobby.

## Sound

Synthesised in the browser with the Web Audio API (`quiz_audio.js`, served at `/audio.js`).
There are no audio files, nothing to download, and it works with no network.

The host screen carries the music, since it is the one in the room: a quiet lobby loop, a
pulse under each question that doubles in tempo for the closing stretch, stings on the
answer reveal and leaderboard, and a fanfare on the podium. Phones get short cues only —
answer locked in, right, wrong, out of time — because twenty phones running a music bed is
just noise. Both pages have a sound toggle and remember the choice.

Browsers will not start audio before a user gesture, so it unlocks on the first tap or click.
Anything triggered before that is a silent no-op.

## Files

| File | |
|---|---|
| `server.py` | FastAPI app: game state machine, WebSockets, upload/download, auth |
| `sheets_store.py` | Google Sheets persistence; degrades to no-ops when unconfigured |
| `host.html` | Host Panel — live hosting and quiz management |
| `player.html` | Participant screen |
| `quiz_audio.js` | Web Audio sound engine for both pages |
| `questions.json` | Bundled fallback quiz, used when Sheets is unreachable |

## Deploying

`render.yaml`, `Procfile` and `railway.toml` are all set up for a Python web service running
`uvicorn server:app`. Bump `APP_VERSION` in `server.py` on each deploy — it shows in small
print on both pages, so you can confirm at a glance what is actually live.
