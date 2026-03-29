import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

PORT    = int(os.environ.get("PORT", 8000))
BASE    = Path(__file__).parent            # always the quiz/ directory

app = FastAPI()

ADMIN_USER = "admin"
ADMIN_PASS = "pass@123"
HOST_TOKEN = hashlib.sha256(f"{ADMIN_USER}:{ADMIN_PASS}:qi-quiz".encode()).hexdigest()[:32]

with open(BASE / "questions.json") as f:
    _default_questions = json.load(f)["questions"]

quizzes: dict[str, list] = {"Default": _default_questions}
active_quiz_name: str = "Default"
QUESTIONS: list = _default_questions


REVEAL_DELAY = 5  # seconds before answer options are shown to players

class Game:
    def __init__(self):
        self.phase = "waiting"      # waiting | question | results | leaderboard | finished
        self.current_q = -1
        self.players: dict[str, dict] = {}   # name -> {score, answered}
        self.answers: dict[str, int] = {}    # name -> answer index (0-3)
        self.answer_times: dict[str, float] = {}  # name -> epoch time of answer
        self.q_start_time: float = 0
        self.timer_task: asyncio.Task | None = None
        self.skip_leaderboard: bool = False
        self.global_time_limit: int | None = None  # overrides per-question time_limit when set

    def reset(self):
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        self.__init__()


game = Game()
host_ws: WebSocket | None = None
player_ws: dict[str, WebSocket] = {}   # name -> websocket


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
        player_ws.pop(name, None)
        game.players.pop(name, None)
    if include_host and host_ws:
        await _send(host_ws, msg)


async def _to_host(msg: dict):
    if host_ws:
        await _send(host_ws, msg)


def _leaderboard() -> list[dict]:
    return sorted(
        [{"name": n, "score": p["score"]} for n, p in game.players.items()],
        key=lambda x: x["score"],
        reverse=True,
    )


# ── game logic ────────────────────────────────────────────────────────────────

async def end_question():
    """Called when timer expires or all players have answered."""
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

    # Pre-compute per-player elapsed times so scoring is based on when they answered,
    # not when end_question() runs. Store answer timestamps alongside answers.
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

    await _broadcast(
        {
            "type": "results",
            "correct": correct,
            "correct_text": q["options"][correct],
            "answer_counts": answer_counts,
            "points_earned": points_earned,
        },
        include_host=True,
    )

    game.skip_leaderboard = False
    for _ in range(80):           # poll every 0.1 s, up to 8 s
        if game.skip_leaderboard:
            break
        await asyncio.sleep(0.1)

    lb = _leaderboard()
    is_last = game.current_q + 1 >= len(QUESTIONS)

    if is_last:
        game.phase = "finished"
        await _broadcast({"type": "game_over", "leaderboard": lb}, include_host=True)
    else:
        game.phase = "leaderboard"
        await _broadcast({"type": "leaderboard", "leaderboard": lb}, include_host=True)


async def _do_countdown():
    for i in [3, 2, 1]:
        await _broadcast({"type": "countdown", "value": i}, include_host=True)
        await asyncio.sleep(1)
    game.phase = "question"
    game.current_q = 0
    game.answers = {}
    game.answer_times = {}
    for p in game.players.values():
        p["answered"] = False
    game.q_start_time = time.time()
    await _broadcast(_build_question_msg(), include_host=True)
    game.timer_task = asyncio.create_task(_run_timer())


def _effective_time_limit() -> int:
    """Return the time limit to use: global override if set, else per-question value."""
    q = QUESTIONS[game.current_q]
    return game.global_time_limit or q.get("time_limit", 20)


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
    }


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def player_page():
    return FileResponse(BASE / "player.html")


@app.get("/host")
async def host_page():
    return FileResponse(BASE / "host.html")


@app.post("/api/host-login")
async def api_host_login(request: Request):
    data = await request.json()
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
        return {"token": HOST_TOKEN}
    return JSONResponse({"error": "Invalid username or password"}, status_code=401)


@app.get("/api/quizzes")
async def api_list_quizzes():
    return {
        "quizzes": [{"name": n, "count": len(q)} for n, q in quizzes.items()],
        "active": active_quiz_name,
    }


