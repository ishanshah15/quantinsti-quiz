import asyncio
import json
import os
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

PORT = int(os.environ.get("PORT", 8000))   # Railway injects PORT automatically

app = FastAPI()

with open("questions.json") as f:
    QUESTIONS = json.load(f)["questions"]


class Game:
    def __init__(self):
        self.phase = "waiting"      # waiting | question | results | leaderboard | finished
        self.current_q = -1
        self.players: dict[str, dict] = {}   # name -> {score, answered}
        self.answers: dict[str, int] = {}    # name -> answer index (0-3)
        self.q_start_time: float = 0
        self.timer_task: asyncio.Task | None = None
        self.skip_leaderboard: bool = False

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
    time_limit = q.get("time_limit", 20)
    answer_counts = [0, 0, 0, 0]

    for name, ans in game.answers.items():
        if 0 <= ans <= 3:
            answer_counts[ans] += 1
        if ans == correct and name in game.players:
            elapsed = time.time() - game.q_start_time
            speed_bonus = int(1000 * max(0.0, 1 - elapsed / time_limit))
            game.players[name]["score"] += 1000 + speed_bonus

    await _broadcast(
        {
            "type": "results",
            "correct": correct,
            "correct_text": q["options"][correct],
            "answer_counts": answer_counts,
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
    for p in game.players.values():
        p["answered"] = False
    game.q_start_time = time.time()
    await _broadcast(_build_question_msg(), include_host=True)
    game.timer_task = asyncio.create_task(_run_timer())


async def _run_timer():
    q = QUESTIONS[game.current_q]
    await asyncio.sleep(q.get("time_limit", 20))
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
        "time_limit": q.get("time_limit", 20),
    }


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def player_page():
    return FileResponse("player.html")


@app.get("/host")
async def host_page():
    return FileResponse("host.html")


@app.get("/logo.png")
async def logo():
    p = Path("logo.png")
    if p.exists():
        return FileResponse("logo.png", media_type="image/png")
    return JSONResponse({"error": "logo not found"}, status_code=404)


# ── host websocket ────────────────────────────────────────────────────────────

@app.websocket("/ws/host")
async def ws_host(websocket: WebSocket):
    global host_ws
    await websocket.accept()
    host_ws = websocket

    await _send(websocket, {
        "type": "connected",
        "phase": game.phase,
        "players": [{"name": n, "score": p["score"]} for n, p in game.players.items()],
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
                await _send(websocket, {"type": "answer_received", "answer": ans})
                answered = sum(1 for p in game.players.values() if p["answered"])
                await _to_host({"type": "answer_update", "answered": answered, "total": len(game.players)})
                if answered >= len(game.players) > 0:
                    await end_question()

    except WebSocketDisconnect:
        if name:
            player_ws.pop(name, None)
            await _to_host({"type": "player_left", "name": name, "count": len(player_ws)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
