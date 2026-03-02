import os
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)

_DB_PATH = os.environ.get("DATABASE_PATH", "jobs.db")


def _connect() -> sqlite3.Connection:
    """Create database connection.

    Returns:
        SQLite connection with row factory set
    """
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database schema.

    Creates jobs and feature_results tables if they don't exist.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
        )
        # table for per-feature results including paths, PU consumed and status
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feature_results (
                job_id TEXT NOT NULL,
                feature_idx INTEGER NOT NULL,
                pu_consumed REAL DEFAULT 0,
                paths TEXT,
                status TEXT NOT NULL,
                error TEXT,
                PRIMARY KEY (job_id, feature_idx)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def insert_job(job_id: str, status: str, payload: Dict[str, Any]) -> None:
    """Insert a new job into the database.

    Args:
        job_id: Unique job identifier
        status: Job status (pending, processing, done, failed)
        payload: Job payload dictionary
    """
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO jobs(job_id, status, created_at, payload) VALUES (?, ?, ?, ?)",
            (job_id, status, created_at, json.dumps(payload, separators=(",", ":"))),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_and_claim_pending(limit: int) -> List[Tuple[str, str]]:
    """Fetch and atomically claim pending jobs for processing.

    Uses transaction to ensure consistent state: fetches jobs with status
    'pending' and atomically updates them to 'processing' in one transaction.

    Args:
        limit: Maximum number of jobs to fetch

    Returns:
        List of (job_id, payload_json) tuples
    """
    if limit <= 0:
        return []

    conn = _connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT job_id, payload FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()

        job_ids = [r["job_id"] for r in rows]
        if job_ids:
            placeholders = ",".join(["?"] * len(job_ids))
            conn.execute(
                f"UPDATE jobs SET status = 'processing' WHERE job_id IN ({placeholders})",
                job_ids,
            )

        conn.execute("COMMIT")
        return [(r["job_id"], r["payload"]) for r in rows]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update_status(job_id: str, status: str) -> None:
    """Update job status in database.

    Args:
        job_id: Job identifier
        status: New status value
    """
    conn = _connect()
    try:
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
        conn.commit()
    finally:
        conn.close()


def insert_feature_result(
    job_id: str,
    feature_idx: int,
    pu_consumed: float,
    paths: Dict[str, Any],
    status: str,
    error: str | None = None,
) -> None:
    """Insert or update a feature result record.

    The `paths` argument is serialized to JSON and can contain nested
    dictionaries such as {"NDVI": {"tif": "path", "png": "path"}, ...}.
    """
    conn = _connect()
    try:
        conn.execute(
            "REPLACE INTO feature_results(job_id, feature_idx, pu_consumed, paths, status, error) VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                feature_idx,
                pu_consumed,
                json.dumps(paths, separators=(",", ":"), default=str),
                status,
                error,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> Dict[str, Any] | None:
    """Fetch job by job_id. Returns job details with status, created_at, and payload.

    Args:
        job_id: Job identifier

    Returns:
        Dictionary with job info or None if not found
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT job_id, status, created_at, payload FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

        if row is None:
            return None

        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload"]),
        }
    finally:
        conn.close()


def get_feature_results(job_id: str, base_url: str = "") -> List[Dict[str, Any]]:
    """Fetch all feature results for a job and convert file paths to public URLs.

    Args:
        job_id: Job identifier
        base_url: request.host_url (e.g., http://192.168.1.10:5000/)

    Returns:
        List of feature result dictionaries with public URLs
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT feature_idx, pu_consumed, paths, status, error
               FROM feature_results
               WHERE job_id = ?
               ORDER BY feature_idx ASC""",
            (job_id,),
        ).fetchall()

        results = []

        for row in rows:
            raw_paths = json.loads(row["paths"]) if row["paths"] else {}

            # Convert filesystem paths to URLs
            for metric, files in raw_paths.items():
                for ext, path in files.items():
                    if not path:
                        continue

                    # Normalize Windows paths to URL format
                    normalized = path.replace("\\", "/")

                    # Remove leading "results/" if exists
                    if normalized.startswith("results/"):
                        normalized = normalized[len("results/") :]

                    # Build public URL
                    raw_paths[metric][ext] = f"{base_url}/results/{normalized}"

            results.append(
                {
                    "feature_idx": row["feature_idx"],
                    "pu_consumed": row["pu_consumed"],
                    "paths": raw_paths,
                    "status": row["status"],
                    "error": row["error"],
                }
            )

        return results

    finally:
        conn.close()


def get_jobs(
    status: str | None = None, since: str | None = None, limit: int = 100
) -> List[Dict[str, Any]]:
    """Fetch jobs with optional status and time filters.

    Args:
        status: Optional job status to filter (exact match)
        since: Optional ISO-8601 datetime string; only return jobs created at or after this time
        limit: Maximum number of rows to return

    Returns:
        List of job dictionaries
    """
    conn = _connect()
    try:
        clauses = []
        params: List[Any] = []

        if status:
            clauses.append("status = ?")
            params.append(status)

        if since:
            # normalize since to ISO format if possible
            try:
                s = since.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                # validate
                _ = datetime.fromisoformat(s)
                params.append(s)
                clauses.append("created_at >= ?")
            except Exception:
                # invalid since value -> return empty
                return []

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT job_id, status, created_at, payload FROM jobs{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, tuple(params)).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "payload": json.loads(row["payload"]),
                }
            )
        return results
    finally:
        conn.close()


def delete_job(job_id: str) -> bool:
    """Delete job and all associated feature results from database.

    Args:
        job_id: Job identifier

    Returns:
        True if job existed and was deleted, False otherwise
    """
    conn = _connect()
    try:
        # First delete feature results
        conn.execute("DELETE FROM feature_results WHERE job_id = ?", (job_id,))

        # Then delete job
        cursor = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        conn.commit()

        return cursor.rowcount > 0
    finally:
        conn.close()
