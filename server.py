import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import openpyxl
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

import sheets_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quiz")

BASE = Path(__file__).parent            # always the quiz/ directory
IST = ZoneInfo("Asia/Kolkata")

# Bump on every deploy. Shown in tiny print on the host and player pages so a
# glance tells you whether the live site is actually running the new build.
APP_VERSION = "2026.08.10-1"

# Optional human label for this deployment (e.g. "Mumbai"). Surfaced on the host
# and player pages and in /health so whoever runs a quiz can confirm at a glance
# which instance is live.
INSTANCE_LABEL = os.environ.get("INSTANCE_LABEL", "").strip()


def _load_dotenv(path: Path):
    """Minimal .env loader so local runs can set secrets without exporting them."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE / ".env")

# Default to the service-account key sitting next to this file, so a local run
# needs no env var at all. An explicit GOOGLE_SERVICE_ACCOUNT_FILE/JSON wins.
_local_key = BASE / "service_account.json"
if _local_key.exists():
    os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", str(_local_key))

PORT = int(os.environ.get("PORT", 8000))


def _now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p")


app = FastAPI()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_DEFAULT_PASS = "pass@123"
ADMIN_PASS = os.environ.get("ADMIN_PASS", _DEFAULT_PASS)
HOST_TOKEN = hashlib.sha256(f"{ADMIN_USER}:{ADMIN_PASS}:qi-quiz".encode()).hexdigest()[:32]

if ADMIN_PASS == _DEFAULT_PASS:
    # This default is published in a public repository, so on any host that
    # strangers can reach it is equivalent to no password at all: anyone could
    # sign into the Host Panel and disrupt a live session. Fine for localhost,
    # not fine for a deployment.
    logger.warning(
        "The Host Panel is using the built-in default password. Set the ADMIN_PASS "
        "environment variable before exposing this server to anyone else."
    )

# Participants are identified by email, so a rejoin restores the right score even
# from a different device or after a name edit.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

with open(BASE / "questions.json") as f:
    _default_questions = json.load(f)["questions"]

# quizzes: quiz id -> {"subject", "topic", "questions": [...], "uploaded_at", "uploaded_by"}
quizzes: dict[int, dict] = {
    0: {"subject": "AI at Work", "topic": "Custom GPTs", "questions": _default_questions,
        "uploaded_at": "", "uploaded_by": ""},
}

# Quizzes uploaded in past sessions, persisted in the Quiz Library tab.
_next_quiz_id = 1
for _qid, _entry in sheets_store.load_templates().items():
    quizzes.setdefault(_qid, _entry)
    _next_quiz_id = max(_next_quiz_id, _qid + 1)

# Resume with the most recently uploaded quiz after a restart, instead of
# silently falling back to the bundled sample quiz.
active_quiz_id: int = max(quizzes.keys())
QUESTIONS: list = quizzes[active_quiz_id]["questions"]
current_host_name: str = ""


REVEAL_DELAY = 5  # seconds before answer options are shown to participants

class Game:
    def __init__(self):
        self.phase = "waiting"      # waiting | countdown | question | results | leaderboard | finished
        self.current_q = -1
        self.players: dict[str, dict] = {}   # name -> {score, answered, email, device_id}
        self.answers: dict[str, int] = {}    # name -> answer index (0-3)
        self.answer_times: dict[str, float] = {}  # name -> epoch time of answer
        self.q_start_time: float = 0
        self.timer_task: asyncio.Task | None = None
        self.countdown_task: asyncio.Task | None = None
        self.skip_leaderboard: bool = False
        self.last_results: dict | None = None
        self.global_time_limit: int | None = None  # overrides per-question time_limit when set
        # One entry per completed question, kept for the downloadable results
        # report: {"question", "concept", "correct", "answers": {name: idx}}.
        self.question_log: list[dict] = []
        self.started_at: str = ""  # IST timestamp of when the quiz began

    def reset(self):
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        if self.countdown_task and not self.countdown_task.done():
            self.countdown_task.cancel()
        self.__init__()


game = Game()
host_sockets: set[WebSocket] = set()   # every connected host panel (supports more than one at once)
host_names: dict[WebSocket, str] = {}  # host socket -> the host name it connected with
player_ws: dict[str, WebSocket] = {}   # name -> websocket


def _host_names() -> list[str]:
    """Names of every currently connected host panel, for the multi-host warning."""
    return sorted(n for n in host_names.values() if n)

SESSION_RESUME_MAX_AGE_HOURS = 6


def _try_resume_session():
    """Rebuild in-progress game state from the last saved snapshot, if there is
    one and it isn't too old. Snapshots are only ever taken right after a
    question is fully scored (the "leaderboard" checkpoint): a question that
    was mid-flight when the process stopped isn't resumable, so on restart the
    session simply continues from the last completed question via "Next Question"."""
    global active_quiz_id, QUESTIONS, current_host_name
    snap = sheets_store.load_session_snapshot()
    if not snap:
        return
    try:
        saved_at = datetime.strptime(snap["saved_at"], "%d %b %Y, %I:%M %p").replace(tzinfo=IST)
    except (KeyError, ValueError):
        return
    if (datetime.now(IST) - saved_at).total_seconds() > SESSION_RESUME_MAX_AGE_HOURS * 3600:
        return
    qid = snap.get("active_quiz_id")
    if qid not in quizzes:
        return
    active_quiz_id = qid
    QUESTIONS = quizzes[qid]["questions"]
    current_host_name = snap.get("host", "")
    game.phase = "leaderboard"
    game.current_q = snap.get("current_q", -1)
    game.players = snap.get("players", {})
    game.global_time_limit = snap.get("global_time_limit")
    game.question_log = snap.get("question_log", [])
    game.started_at = snap.get("started_at", "")


_try_resume_session()


# ── helpers ──────────────────────────────────────────────────────────────────

async def _send(ws: WebSocket, msg: dict):
    try:
        await ws.send_json(msg)
    except Exception:
        pass


async def _broadcast(msg: dict, include_host: bool = False):
    dead = []
    for name, ws in list(player_ws.items()):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(name)
    for name in dead:
        # Drop only the dead socket. The entry in game.players must survive so
        # the participant can reconnect with their score and identity intact.
        player_ws.pop(name, None)
    if include_host:
        await _to_host(msg)


async def _to_host(msg: dict):
    """Send to every connected host panel, not just one: opening a second host
    tab (or a diagnostic script) must never silently steal updates away from
    an existing, actively-used host session."""
    for ws in list(host_sockets):
        await _send(ws, msg)


def _leaderboard() -> list[dict]:
    return sorted(
        [{"name": n, "score": p["score"]} for n, p in game.players.items()],
        key=lambda x: x["score"],
        reverse=True,
    )


def _quiz_meta() -> dict:
    q = quizzes.get(active_quiz_id, {})
    return {"subject": q.get("subject", ""), "topic": q.get("topic", "")}


def _progress() -> dict:
    return {"current": game.current_q + 1, "total": len(QUESTIONS)}


def _session_snapshot() -> dict:
    return {
        "phase": game.phase,
        "current_q": game.current_q,
        "active_quiz_id": active_quiz_id,
        "global_time_limit": game.global_time_limit,
        "host": current_host_name,
        "players": game.players,
        "question_log": game.question_log,
        "started_at": game.started_at,
        "saved_at": _now_ist_str(),
    }


def _find_player_by_email(email: str) -> str | None:
    """Email is the real participant identity, so a rejoin from a new device or
    under a corrected name still lands on the same score."""
    if not email:
        return None
    target = email.strip().lower()
    for pn, pd in game.players.items():
        if str(pd.get("email", "")).strip().lower() == target:
            return pn
    return None


def _log_question_results(q: dict, correct: int, points_earned: dict[str, int]):
    """Fire-and-forget: append each participant's result for this question to the
    Results tab. Runs off the event loop so Sheets latency never stalls the game,
    and failures are swallowed inside sheets_store."""
    meta = quizzes.get(active_quiz_id, {})
    subject = meta.get("subject", "")
    topic = meta.get("topic", "")
    timestamp = _now_ist_str()
    correct_cell = f"{'ABCD'[correct]} - {q['options'][correct]}"

    rows = []
    for name, pdata in game.players.items():
        ans = game.answers.get(name)
        if ans is None:
            selected_cell = "No answer"
            result = "Incorrect"
            pts = 0
        else:
            selected_cell = f"{'ABCD'[ans]} - {q['options'][ans]}"
            result = "Correct" if ans == correct else "Incorrect"
            pts = points_earned.get(name, 0)
        rows.append([
            timestamp, active_quiz_id, subject, topic, current_host_name,
            game.current_q + 1, q["question"], name, pdata.get("email", ""),
            selected_cell, correct_cell, result, pts,
        ])

    if rows:
        asyncio.create_task(asyncio.to_thread(sheets_store.log_results, rows))


# ── game logic ────────────────────────────────────────────────────────────────

async def end_question():
    """Called when timer expires or all participants have answered."""
    if game.phase != "question":
        return
    game.phase = "results"                        # guard against re-entry

    # Only cancel the timer task if we are NOT being called from within it.
    # Cancelling the current task would raise CancelledError at the next await
    # (asyncio.sleep below), aborting this function before leaderboard is sent.
    current = asyncio.current_task()
    if game.timer_task and not game.timer_task.done() and current is not game.timer_task:
        game.timer_task.cancel()

    q = QUESTIONS[game.current_q]
    correct = q["correct"]
    time_limit = game.global_time_limit or q.get("time_limit", 20)
    answer_counts = [0, 0, 0, 0]

    # Pre-compute per-participant elapsed times so scoring is based on when they
    # answered, not when end_question() runs.
    now = time.time()
    points_earned: dict[str, int] = {}
    for name, ans in game.answers.items():
        if 0 <= ans <= 3:
            answer_counts[ans] += 1
        if ans == correct and name in game.players:
            elapsed = game.answer_times.get(name, now) - game.q_start_time
            base = 500
            # Offset by reveal delay so the full 0-500 bonus range is available
            # regardless of the configured time_limit.
            effective_elapsed = max(0.0, elapsed - REVEAL_DELAY)
            effective_window = max(1.0, time_limit - REVEAL_DELAY)
            speed_bonus = int(500 * max(0.0, 1 - effective_elapsed / effective_window))
            earned = base + speed_bonus
            game.players[name]["score"] += earned
            points_earned[name] = earned

    game.last_results = {
        "type": "results",
        "question": q["question"],
        "options": q["options"],
        "correct": correct,
        "correct_text": q["options"][correct],
        "answer_counts": answer_counts,
        "points_earned": points_earned,
        **_quiz_meta(),
    }
    await _broadcast(game.last_results, include_host=True)

    # Keep this question's outcome in memory so the host can download a full
    # per-participant report once the quiz finishes. game.answers is cleared on
    # the next question, so this is the only in-process record.
    game.question_log.append({
        "question": q["question"],
        "concept": q.get("concept", ""),
        "correct": correct,
        "answers": dict(game.answers),
    })

    _log_question_results(q, correct, points_earned)

    # Wait here until the host clicks "Show Leaderboard": no auto-advance, so
    # there's no time pressure to wrap up discussing the answer with the room.
    game.skip_leaderboard = False
    while game.phase == "results" and not game.skip_leaderboard:
        await asyncio.sleep(0.1)
    if game.phase != "results":
        return  # force-stopped (or otherwise moved on) while waiting

    lb = _leaderboard()
    is_last = game.current_q + 1 >= len(QUESTIONS)

    if is_last:
        game.phase = "finished"
        asyncio.create_task(asyncio.to_thread(sheets_store.clear_session_snapshot))
        await _broadcast({"type": "game_over", "leaderboard": lb, **_quiz_meta()}, include_host=True)
    else:
        game.phase = "leaderboard"
        asyncio.create_task(asyncio.to_thread(sheets_store.save_session_snapshot, _session_snapshot()))
        await _broadcast({"type": "leaderboard", "leaderboard": lb, **_quiz_meta(), **_progress()}, include_host=True)


async def _do_countdown():
    for i in [3, 2, 1]:
        if game.phase != "countdown":
            return
        await _broadcast({"type": "countdown", "value": i}, include_host=True)
        await asyncio.sleep(1)
    if game.phase != "countdown":
        return
    game.phase = "question"
    game.current_q = 0
    game.answers = {}
    game.answer_times = {}
    game.last_results = None
    game.question_log = []
    game.started_at = _now_ist_str()
    for p in game.players.values():
        p["answered"] = False
    game.q_start_time = time.time()
    await _broadcast(_build_question_msg(), include_host=True)
    game.timer_task = asyncio.create_task(_run_timer())
    game.countdown_task = None


async def _force_stop_game():
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    if game.countdown_task and not game.countdown_task.done():
        game.countdown_task.cancel()

    game.phase = "waiting"
    game.current_q = -1
    game.answers = {}
    game.answer_times = {}
    game.q_start_time = 0
    game.skip_leaderboard = False
    game.last_results = None
    game.question_log = []
    game.started_at = ""
    game.timer_task = None
    game.countdown_task = None
    for p in game.players.values():
        p["score"] = 0
        p["answered"] = False

    asyncio.create_task(asyncio.to_thread(sheets_store.clear_session_snapshot))

    await _broadcast(
        {
            "type": "force_stopped",
            "connected_count": len(player_ws),
        },
        include_host=True,
    )


def _effective_time_limit() -> int:
    """Return the time limit to use: global override if set, else per-question value.
    Clamped to a sane range so a bad uploaded value (0, negative, or non-numeric)
    can never make a question end instantly."""
    q = QUESTIONS[game.current_q]
    try:
        tl = int(game.global_time_limit or q.get("time_limit", 20))
    except (TypeError, ValueError):
        tl = 20
    return min(600, max(5, tl))


async def _run_timer():
    await asyncio.sleep(_effective_time_limit())
    if game.phase == "question":
        await end_question()


def _build_question_msg() -> dict:
    q = QUESTIONS[game.current_q]
    return {
        "type": "question_start",
        "index": game.current_q,
        "total": len(QUESTIONS),
        "question": q["question"],
        "options": q["options"],
        "time_limit": _effective_time_limit(),
        "elapsed": time.time() - game.q_start_time,  # seconds already spent on this question
        **_quiz_meta(),
    }


async def _send_current_state(websocket, name: str):
    """Bring a just-(re)joined participant's screen to the live game state: the
    current question (with their locked answer if any), the leaderboard, or the podium."""
    if game.phase == "question":
        await _send(websocket, _build_question_msg())
        if game.players.get(name, {}).get("answered"):
            await _send(websocket, {"type": "answer_received", "answer": game.answers.get(name, 0)})
    elif game.phase in ("results", "leaderboard"):
        await _send(websocket, {"type": "leaderboard", "leaderboard": _leaderboard(), **_quiz_meta(), **_progress()})
    elif game.phase == "finished":
        await _send(websocket, {"type": "game_over", "leaderboard": _leaderboard(), **_quiz_meta()})


# ── Downloadable quiz template ─────────────────────────────────────────────────
# Subject and Topic are collected at upload time (not per row), so they are NOT
# columns here. The Concept / Thing Tested and Skill Tested columns are kept: the
# uploader ignores them, but they guide both the author and any LLM used to draft
# questions. The Guidelines and Values sheets travel with the workbook so an LLM
# handed the template gets the full authoring brief.

SAMPLE_HEADER = [
    "Concept / Thing Tested", "Skill Tested", "Question",
    "Option A", "Option B", "Option C", "Option D",
    "Correct Answer", "Time Limit (sec)",
]

# [concept, skill, question, A, B, C, D, correct letter, time limit]
SAMPLE_QUESTION_ROWS = [
    ["Purpose of a custom GPT knowledge base", "Understanding", "Why does uploading a reference document to a custom GPT usually improve its answers?", "It permanently retrains the underlying model", "It gives the assistant grounded source material to draw from", "It makes the assistant respond faster", "It removes the need for clear instructions", "B", 45],
    ["Grounding instructions", "Application", "A custom GPT keeps giving generic answers despite an uploaded policy PDF. What is the most effective fix?", "Re-upload the same PDF a second time", "Instruct it to search the uploaded document before answering", "Shorten every user prompt", "Switch to a different chat interface", "B", 45],
    ["Setting tone durably", "Application", "A team wants a consistent brand voice from an assistant across every chat. What is the most reliable approach?", "Ask for the right tone at the start of each new chat", "Put examples of the desired voice in the assistant's standing instructions", "Upload a spreadsheet of past engagement metrics", "Use a longer prompt each time", "B", 40],
    ["Judging automation candidates", "Analysis", "Which task is the best first candidate to automate: high value, low risk?", "Making a final hiring decision between two candidates", "Turning messy meeting notes into a structured action table", "Judging a candidate's cultural fit from a transcript", "Approving a vendor payment without review", "B", 50],
    ["Human review in the loop", "Understanding", "Why does an automated workflow that drafts client emails still need human review before sending?", "Because drafting is slower without review", "Because the output can be confidently wrong and is outward-facing", "Because review reduces the token cost", "Because the tool cannot format email", "B", 40],
    ["Prompt specificity", "Application", "Which instruction is most likely to produce a usable first draft?", "Write something about our new course", "Write a 120-word LinkedIn post for working professionals on our new course, one clear benefit, no hype", "Write a great marketing post", "Write a post, make it engaging", "B", 45],
    ["Verifying model output", "Analysis", "An assistant returns a confident figure with no source. What is the right next step before using it in a report?", "Use it, since the assistant sounded certain", "Trace the figure to a primary source and confirm it", "Rephrase the prompt until the number changes", "Round the figure to look more cautious", "B", 50],
    ["Data handling discipline", "Recall", "Which practice best protects sensitive data when using an external AI tool?", "Paste the full raw dataset for better context", "Share only the minimum fields the task actually needs", "Disable the chat history feature only", "Rename the file before uploading it", "B", 35],
    ["Choosing structured output", "Application", "A workflow feeds an assistant's answer straight into a spreadsheet. What should the prompt require?", "A friendly narrative summary", "A fixed set of named fields in a consistent format", "The longest answer possible", "A bulleted list with emoji", "B", 45],
    ["Measuring whether automation helped", "Analysis", "Which measure best shows an automation actually delivered value?", "The number of prompts sent per week", "Time saved on the task with quality held steady", "The length of the assistant's answers", "How many people tried the tool once", "B", 50],
]

SAMPLE_ROWS = [SAMPLE_HEADER] + SAMPLE_QUESTION_ROWS

_LLM_INSTRUCTION = (
    "You are an experienced assessment designer writing on-screen multiple-choice questions for a "
    "QuantInsti internal training session with working professionals. The Subject and Topic are provided "
    "separately at upload time, so you do not need to repeat them in each row. For each question, provide: "
    "Concept / Thing Tested, Skill Tested, Question, Options A-D, Correct Answer, and Time Limit (sec). "
    "Use only the approved Skill Tested values: Recall, Understanding, Application, Analysis. "
    "Create exactly one clearly best answer. Make distractors plausible and based on common misconceptions, "
    "reversed relationships, wrong sequences, or correct facts used in the wrong context. "
    "Avoid trick questions, double negatives, all/none of the above, vague stems, overlapping options, and "
    "obvious answer-length clues. Shuffle the position of correct answers across A, B, C and D, avoid "
    "predictable patterns, and keep the distribution reasonably balanced across the full quiz. Write in clear "
    "professional English. Favour applied, scenario-based questions over pure recall. "
    "Return the output as a table matching the Quiz Template columns."
)

_GUIDELINES_ROWS = [
    ["QuantInsti Quiz — MCQ Authoring Guidelines", ""],
    ["", ""],
    ["1. Purpose", "Write questions that diagnose real understanding, not recall of wording. Every question should map to one clear concept and one skill."],
    ["2. Concept Tested", "State the exact concept being assessed in 3-8 words. Examples: \"Purpose of a knowledge base\", \"Judging automation candidates\", \"Verifying model output\". Avoid broad labels such as \"AI\"."],
    ["3. Applied over recall", "Use realistic work situations. Include questions that ask the participant to apply a principle, interpret a scenario, or pick the best course of action. Do not make every question a definition check."],
    ["4. Question stem", "Keep the stem complete, specific and self-contained. Ask one thing only. Avoid unnecessary background detail, double negatives, \"all of the above\", and trick wording."],
    ["5. Correct answer", "There must be exactly one clearly best answer. It should be accurate and fully answer the question. Avoid making it noticeably longer or more detailed than the distractors."],
    ["6. Distractors", "Use plausible wrong answers drawn from real misconceptions. Every distractor should look possible to someone who has not grasped the concept. Avoid joke options, overlapping answers, grammatical clues, and wording lifted from the stem."],
    ["7. Distractor sources", "Good distractors come from: reversed cause and effect, a correct fact used in the wrong context, an incomplete explanation, a common workplace misconception, an over-generalisation, or the right steps in the wrong order."],
    ["8. Time limit", "Use 30-35 seconds for recall, 40-45 for understanding and application, and 50-60 for scenarios, calculations or analysis. Adjust upward for long reading."],
    ["9. Balance of skills", "For a typical session, aim for roughly 15-25% Recall, 30-40% Understanding, 25-35% Application and 10-20% Analysis. Treat this as a design guide, not a rigid rule."],
    ["10. Quality check", "Before upload, verify: one clear answer; no clue from option length; distractors are plausible; language is precise; the concept and skill match the question."],
    ["11. Shuffle correct options", "Distribute correct answers across A, B, C and D. Avoid predictable patterns such as repeatedly using the same option or running A-B-C-D in order. Check the distribution before finalising."],
    ["", ""],
    ["Recommended workflow", "Owner defines the topic and concepts -> LLM drafts questions -> owner checks factual accuracy, language, distractor quality and correct-answer distribution -> a second reviewer signs off -> quiz is uploaded."],
]

_VALUES_ROWS = [
    ["Skill Tested", "Correct Answer", "Skill Definition", "Useful Verbs", "LLM Instruction"],
    ["Recall", "A", "Remember a fact, term, step or definition.", "identify, name, state, select", _LLM_INSTRUCTION],
    ["Understanding", "B", "Show meaning, relationships or reasons.", "explain, describe, compare, classify", ""],
    ["Application", "C", "Use knowledge in a realistic or unfamiliar work situation.", "apply, predict, determine, choose", ""],
    ["Analysis", "D", "Interpret evidence, connect ideas or infer the best explanation.", "interpret, infer, analyse, conclude", ""],
    ["", "", "", "", ""],
    ["", "", "", "", ""],
    ["Distractor Pattern", "Description", "Example", "Use When", ""],
    ["Reversed relationship", "Reverse the correct direction, cause or effect.", "\"Reviewing output slows the workflow down, so skip it.\"", "Testing cause and effect", ""],
    ["Correct fact, wrong context", "Use a true statement that does not answer this question.", "\"Prompts should be specific\" as the answer to a data-privacy question.", "Testing whether the participant selects relevant knowledge", ""],
    ["Incomplete explanation", "Give only part of the correct idea.", "\"It gives faster answers\" when the best answer is about grounding in source material.", "Testing depth of understanding", ""],
    ["Common misconception", "Use a widely held but incorrect belief.", "\"Uploading a document retrains the model.\"", "Diagnosing misconceptions", ""],
    ["Wrong sequence", "Reorder the steps of a process.", "\"Publish, then review, then draft.\"", "Testing processes and workflows", ""],
    ["Over-generalisation", "Take a valid rule too far.", "\"Automate every decision that involves text.\"", "Testing judgement and limits", ""],
]


def _build_sample_workbook() -> openpyxl.Workbook:
    """The downloadable .xlsx template: a Quiz Template sheet (filled with a
    worked example), plus Guidelines and Values reference sheets so an author or
    LLM has the full brief in one file."""
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()

    header_fill = PatternFill("solid", fgColor="173871")
    header_font = Font(bold=True, color="FFFFFF")
    title_font = Font(bold=True, size=13, color="173871")
    wrap = Alignment(wrap_text=True, vertical="top")

    # Sheet 1: Quiz Template (this is the sheet the uploader reads)
    ws = wb.active
    ws.title = "Quiz Template"
    for row in SAMPLE_ROWS:
        ws.append(row)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    for width, col in zip((26, 15, 52, 30, 30, 30, 30, 14, 14), "ABCDEFGHI"):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    # Sheet 2: Guidelines
    gs = wb.create_sheet("Guidelines")
    for row in _GUIDELINES_ROWS:
        gs.append(row)
    gs["A1"].font = title_font
    gs.column_dimensions["A"].width = 28
    gs.column_dimensions["B"].width = 100
    for row in gs.iter_rows(min_col=2, max_col=2):
        row[0].alignment = wrap

    # Sheet 3: Values (approved skills, LLM brief, distractor patterns)
    vs = wb.create_sheet("Values")
    for row in _VALUES_ROWS:
        vs.append(row)
    for cell in vs[1]:
        cell.font = header_font
        cell.fill = header_fill
    for cell in vs[8]:
        cell.font = Font(bold=True)
    for width, col in zip((18, 16, 44, 34, 70), "ABCDE"):
        vs.column_dimensions[col].width = width
    vs["E2"].alignment = wrap

    return wb


# ── quiz file parsing ─────────────────────────────────────────────────────────

def _rows_to_questions(rows: list) -> tuple[list | None, str | None]:
    """rows: list of row-tuples, first row is the header. Returns (questions, error)."""
    if not rows:
        return None, "That file is empty."
    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    idx_q = col("question")
    idx_a = col("option a", "a")
    idx_b = col("option b", "b")
    idx_c = col("option c", "c")
    idx_d = col("option d", "d")
    idx_correct = col("correct answer", "correct")
    idx_concept = col("concept / thing tested", "concept tested", "thing tested", "concept")
    if idx_concept is None:
        # Fall back to any header that starts with "concept" (e.g. "Concept/Thing Tested")
        for i, h in enumerate(header):
            if h.startswith("concept"):
                idx_concept = i
                break
    idx_time = col("time limit", "time limit (seconds)", "time limit (sec)", "time_limit")
    if idx_time is None:
        # Fall back to any header that starts with "time limit" (e.g. "Time Limit (sec)")
        for i, h in enumerate(header):
            if h.startswith("time limit"):
                idx_time = i
                break

    if None in (idx_q, idx_a, idx_b, idx_c, idx_d, idx_correct):
        return None, (
            "We couldn't find the right column headers. The first row must include: "
            "Question, Option A, Option B, Option C, Option D, and Correct Answer."
        )

    questions = []
    for i, row in enumerate(rows[1:], start=2):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        qtext = row[idx_q] if idx_q < len(row) else None
        if qtext is None or not str(qtext).strip():
            continue
        try:
            options = [row[idx_a], row[idx_b], row[idx_c], row[idx_d]]
        except IndexError:
            return None, f"Row {i} is missing one or more of the option columns."
        if any(o is None or not str(o).strip() for o in options):
            return None, f"Row {i}: all four options (A-D) are required."
        raw_correct = str(row[idx_correct]).strip().upper()
        if raw_correct in ("A", "B", "C", "D"):
            correct = "ABCD".index(raw_correct)
        elif raw_correct in ("1", "2", "3", "4"):
            correct = int(raw_correct) - 1
        else:
            return None, f"Row {i}: Correct Answer must be A, B, C, or D."
        q = {
            "question": str(qtext).strip(),
            "options": [str(o).strip() for o in options],
            "correct": correct,
        }
        if idx_concept is not None and idx_concept < len(row) and row[idx_concept] not in (None, ""):
            q["concept"] = str(row[idx_concept]).strip()
        if idx_time is not None and idx_time < len(row) and row[idx_time] not in (None, ""):
            try:
                q["time_limit"] = int(float(row[idx_time]))
            except (ValueError, TypeError):
                pass
        questions.append(q)

    if not questions:
        return None, "No valid questions were found in that file."
    return questions, None


def _store_quiz(subject: str, topic: str, questions: list) -> int:
    global _next_quiz_id
    quiz_id = _next_quiz_id
    _next_quiz_id += 1
    uploaded_at = _now_ist_str()
    quizzes[quiz_id] = {
        "subject": subject,
        "topic": topic,
        "questions": questions,
        "uploaded_at": uploaded_at,
        "uploaded_by": current_host_name,
    }
    asyncio.create_task(asyncio.to_thread(
        sheets_store.append_template, quiz_id, subject, topic, questions, current_host_name, uploaded_at,
    ))
    return quiz_id


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION, "instance": INSTANCE_LABEL,
            "sheets_connected": sheets_store.is_configured()}


@app.get("/api/version")
async def api_version():
    return {"version": APP_VERSION, "instance": INSTANCE_LABEL}


# no-cache = the browser must revalidate before reusing its copy, so every
# deploy is picked up on the next page load instead of serving stale HTML.
_HTML_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/")
async def player_page():
    return FileResponse(BASE / "player.html", headers=_HTML_HEADERS)


@app.get("/host")
async def host_page():
    return FileResponse(BASE / "host.html", headers=_HTML_HEADERS)


@app.post("/api/host-login")
async def api_host_login(request: Request):
    data = await request.json()
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
        return {"token": HOST_TOKEN}
    return JSONResponse({"error": "Invalid username or password"}, status_code=401)


def require_host(
    authorization: str = Header(default=""),
    x_host_token: str = Header(default=""),
) -> None:
    """Guard admin HTTP endpoints with the same token the host WebSocket uses.
    Accepts either an `Authorization: Bearer <token>` or `X-Host-Token` header."""
    token = x_host_token
    if not token and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token != HOST_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/quizzes")
async def api_list_quizzes(_: None = Depends(require_host)):
    items = [
        {
            "id": qid,
            "count": len(q["questions"]),
            "subject": q.get("subject", ""),
            "topic": q.get("topic", ""),
            "uploaded_at": q.get("uploaded_at", ""),
            "uploaded_by": q.get("uploaded_by", ""),
        }
        for qid, q in quizzes.items()
    ]
    items.sort(key=lambda x: x["id"], reverse=True)  # latest upload first
    return {
        "quizzes": items,
        "active": active_quiz_id,
        "sheets_connected": sheets_store.is_configured(),
    }


@app.get("/api/quiz/{quiz_id:int}")
async def api_get_quiz(quiz_id: int, _: None = Depends(require_host)):
    """Full question content for one quiz, so the host can preview it before starting."""
    q = quizzes.get(quiz_id)
    if not q:
        return JSONResponse({"error": "Quiz not found"}, status_code=404)
    return {
        "id": quiz_id,
        "subject": q.get("subject", ""),
        "topic": q.get("topic", ""),
        "uploaded_by": q.get("uploaded_by", ""),
        "uploaded_at": q.get("uploaded_at", ""),
        "questions": q["questions"],
    }


@app.post("/api/quiz/upload")
async def api_upload_quiz(
    subject: str = Form(...),
    topic: str = Form(...),
    file: UploadFile = File(...),
    _: None = Depends(require_host),
):
    subject = subject.strip()
    topic = topic.strip()
    if not subject or not topic:
        return JSONResponse({"error": "Please enter both a Subject and a Topic."}, status_code=400)

    filename = (file.filename or "").lower()
    content = await file.read()

    if filename.endswith(".xlsx"):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            rows = list(wb.active.iter_rows(values_only=True))
        except Exception:
            return JSONResponse({"error": "We couldn't read that Excel file. Please make sure it's a valid .xlsx file."}, status_code=400)
    else:
        return JSONResponse({"error": "Please upload a .xlsx file."}, status_code=400)

    questions, err = _rows_to_questions(rows)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    quiz_id = _store_quiz(subject, topic, questions)
    return {"ok": True, "id": quiz_id, "count": len(questions)}


@app.get("/api/quiz/sample.xlsx")
async def api_sample_xlsx():
    wb = _build_sample_workbook()
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=quiz_template.xlsx"},
    )


def _build_results_workbook() -> openpyxl.Workbook:
    """One sheet: quiz details on top, then a question-by-participant matrix of
    Correct / Incorrect / No answer, closed out by each participant's total
    correct answers and final score."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    meta = quizzes.get(active_quiz_id, {})
    # Column order = final ranking, so the report reads like the leaderboard.
    participants = sorted(game.players.keys(), key=lambda n: game.players[n]["score"], reverse=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quiz Results"

    title_font = Font(bold=True, size=14, color="173871")
    label_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="173871")
    header_font = Font(bold=True, color="FFFFFF")
    correct_font = Font(color="1B7F4D")
    incorrect_font = Font(color="C0392B")
    noanswer_font = Font(color="808080")
    wrap = Alignment(wrap_text=True, vertical="top")

    ws.append(["Quiz Results"])
    ws["A1"].font = title_font
    ws.append([])
    for label, value in [
        ("Date of Quiz", game.started_at or _now_ist_str()),
        ("Subject", meta.get("subject", "")),
        ("Topic", meta.get("topic", "")),
        ("Host", current_host_name),
    ]:
        ws.append([label, value])
        ws.cell(row=ws.max_row, column=1).font = label_font
    ws.append([])

    # Two header rows: participant name, then their email underneath.
    ws.append(["Q#", "Concept Tested", "Question", "Correct Answer"] + participants)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.fill = header_fill
        cell.font = header_font
    ws.append(["", "", "", "Email"] +
              [game.players[n].get("email", "") for n in participants])
    for cell in ws[ws.max_row]:
        cell.font = Font(italic=True, color="555555")

    correct_counts = {name: 0 for name in participants}
    for i, entry in enumerate(game.question_log, start=1):
        correct = entry["correct"]
        row = [i, entry.get("concept", ""), entry["question"], "ABCD"[correct]]
        for name in participants:
            ans = entry["answers"].get(name)
            if ans is None:
                row.append("No answer")
            elif ans == correct:
                row.append("Correct")
                correct_counts[name] += 1
            else:
                row.append("Incorrect")
        ws.append(row)
        r = ws.max_row
        ws.cell(row=r, column=2).alignment = wrap
        ws.cell(row=r, column=3).alignment = wrap
        for j in range(len(participants)):
            cell = ws.cell(row=r, column=5 + j)
            cell.font = {"Correct": correct_font, "Incorrect": incorrect_font}.get(cell.value, noanswer_font)

    total_q = len(game.question_log)
    ws.append(["", "", "Total Correct Answers", f"out of {total_q}"] +
              [correct_counts[name] for name in participants])
    for cell in ws[ws.max_row]:
        cell.font = label_font
    ws.append(["", "", "Final Score", ""] + [game.players[name]["score"] for name in participants])
    for cell in ws[ws.max_row]:
        cell.font = label_font

    for width, col_letter in zip((14, 28, 52, 16), "ABCD"):
        ws.column_dimensions[col_letter].width = width
    for j in range(len(participants)):
        ws.column_dimensions[get_column_letter(5 + j)].width = 18
    ws.freeze_panes = f"E{header_row + 2}"
    return wb


