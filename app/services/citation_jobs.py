from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "addref.sqlite3"
JOB_TTL_HOURS = 24
MAX_HISTORY_ITEMS = 24
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


class CitationJobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()

    def create_job(self, *, user_id: int) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            job_id = uuid.uuid4().hex
            now = _now_iso()
            job = {
                "job_id": job_id,
                "user_id": int(user_id),
                "status": "queued",
                "progress_percent": 0,
                "stage": "queued",
                "message": "任务已创建。",
                "detail": "",
                "history": [{"time": now, "message": "任务已创建。"}],
                "result": None,
                "error": "",
                "created_at": now,
                "updated_at": now,
            }
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO citation_jobs (
                        job_id,
                        user_id,
                        status,
                        progress_percent,
                        stage,
                        message,
                        detail,
                        history_json,
                        result_json,
                        error,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["job_id"],
                        job["user_id"],
                        job["status"],
                        job["progress_percent"],
                        job["stage"],
                        job["message"],
                        job["detail"],
                        _json_dumps(job["history"]),
                        _json_dumps(job["result"]),
                        job["error"],
                        job["created_at"],
                        job["updated_at"],
                    ),
                )
            return job

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            job = self._get_job_by_id_locked(job_id)
            if job is None:
                return None

            event_message = str(fields.pop("event_message", "") or "").strip()
            for key, value in fields.items():
                job[key] = value
            job["updated_at"] = _now_iso()
            if event_message:
                history = list(job.get("history", []))
                history.append({"time": job["updated_at"], "message": event_message})
                if len(history) > MAX_HISTORY_ITEMS:
                    history = history[-MAX_HISTORY_ITEMS:]
                job["history"] = history

            with _connect() as conn:
                conn.execute(
                    """
                    UPDATE citation_jobs
                    SET status = ?,
                        progress_percent = ?,
                        stage = ?,
                        message = ?,
                        detail = ?,
                        history_json = ?,
                        result_json = ?,
                        error = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        str(job.get("status", "")),
                        int(job.get("progress_percent", 0) or 0),
                        str(job.get("stage", "")),
                        str(job.get("message", "")),
                        str(job.get("detail", "")),
                        _json_dumps(job.get("history", [])),
                        _json_dumps(job.get("result")),
                        str(job.get("error", "")),
                        str(job.get("updated_at", "")),
                        job_id,
                    ),
                )
            return job

    def complete_job(self, job_id: str, *, result: dict[str, Any]) -> dict[str, Any] | None:
        return self.update_job(
            job_id,
            status="completed",
            progress_percent=100,
            stage="completed",
            message="处理完成。",
            detail="结果已生成。",
            result=result,
            error="",
            event_message="处理完成。",
        )

    def fail_job(self, job_id: str, *, error_message: str) -> dict[str, Any] | None:
        return self.update_job(
            job_id,
            status="failed",
            stage="failed",
            message="处理失败。",
            detail=error_message,
            error=error_message,
            event_message=f"处理失败：{error_message}",
        )

    def get_job(self, *, job_id: str, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            job = self._get_job_by_id_locked(job_id)
            if job is None or int(job["user_id"]) != int(user_id):
                return None
            return job

    def get_latest_job(self, *, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            with _connect() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM citation_jobs
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (int(user_id),),
                ).fetchone()
            return _row_to_job(row) if row is not None else None

    def list_jobs(self, *, user_id: int, limit: int = 24) -> list[dict[str, Any]]:
        with self._lock:
            self._cleanup_locked()
            with _connect() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM citation_jobs
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (int(user_id), max(1, int(limit))),
                ).fetchall()
            return [_row_to_job_summary(row) for row in rows]

    def _get_job_by_id_locked(self, job_id: str) -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM citation_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def _cleanup_locked(self) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(hours=JOB_TTL_HOURS)).isoformat()
        with _connect() as conn:
            conn.execute(
                """
                DELETE FROM citation_jobs
                WHERE updated_at < ?
                """,
                (cutoff,),
            )

    def _init_db(self) -> None:
        global _INITIALIZED
        if _INITIALIZED:
            return

        with _INIT_LOCK:
            if _INITIALIZED:
                return
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _connect() as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode = WAL;

                    CREATE TABLE IF NOT EXISTS citation_jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        progress_percent INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        message TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        history_json TEXT NOT NULL,
                        result_json TEXT,
                        error TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_citation_jobs_user_updated
                    ON citation_jobs(user_id, updated_at DESC);
                    """
                )
            _INITIALIZED = True


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": str(row["job_id"]),
        "user_id": int(row["user_id"]),
        "status": str(row["status"]),
        "progress_percent": int(row["progress_percent"]),
        "stage": str(row["stage"]),
        "message": str(row["message"]),
        "detail": str(row["detail"]),
        "history": _json_loads(str(row["history_json"] or ""), []),
        "result": _json_loads(row["result_json"], None),
        "error": str(row["error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _row_to_job_summary(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_loads(row["result_json"], None)
    result_payload = result if isinstance(result, dict) else {}
    source_text = str(result_payload.get("source_text", "") or "")
    return {
        "job_id": str(row["job_id"]),
        "status": str(row["status"]),
        "progress_percent": int(row["progress_percent"]),
        "stage": str(row["stage"]),
        "message": str(row["message"]),
        "detail": str(row["detail"]),
        "error": str(row["error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "has_result": bool(result_payload),
        "placement_count": len(result_payload.get("placements", []))
        if isinstance(result_payload.get("placements"), list)
        else 0,
        "reference_count": len(result_payload.get("references", []))
        if isinstance(result_payload.get("references"), list)
        else 0,
        "source_text_preview": source_text[:160].strip(),
        "source_text_length": len(source_text),
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()
