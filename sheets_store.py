"""Google Sheets persistence: quiz library, participant results, session resume.

Needs a Google Cloud service account with the Sheets API enabled. Provide its
key via the GOOGLE_SERVICE_ACCOUNT_JSON env var (the raw JSON key content) or
GOOGLE_SERVICE_ACCOUNT_FILE (a path to the key file). The service account's
email must be added as an Editor on the target spreadsheet.

Until credentials are configured — or while the sheet is not yet shared with the
service account — every function here is a no-op that logs a warning and returns
cleanly, so the quiz still runs fine without Sheets.
"""

import json
import logging
import os
import threading

logger = logging.getLogger("sheets_store")

# One spreadsheet holds everything, in three named tabs:
#
#   Quiz Library    every uploaded quiz's questions (the persistent template store)
#   Results         one row per participant per question, across all sessions
#   session_state   a single overwritten snapshot so a restart can resume a live quiz
#
# Override with QUIZ_SHEET_ID to point a second deployment at its own spreadsheet.
_DEFAULT_SHEET_ID = "1m3jDIXVvHAFN4glTrTRHNFF1wskUHK1DvzPKBVl9y_o"

SHEET_ID = (os.environ.get("QUIZ_SHEET_ID") or _DEFAULT_SHEET_ID).strip()

LIBRARY_TAB = "Quiz Library"
RESULTS_TAB = "Results"
SESSION_TAB = "session_state"

# "Concept" is appended at the end (not next to Question) so rows written
# before the column existed stay aligned with the header.
LIBRARY_HEADER = [
    "Quiz ID", "Subject", "Topic", "Question",
    "Option A", "Option B", "Option C", "Option D",
    "Correct Answer", "Time Limit", "Uploaded By", "Uploaded At (IST)", "Deleted", "Concept",
]

# A quiz whose rows carry any of these in the "Deleted" column is treated as
# soft-deleted and hidden, but its rows are kept so it can be recovered.
_DELETED_VALUES = {"Y", "YES", "TRUE", "1", "X"}

RESULTS_HEADER = [
    "Timestamp (IST)", "Quiz ID", "Subject", "Topic", "Host",
    "Question #", "Question", "Participant Name", "Email",
    "Selected Answer", "Correct Answer", "Result", "Points Earned",
]

_lock = threading.Lock()
_client = None
_client_error = None


def _load_credentials_info():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        return json.loads(raw)
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _get_client():
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    with _lock:
        if _client is not None or _client_error is not None:
            return _client
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            info = _load_credentials_info()
            if not info:
                _client_error = "not configured"
                logger.warning(
                    "Google Sheets not configured: set GOOGLE_SERVICE_ACCOUNT_JSON "
                    "(or GOOGLE_SERVICE_ACCOUNT_FILE) to enable the quiz library "
                    "and result logging."
                )
                return None
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            _client = gspread.authorize(creds)
            logger.info("Google Sheets connected as %s", info.get("client_email", "?"))
        except Exception as e:
            _client_error = str(e)
            logger.warning(f"Google Sheets client init failed: {e}")
            return None
    return _client


def is_configured() -> bool:
    return _get_client() is not None


def _ensure_header(ws, header):
    values = ws.row_values(1)
    if values != header:
        # gspread >= 6.0 signature: update(values, range_name=...).
        ws.update([header], "A1")


def _open_tab(title: str, header: list, rows: int = 200):
    """Open (creating if missing) one named tab of the quiz spreadsheet.

    A 403 here almost always means the spreadsheet has not been shared with the
    service account; it is logged once by the caller and treated as "no Sheets",
    never as a fatal error.
    """
    client = _get_client()
    if not client:
        return None
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=rows, cols=max(len(header), 2))
    _ensure_header(ws, header)
    return ws


def _library_row_for_header(header: list, values_by_col: dict) -> list:
    """Lay a quiz row out to match the live sheet's actual header order, so every
    field (Concept especially, being last) lands under its named column even if
    the sheet's columns were reordered or an extra column was inserted. Falls
    back to the canonical order for any header cell we don't recognise."""
    row = []
    for title in header:
        key = str(title).strip().lower()
        if key.startswith("concept"):
            row.append(values_by_col.get("concept", ""))
        elif key.startswith("time limit"):
            row.append(values_by_col.get("time limit", ""))
        else:
            row.append(values_by_col.get(key, ""))
    return row


def append_template(quiz_id: int, subject: str, topic: str, questions: list,
                    uploaded_by: str, uploaded_at: str) -> bool:
    """Persist one uploaded quiz's questions as rows in the Quiz Library tab."""
    try:
        ws = _open_tab(LIBRARY_TAB, LIBRARY_HEADER, rows=2000)
        if not ws:
            return False
        # Write by column name against the live header, not by fixed position, so
        # Concept can never slide into the wrong column if the sheet has drifted.
        header = ws.row_values(1) or LIBRARY_HEADER
        rows = []
        for q in questions:
            opts = q["options"]
            values_by_col = {
                "quiz id": quiz_id, "subject": subject, "topic": topic,
                "question": q["question"],
                "option a": opts[0], "option b": opts[1],
                "option c": opts[2], "option d": opts[3],
                "correct answer": "ABCD"[q["correct"]],
                "time limit": q.get("time_limit", ""),
                "uploaded by": uploaded_by, "uploaded at (ist)": uploaded_at,
                "deleted": "",
                "concept": q.get("concept", ""),
            }
            rows.append(_library_row_for_header(header, values_by_col))
        ws.append_rows(rows, value_input_option="RAW")
        return True
    except Exception as e:
        logger.warning(f"Failed to persist quiz {quiz_id}: {e}")
        return False