@app.get("/api/results.xlsx")
async def api_download_results(_: None = Depends(require_host)):
    if not game.question_log or not game.players:
        return JSONResponse(
            {"error": "No quiz results are available yet. Results can be downloaded once a quiz has been played."},
            status_code=404,
        )
    wb = _build_results_workbook()
    buf = io.BytesIO()
    wb.save(buf)
    meta = quizzes.get(active_quiz_id, {})
    name_bits = [meta.get("subject", ""), meta.get("topic", ""), (game.started_at or "").split(",")[0]]
    safe = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", b).strip("-") for b in name_bits if str(b).strip())
    filename = f"Quiz_Results_{safe or 'quiz'}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/quiz/{quiz_id:int}/select")
async def api_select_quiz(quiz_id: int, _: None = Depends(require_host)):
    # Manage Quizzes mode has no host WebSocket, so selection must work over HTTP too.
    global QUESTIONS, active_quiz_id
    if game.phase != "waiting":
        return JSONResponse({"error": "A quiz is in progress. Wait for it to finish before switching quizzes."}, status_code=409)
    if quiz_id not in quizzes:
        return JSONResponse({"error": "Quiz not found"}, status_code=404)
    active_quiz_id = quiz_id
    QUESTIONS = quizzes[quiz_id]["questions"]
    await _to_host({"type": "quiz_selected", "id": quiz_id, "count": len(QUESTIONS), **_quiz_meta()})
    return {"ok": True, "id": quiz_id, "count": len(QUESTIONS)}


