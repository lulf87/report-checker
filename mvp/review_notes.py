"""Reviewer dispositions ("处理意见") kept beside the run state database.

A disposition records what a person decided about one result: it is a
problem, it is not a problem, or the report writer has been told.  It never
changes the machine conclusion or the audit trail of the run.

Dispositions are keyed by the report number (for example ``QW2024-3944``),
the check mode and the stable part of the finding id (the id without the
``@run`` suffix).  A corrected report run again therefore shows the earlier
decisions, marked as carried over from that earlier run.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DECISIONS = ("is_problem", "not_problem", "notified", "cleared")
MAX_NOTE_LENGTH = 2000


class ReviewNoteError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def stable_finding_key(finding_id: str) -> str:
    return str(finding_id).split("@", 1)[0]


class ReviewNoteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.Lock()
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(target, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS review_notes (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       subject TEXT NOT NULL,
                       mode TEXT NOT NULL,
                       finding_key TEXT NOT NULL,
                       decision TEXT NOT NULL,
                       note TEXT NOT NULL DEFAULT '',
                       run_id TEXT NOT NULL,
                       created_at TEXT NOT NULL
                   )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS review_notes_subject ON review_notes(subject, mode, finding_key, id)"
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def add(
        self,
        *,
        subject: str,
        mode: str,
        finding_key: str,
        decision: str,
        note: str,
        run_id: str,
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ReviewNoteError("INVALID_DECISION", "处理意见只能是：是问题、不是问题、已通知修改或清除")
        if not isinstance(note, str):
            raise ReviewNoteError("INVALID_NOTE", "备注必须是文字")
        note = note.strip()
        if len(note) > MAX_NOTE_LENGTH:
            raise ReviewNoteError("INVALID_NOTE", f"备注最多 {MAX_NOTE_LENGTH} 个字")
        key = stable_finding_key(finding_key)
        if not key:
            raise ReviewNoteError("INVALID_FINDING_KEY", "缺少结果编号")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            cursor = self._connection.execute(
                """INSERT INTO review_notes(subject, mode, finding_key, decision, note, run_id, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (subject, mode, key, decision, note, run_id, created_at),
            )
            self._connection.commit()
            note_id = cursor.lastrowid
        return {
            "id": note_id,
            "finding_key": key,
            "decision": decision,
            "note": note,
            "run_id": run_id,
            "created_at": created_at,
        }

    def latest(self, *, subject: str, mode: str) -> dict[str, dict[str, Any]]:
        """The latest decision per finding for this report and mode."""

        with self._lock:
            rows = self._connection.execute(
                """SELECT n.* FROM review_notes n
                   JOIN (SELECT finding_key, MAX(id) AS id FROM review_notes
                         WHERE subject = ? AND mode = ? GROUP BY finding_key) last
                     ON last.id = n.id""",
                (subject, mode),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["decision"] == "cleared":
                continue
            result[row["finding_key"]] = {
                "finding_key": row["finding_key"],
                "decision": row["decision"],
                "note": row["note"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
            }
        return result