@app.post("/api/quiz")
async def api_upload_quiz(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    name = str(data.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "Quiz name is required"}, status_code=400)
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return JSONResponse({"error": "'questions' must be a non-empty array"}, status_code=400)
    for i, q in enumerate(questions):
        if not isinstance(q.get("question"), str) or not str(q["question"]).strip():
            return JSONResponse({"error": f"Question {i+1}: missing 'question' text"}, status_code=400)
        if not isinstance(q.get("options"), list) or len(q["options"]) != 4:
            return JSONResponse({"error": f"Question {i+1}: must have exactly 4 options"}, status_code=400)
        if q.get("correct") not in (0, 1, 2, 3):
            return JSONResponse({"error": f"Question {i+1}: 'correct' must be 0, 1, 2, or 3"}, status_code=400)
    quizzes[name] = questions
    return {"ok": True, "name": name, "count": len(questions)}


@app.get("/logo.png")
async def logo():
    p = BASE / "logo.png"
    if p.exists():
        return FileResponse(p, media_type="image/png")
    return JSONResponse({"error": "logo not found"}, status_code=404)


# ── host websocket ────────────────────────────────────────────────────────────

@app.websocket("/ws/host")
async def ws_host(websocket: WebSocket, token: str = Query(default="")):
    if token != HOST_TOKEN:
        await websocket.close(code=1008, reason="Unauthorized")
        return
    global host_ws
    await websocket.accept()
    host_ws = websocket

    await _send(websocket, {
        "type": "connected",
        "phase": game.phase,
        "players": [{"name": n, "score": p["score"]} for n, p in game.players.items()],
        "global_time_limit": game.global_time_limit,
    })

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "start_game" and game.phase == "waiting":
                game.phase = "countdown"
                asyncio.create_task(_do_countdown())

            elif action == "next_question" and game.phase == "leaderboard":
                game.current_q += 1
                game.answers = {}
                game.answer_times = {}
                for p in game.players.values():
                    p["answered"] = False
                game.phase = "question"
                game.q_start_time = time.time()
                await _broadcast(_build_question_msg(), include_host=True)
                game.timer_task = asyncio.create_task(_run_timer())

            elif action == "show_leaderboard" and game.phase == "results":
                game.skip_leaderboard = True

            elif action == "skip_timer" and game.phase == "question":
                await end_question()

            elif action == "set_time_limit" and game.phase == "waiting":
                tl = data.get("time_limit")
                game.global_time_limit = int(tl) if tl else None
                await _send(websocket, {"type": "time_limit_set", "global_time_limit": game.global_time_limit})

            elif action == "select_quiz" and game.phase == "waiting":
                global QUESTIONS, active_quiz_name
                qname = data.get("name", "")
                if qname in quizzes:
                    active_quiz_name = qname
                    QUESTIONS = quizzes[qname]
                    await _send(websocket, {"type": "quiz_selected", "name": qname, "count": len(QUESTIONS)})

            elif action == "reset":
                game.reset()
                player_ws.clear()
                await _send(websocket, {"type": "reset"})

    except WebSocketDisconnect:
        host_ws = None


# ── player websocket ──────────────────────────────────────────────────────────

@app.websocket("/ws/player")
async def ws_player(websocket: WebSocket):
    await websocket.accept()
    name: str | None = None

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "join":
                n = data.get("name", "").strip()[:20]
                if not n:
                    await _send(websocket, {"type": "error", "message": "Please enter your name."})
                    continue
                if n in player_ws:
                    await _send(websocket, {"type": "error", "message": "That name is already taken."})
                    continue
                if game.phase != "waiting":
                    await _send(websocket, {"type": "error", "message": "Game is already in progress."})
                    continue
                name = n
                player_ws[name] = websocket
                game.players[name] = {"score": 0, "answered": False}
                await _send(websocket, {"type": "joined", "name": name})
                await _to_host({"type": "player_joined", "name": name, "count": len(player_ws)})

            elif action == "answer" and name:
                if game.phase != "question":
                    continue
                if game.players.get(name, {}).get("answered"):
                    continue
                ans = data.get("answer")
                if ans not in (0, 1, 2, 3):
                    continue
                game.players[name]["answered"] = True
                game.answers[name] = ans
                game.answer_times[name] = time.time()
                await _send(websocket, {"type": "answer_received", "answer": ans})
                answered = sum(1 for p in game.players.values() if p["answered"])
                await _to_host({"type": "answer_update", "answered": answered, "total": len(game.players)})
                if answered >= len(game.players) > 0:
                    await end_question()

    except WebSocketDisconnect:
        if name:
            player_ws.pop(name, None)
            game.players.pop(name, None)
            await _to_host({"type": "player_left", "name": name, "count": len(player_ws)})
            # If everyone remaining has now answered, end the question
            if game.phase == "question" and len(game.players) > 0:
                if all(p["answered"] for p in game.players.values()):
                    await end_question()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