@app.delete("/api/quiz/{quiz_id:int}")
async def api_delete_quiz(quiz_id: int, _: None = Depends(require_host)):
    if quiz_id not in quizzes:
        return JSONResponse({"error": "Quiz not found"}, status_code=404)
    if quiz_id == 0:
        return JSONResponse({"error": "The built-in quiz can't be deleted"}, status_code=400)
    if quiz_id == active_quiz_id:
        return JSONResponse({"error": "Can't delete the quiz that's currently selected"}, status_code=400)
    del quizzes[quiz_id]
    # Soft delete: mark the rows in the sheet rather than removing them, so an
    # accidental delete can be recovered by clearing the flag in the sheet.
    asyncio.create_task(asyncio.to_thread(sheets_store.mark_template_deleted, quiz_id))
    return {"ok": True}


@app.get("/audio.js")
async def audio_js():
    """Shared sound engine for both pages. Everything is synthesised in the
    browser, so there are no audio assets to ship or download."""
    return FileResponse(BASE / "quiz_audio.js", media_type="application/javascript",
                        headers=_HTML_HEADERS)


@app.get("/logo.png")
async def logo():
    p = BASE / "logo.png"
    if p.exists():
        return FileResponse(p, media_type="image/png")
    return JSONResponse({"error": "logo not found"}, status_code=404)