def load_templates() -> dict:
    """Rebuild {quiz_id: {subject, topic, questions, uploaded_at, uploaded_by}} from the library tab."""
    try:
        ws = _open_tab(LIBRARY_TAB, LIBRARY_HEADER, rows=2000)
        if not ws:
            return {}
        records = ws.get_all_records()
        out = {}
        for r in records:
            try:
                quiz_id = int(r.get("Quiz ID") or 0)
            except (ValueError, TypeError):
                continue
            if quiz_id <= 0:
                continue
            # Skip soft-deleted rows. Every row of a deleted quiz is flagged, so
            # a fully-deleted quiz never gets an entry created.
            if str(r.get("Deleted", "")).strip().upper() in _DELETED_VALUES:
                continue
            entry = out.setdefault(quiz_id, {
                "subject": r.get("Subject", ""),
                "topic": r.get("Topic", ""),
                "uploaded_by": r.get("Uploaded By", ""),
                "uploaded_at": r.get("Uploaded At (IST)", ""),
                "questions": [],
            })
            try:
                correct = "ABCD".index(str(r.get("Correct Answer", "A")).strip().upper())
            except ValueError:
                correct = 0
            q = {
                "question": r.get("Question", ""),
                "options": [
                    r.get("Option A", ""), r.get("Option B", ""),
                    r.get("Option C", ""), r.get("Option D", ""),
                ],
                "correct": correct,
            }
            tl = r.get("Time Limit")
            if tl not in (None, ""):
                try:
                    q["time_limit"] = int(tl)
                except (ValueError, TypeError):
                    pass
            concept = str(r.get("Concept", "") or "").strip()
            if concept:
                q["concept"] = concept
            entry["questions"].append(q)
        return out
    except Exception as e:
        logger.warning(f"Failed to load the quiz library: {e}")
        return {}


def _col_letter(n: int) -> str:
    """1-based column number to A1 letter: 1->A, 27->AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def mark_template_deleted(quiz_id: int) -> bool:
    """Soft delete: set the "Deleted" flag on every row belonging to quiz_id
    instead of removing them, so the quiz is hidden but recoverable by clearing
    the flag in the sheet. Only touches this quiz's rows in one column, so it can
    never corrupt other quizzes' data."""
    try:
        ws = _open_tab(LIBRARY_TAB, LIBRARY_HEADER, rows=2000)
        if not ws:
            return False
        values = ws.get_all_values()
        if len(values) <= 1:
            return True
        header = values[0]
        try:
            id_col = header.index("Quiz ID")
        except ValueError:
            id_col = 0
        try:
            del_col = header.index("Deleted")
        except ValueError:
            # _ensure_header adds the column; if it's somehow missing, don't guess.
            logger.warning("Quiz Library tab has no 'Deleted' column; skipping soft delete.")
            return False
        col = _col_letter(del_col + 1)
        # body row i (0-based) is sheet row i + 2 (1 header row, 1-based rows).
        updates = [
            {"range": f"{col}{i + 2}", "values": [["Y"]]}
            for i, row in enumerate(values[1:])
            if len(row) > id_col and str(row[id_col]) == str(quiz_id)
        ]
        if not updates:
            return True
        ws.batch_update(updates, value_input_option="RAW")
        return True
    except Exception as e:
        logger.warning(f"Failed to soft-delete quiz {quiz_id}: {e}")
        return False


def log_results(rows: list) -> bool:
    """Append result rows to the Results tab. Each row matches RESULTS_HEADER."""
    if not rows:
        return False
    try:
        ws = _open_tab(RESULTS_TAB, RESULTS_HEADER, rows=5000)
        if not ws:
            return False
        ws.append_rows(rows, value_input_option="RAW")
        return True
    except Exception as e:
        logger.warning(f"Failed to log results: {e}")
        return False


# ── live session resume ───────────────────────────────────────────────────────
# A single overwritten snapshot (not an append-only log) of the in-progress
# game, so a restart can resume at the right question instead of from scratch.

_SESSION_HEADER = ["Updated At", "Snapshot JSON"]


def _open_session_worksheet():
    return _open_tab(SESSION_TAB, _SESSION_HEADER, rows=10)


def save_session_snapshot(snapshot: dict) -> bool:
    try:
        ws = _open_session_worksheet()
        if not ws:
            return False
        payload = json.dumps(snapshot)
        if len(payload) > 45000:
            # A single Google Sheets cell holds at most 50,000 chars. Skip the
            # write rather than raising, so a very large game keeps running (it
            # just won't be resumable) instead of erroring on every question.
            logger.warning(
                "Session snapshot too large (%d chars); skipping resume save.", len(payload)
            )
            return False
        ws.update([[snapshot.get("saved_at", ""), payload]], "A2")
        return True
    except Exception as e:
        logger.warning(f"Failed to save session snapshot: {e}")
        return False


def load_session_snapshot() -> dict | None:
    try:
        ws = _open_session_worksheet()
        if not ws:
            return None
        raw = ws.acell("B2").value
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Failed to load session snapshot: {e}")
        return None


def clear_session_snapshot() -> bool:
    try:
        ws = _open_session_worksheet()
        if not ws:
            return False
        ws.update([["", ""]], "A2")
        return True
    except Exception as e:
        logger.warning(f"Failed to clear session snapshot: {e}")
        return False