@app.get("/qr-code.png")
async def qr_code(request: Request, u: str = Query(default="")):
    # Generate the join QR from the URL this instance is actually served at, so a
    # second deployment hands out a QR that points to itself. A single committed
    # PNG can't encode two different URLs, so the host page passes its own
    # location.origin as `u`; we fall back to the request base URL and finally to
    # a bundled static image if the QR library is unavailable.
    join_url = u.strip() or str(request.base_url)
    try:
        import segno
        buf = io.BytesIO()
        segno.make(join_url, error="m").save(buf, kind="png", scale=10, border=2,
                                             dark="#173871", light="#ffffff")
        return Response(content=buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        logger.warning("QR generation failed (%s); serving static qr-code.png", e)
        p = BASE / "qr-code.png"
        if p.exists():
            return FileResponse(p, media_type="image/png")
        return JSONResponse({"error": "QR code not found"}, status_code=404)


# ── host websocket ────────────────────────────────────────────────────────────

@app.websocket("/ws/host")
async def ws_host(websocket: WebSocket, token: str = Query(default=""), host: str = Query(default="")):
    if token != HOST_TOKEN:
        await websocket.close(code=1008, reason="Unauthorized")
        return
    global current_host_name
    await websocket.accept()
    host_sockets.add(websocket)
    host_names[websocket] = host.strip()[:40] or "A host"
    # Only claim the host-of-record name while nothing is running. A second host
    # (or a reconnect) joining mid-quiz must not overwrite the name of the person
    # who actually started the live session, or results get misattributed.
    if host.strip() and (game.phase == "waiting" or not current_host_name):
        current_host_name = host.strip()[:40]

    connected_players = list(player_ws.keys())
    connected_set = set(connected_players)
    payload = {
        "type": "connected",
        "phase": game.phase,
        "players": [
            {"name": n, "score": p["score"], "email": p.get("email", "")}
            for n, p in game.players.items()
        ],
        "connected_players": connected_players,
        "connected_count": len(connected_players),
        "global_time_limit": game.global_time_limit,
        "host": current_host_name,
        "host_count": len(host_sockets),  # >1 means another host panel is also live
        "hosts": _host_names(),           # names, so the warning can say who
        **_quiz_meta(),
    }

    if game.phase == "question" and game.current_q >= 0:
        payload["question"] = _build_question_msg()
        payload["answered"] = sum(
            1
            for n, p in game.players.items()
            if n in connected_set and p.get("answered")
        )
        payload["total_connected"] = len(connected_players)
    elif game.phase == "results" and game.last_results:
        payload["results"] = game.last_results
    elif game.phase in ("leaderboard", "finished"):
        payload["leaderboard"] = _leaderboard()
        payload.update(_progress())

    await _send(websocket, payload)
    # Let every host panel know how many hosts are now connected, so each can
    # warn when it is not the only one controlling this live session.
    await _to_host({"type": "host_presence", "host_count": len(host_sockets), "hosts": _host_names()})

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "ping":
                continue  # keepalive, no-op

            if action == "start_game" and game.phase == "waiting":
                game.phase = "countdown"
                game.countdown_task = asyncio.create_task(_do_countdown())

            elif action == "next_question" and game.phase == "leaderboard":
                game.current_q += 1
                game.answers = {}
                game.answer_times = {}
                game.last_results = None
                for p in game.players.values():
                    p["answered"] = False
                game.phase = "question"
                game.q_start_time = time.time()
                await _broadcast(_build_question_msg(), include_host=True)
                game.timer_task = asyncio.create_task(_run_timer())

            elif action == "show_leaderboard" and game.phase == "results":
                game.skip_leaderboard = True

            elif action == "skip_timer" and game.phase == "question":
                # Must NOT await: end_question() blocks until the host sends
                # "show_leaderboard", which arrives on THIS receive loop. Awaiting
                # inline would deadlock the host socket (skip timer then click
                # Show Leaderboard = frozen host).
                asyncio.create_task(end_question())

            elif action == "set_time_limit" and game.phase == "waiting":
                tl = data.get("time_limit")
                try:
                    val = int(float(tl)) if tl not in (None, "") else None
                except (TypeError, ValueError):
                    val = None
                # Ignore nonsensical values rather than letting a negative or zero
                # limit end questions instantly.
                game.global_time_limit = val if (val is None or val > 0) else None
                await _to_host({"type": "time_limit_set", "global_time_limit": game.global_time_limit})

            elif action == "select_quiz" and game.phase == "waiting":
                global QUESTIONS, active_quiz_id
                try:
                    qid = int(data.get("id"))
                except (TypeError, ValueError):
                    qid = None
                if qid in quizzes:
                    active_quiz_id = qid
                    QUESTIONS = quizzes[qid]["questions"]
                    await _to_host({"type": "quiz_selected", "id": qid, "count": len(QUESTIONS), **_quiz_meta()})

            elif action == "reset":
                # Tell every connected participant to return to the join screen
                # before we drop them, otherwise their phones sit on a stale
                # screen and their next tap hits a player the server has forgotten.
                stale_sockets = list(player_ws.values())
                game.reset()
                player_ws.clear()
                for p_ws in stale_sockets:
                    await _send(p_ws, {"type": "game_reset"})
                asyncio.create_task(asyncio.to_thread(sheets_store.clear_session_snapshot))
                await _to_host({"type": "reset"})

            elif action == "force_stop" and game.phase != "waiting":
                await _force_stop_game()

            elif action == "remove_player" and game.phase == "waiting":
                pname = data.get("name")
                if pname in game.players:
                    del game.players[pname]
                    p_ws = player_ws.pop(pname, None)
                    if p_ws:
                        await _send(p_ws, {"type": "removed_by_host"})
                    await _broadcast({"type": "player_left", "name": pname}, include_host=True)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # A malformed frame or an unexpected error must not leave the socket in
        # host_sockets, or every future broadcast keeps trying to reach a dead peer.
        logger.warning(f"Host socket loop error: {e}")
    finally:
        host_sockets.discard(websocket)
        host_names.pop(websocket, None)
        # Refresh the remaining hosts' multi-host warning after one leaves.
        await _to_host({"type": "host_presence", "host_count": len(host_sockets), "hosts": _host_names()})


# ── player websocket ──────────────────────────────────────────────────────────

@app.websocket("/ws/player")
async def ws_player(websocket: WebSocket):
    await websocket.accept()
    name: str | None = None

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "ping":
                continue  # keepalive, no-op

            if action == "join":
                n = str(data.get("name") or "").strip()[:30]
                if not n:
                    await _send(websocket, {"type": "error", "message": "Please enter your name."})
                    continue
                email = str(data.get("email") or "").strip()[:120]
                if not EMAIL_RE.match(email):
                    await _send(websocket, {"type": "error", "message": "Please enter a valid email address."})
                    continue
                device_id = str(data.get("device_id") or "").strip()[:64]

                # Returning participant. Match on email first (the real identity),
                # then device id: after a page refresh on mobile the old socket
                # often still looks connected, so a name-only check would reject
                # the participant's own rejoin with "name already taken".
                resume_as = _find_player_by_email(email)
                if resume_as is None and device_id:
                    for pn, pd in game.players.items():
                        if pd.get("device_id") == device_id:
                            resume_as = pn
                            break
                # Fallback for participants registered without either (older
                # clients, cleared browser storage): name match with no live socket.
                if resume_as is None and n in game.players and n not in player_ws:
                    resume_as = n

                if resume_as is not None:
                    # In the lobby the typed name wins, so someone can fix a typo
                    # instead of being stuck with the old name.
                    if game.phase == "waiting" and n != resume_as and n not in game.players and n not in player_ws:
                        game.players[n] = game.players.pop(resume_as)
                        moved = player_ws.pop(resume_as, None)
                        if moved is not None:
                            player_ws[n] = moved
                        await _to_host({"type": "player_left", "name": resume_as})
                        resume_as = n
                    name = resume_as
                    if device_id:
                        game.players[name]["device_id"] = device_id
                    game.players[name]["email"] = email
                    game.players[name]["answered"] = game.players[name].get("answered", False)
                    # Take over the session: the new socket wins, any lingering
                    # old one is closed with a code the client recognises.
                    stale = player_ws.get(name)
                    player_ws[name] = websocket
                    if stale is not None and stale is not websocket:
                        try:
                            await stale.close(code=4000, reason="Joined again from this device")
                        except Exception:
                            pass
                    await _send(websocket, {"type": "joined", "name": name, "email": email})
                    # Restore current game state so their screen catches up
                    await _send_current_state(websocket, name)
                    await _to_host({"type": "player_joined", "name": name, "email": email, "count": len(player_ws)})
                    continue

                # Existing active connection with that name on a different device
                if n in player_ws:
                    await _send(websocket, {"type": "error", "message": "That name is already taken. Please add a surname or initial."})
                    continue
                # Brand new participant. Late joins (after the quiz has started)
                # are allowed: they start at 0 points and drop straight into the
                # current question, simply missing the questions already gone.
                name = n
                player_ws[name] = websocket
                game.players[name] = {"score": 0, "answered": False, "email": email, "device_id": device_id}
                await _send(websocket, {"type": "joined", "name": name, "email": email})
                await _send_current_state(websocket, name)
                await _to_host({"type": "player_joined", "name": name, "email": email, "count": len(player_ws)})

            elif action == "answer" and name:
                if game.phase != "question":
                    continue
                # The participant may no longer be registered (host reset/removed
                # them while this socket was still open). Ignore the stray answer
                # rather than crashing on game.players[name].
                if name not in game.players:
                    continue
                if game.players[name].get("answered"):
                    continue
                ans = data.get("answer")
                if ans not in (0, 1, 2, 3):
                    continue
                game.players[name]["answered"] = True
                game.answers[name] = ans
                game.answer_times[name] = time.time()
                await _send(websocket, {"type": "answer_received", "answer": ans})
                # Only count connected participants for the "all answered" check
                connected = set(player_ws.keys())
                answered = sum(1 for n, p in game.players.items() if p["answered"] and n in connected)
                total = len(connected)
                await _to_host({"type": "answer_update", "answered": answered, "total": total})
                if answered >= total > 0:
                    # Not awaited: end_question() waits for the host's
                    # "show_leaderboard", and awaiting here would freeze this
                    # participant's receive loop until then.
                    asyncio.create_task(end_question())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # A malformed frame (non-JSON) or unexpected error would otherwise skip
        # cleanup, leaving a ghost socket that inflates the answered/total counts
        # and can stop a question from auto-advancing.
        logger.warning(f"Player socket loop error for {name!r}: {e}")
    finally:
        # Only clean up if this socket still owns the name: after a takeover
        # (rejoin from the same device), the closing stale socket must not
        # evict the participant's fresh connection.
        if name and player_ws.get(name) is websocket:
            player_ws.pop(name, None)
            # Always keep the participant in game.players so they can reconnect
            # with their score. The host sees "disconnected" rather than "left"
            # so the count stays accurate.
            await _to_host({"type": "player_disconnected", "name": name, "count": len(player_ws)})
            # If all currently-connected participants have answered, end the question
            if game.phase == "question":
                connected = set(player_ws.keys())
                if connected and all(game.players[n]["answered"] for n in connected if n in game.players):
                    asyncio.create_task(end_question())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
