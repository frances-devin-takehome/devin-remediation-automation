import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from devin_remediation_automation.remediation import RemediationRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS remediation_jobs (
    delivery_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    status TEXT NOT NULL,
    devin_session_id TEXT,
    devin_session_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    dispatched_at TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    pr_created_at TEXT,
    ci_run_id INTEGER,
    ci_run_url TEXT,
    ci_conclusion TEXT,
    ci_completed_at TEXT
)
"""

# Columns added after the first release; existing databases are upgraded in place.
ADDED_COLUMNS = {
    "pr_number": "INTEGER",
    "pr_url": "TEXT",
    "pr_created_at": "TEXT",
    "ci_run_id": "INTEGER",
    "ci_run_url": "TEXT",
    "ci_conclusion": "TEXT",
    "ci_completed_at": "TEXT",
}

# Deliveries recorded by the idempotency-only schema carry no issue metadata, so they are
# imported with placeholders; their status and session keep redeliveries from re-dispatching.
UNKNOWN_REPOSITORY = "unknown"
MIGRATE_LEGACY_DELIVERIES = f"""
INSERT OR IGNORE INTO remediation_jobs (
    delivery_id, repository, issue_number, issue_title, issue_url, status,
    devin_session_id, devin_session_url, created_at, updated_at, dispatched_at
)
SELECT delivery_id, '{UNKNOWN_REPOSITORY}', 0, '', '', status,
       devin_session_id, devin_session_url, created_at, updated_at,
       CASE WHEN status = 'dispatched' THEN updated_at END
FROM deliveries
"""


class RemediationStatus(str, Enum):
    """Lifecycle of a remediation job.

    `DISPATCHED` only means a Devin session was created and `PR_CREATED` only that Devin
    opened a pull request; the remediation is judged by the `Remediation validation`
    workflow, which drives `CI_RUNNING` and then `SUCCEEDED` or `FAILED`.
    """

    IN_PROGRESS = "in_progress"
    DISPATCHED = "dispatched"
    PR_CREATED = "pr_created"
    CI_RUNNING = "ci_running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Terminal states are never moved backwards by a late or duplicate workflow event.
TERMINAL_STATUSES = (RemediationStatus.SUCCEEDED, RemediationStatus.FAILED)


@dataclass(frozen=True)
class RemediationJob:
    delivery_id: str
    repository: str
    issue_number: int
    issue_title: str
    issue_url: str
    status: RemediationStatus
    attempts: int
    created_at: str
    updated_at: str
    devin_session_id: str | None = None
    devin_session_url: str | None = None
    last_error: str | None = None
    dispatched_at: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_created_at: str | None = None
    ci_run_id: int | None = None
    ci_run_url: str | None = None
    ci_conclusion: str | None = None
    ci_completed_at: str | None = None


@dataclass(frozen=True)
class RemediationSummary:
    total: int
    counts_by_status: dict[str, int]
    dispatch_attempts: int
    last_dispatched_at: str | None
    oldest_in_progress_at: str | None


@dataclass(frozen=True)
class Claim:
    """Outcome of attempting to claim a delivery for dispatch."""

    acquired: bool
    job: RemediationJob


def _job(row: sqlite3.Row) -> RemediationJob:
    return RemediationJob(
        delivery_id=row["delivery_id"],
        repository=row["repository"],
        issue_number=row["issue_number"],
        issue_title=row["issue_title"],
        issue_url=row["issue_url"],
        status=RemediationStatus(row["status"]),
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        devin_session_id=row["devin_session_id"],
        devin_session_url=row["devin_session_url"],
        last_error=row["last_error"],
        dispatched_at=row["dispatched_at"],
        pr_number=row["pr_number"],
        pr_url=row["pr_url"],
        pr_created_at=row["pr_created_at"],
        ci_run_id=row["ci_run_id"],
        ci_run_url=row["ci_run_url"],
        ci_conclusion=row["ci_conclusion"],
        ci_completed_at=row["ci_completed_at"],
    )


class RemediationStore:
    """SQLite-backed remediation jobs, keyed by the GitHub webhook delivery id.

    Claiming relies on SQLite's own atomicity: the primary key makes the first insert for a
    delivery win, and the retry transition is a conditional update, so concurrent duplicate
    deliveries within or across processes cannot both acquire the claim.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(SCHEMA)
            self._add_missing_columns(connection)
            self._migrate_legacy_deliveries(connection)

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(remediation_jobs)")
        }
        for column, column_type in ADDED_COLUMNS.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE remediation_jobs ADD COLUMN {column} {column_type}"
                )

    @staticmethod
    def _migrate_legacy_deliveries(connection: sqlite3.Connection) -> None:
        legacy_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deliveries'"
        ).fetchone()
        if legacy_exists:
            connection.execute(MIGRATE_LEGACY_DELIVERIES)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def claim(self, delivery_id: str, request: RemediationRequest) -> Claim:
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO remediation_jobs (delivery_id, repository, issue_number, "
                    "issue_title, issue_url, status) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        delivery_id,
                        request.repository_full_name,
                        request.issue_number,
                        request.issue_title,
                        request.issue_url,
                        RemediationStatus.IN_PROGRESS.value,
                    ),
                )
                return Claim(acquired=True, job=self._require(connection, delivery_id))
            except sqlite3.IntegrityError:
                pass

            # A dispatch that never reached Devin stays retryable; anything else, including a
            # job failed by its validation workflow, is already handled.
            cursor = connection.execute(
                "UPDATE remediation_jobs SET status = ?, attempts = attempts + 1, "
                "updated_at = datetime('now') WHERE delivery_id = ? AND status = ? "
                "AND devin_session_id IS NULL",
                (
                    RemediationStatus.IN_PROGRESS.value,
                    delivery_id,
                    RemediationStatus.FAILED.value,
                ),
            )
            job = self._require(connection, delivery_id)
            return Claim(acquired=cursor.rowcount == 1, job=job)

    def mark_dispatched(self, delivery_id: str, session_id: str, session_url: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE remediation_jobs SET status = ?, devin_session_id = ?, "
                "devin_session_url = ?, last_error = NULL, updated_at = datetime('now'), "
                "dispatched_at = datetime('now') WHERE delivery_id = ?",
                (RemediationStatus.DISPATCHED.value, session_id, session_url, delivery_id),
            )

    def mark_failed(self, delivery_id: str, error: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE remediation_jobs SET status = ?, last_error = ?, "
                "updated_at = datetime('now') WHERE delivery_id = ?",
                (RemediationStatus.FAILED.value, error, delivery_id),
            )

    def record_pull_request(
        self, delivery_id: str, pr_number: int, pr_url: str, pr_created_at: str | None
    ) -> RemediationJob | None:
        """Correlate an opened pull request with its job; a redelivery is a no-op.

        Returns the job (correlated or already correlated), or None when the delivery id is
        unknown. Only an existing job is ever updated, so a pull-request event cannot create one.
        """
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE remediation_jobs SET status = ?, pr_number = ?, pr_url = ?, "
                "pr_created_at = ?, updated_at = datetime('now') "
                "WHERE delivery_id = ? AND pr_number IS NULL",
                (
                    RemediationStatus.PR_CREATED.value,
                    pr_number,
                    pr_url,
                    pr_created_at,
                    delivery_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM remediation_jobs WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            return _job(row) if row is not None else None

    def record_workflow_run(
        self,
        pr_number: int,
        *,
        run_id: int,
        run_url: str,
        status: RemediationStatus,
        conclusion: str | None,
        completed_at: str | None,
    ) -> RemediationJob | None:
        """Record a validation workflow run against the job holding that pull request.

        Returns the job, or None when no remediation owns the pull request. Only an existing
        job is updated, so repeated workflow events are no-ops. The latest completed run
        decides the outcome, including a rerun that reverses an earlier one, while a job that
        already completed is never moved back to `CI_RUNNING`.
        """
        with closing(self._connect()) as connection:
            query = (
                "UPDATE remediation_jobs SET status = ?, ci_run_id = ?, ci_run_url = ?, "
                "ci_conclusion = ?, ci_completed_at = ?, updated_at = datetime('now') "
                "WHERE pr_number = ?"
            )
            parameters: list[object] = [
                status.value,
                run_id,
                run_url,
                conclusion,
                completed_at,
                pr_number,
            ]
            if status is RemediationStatus.CI_RUNNING:
                query += " AND status NOT IN (?, ?)"
                parameters += [state.value for state in TERMINAL_STATUSES]
            connection.execute(query, parameters)
            row = connection.execute(
                "SELECT * FROM remediation_jobs WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            return _job(row) if row is not None else None

    def get(self, delivery_id: str) -> RemediationJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM remediation_jobs WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            return _job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        status: RemediationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RemediationJob]:
        query = "SELECT * FROM remediation_jobs"
        parameters: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status.value)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        parameters += [limit, offset]
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [_job(row) for row in rows]

    def counts_by_status(self) -> dict[str, int]:
        counts = {status.value: 0 for status in RemediationStatus}
        with closing(self._connect()) as connection:
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM remediation_jobs GROUP BY status"
            ):
                counts[row["status"]] = row["count"]
        return counts

    def summary(self) -> RemediationSummary:
        counts = self.counts_by_status()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, SUM(attempts) AS attempts, "
                "MAX(dispatched_at) AS last_dispatched_at, "
                "MIN(CASE WHEN status = ? THEN updated_at END) AS oldest_in_progress_at "
                "FROM remediation_jobs",
                (RemediationStatus.IN_PROGRESS.value,),
            ).fetchone()
        return RemediationSummary(
            total=row["total"],
            counts_by_status=counts,
            dispatch_attempts=row["attempts"] or 0,
            last_dispatched_at=row["last_dispatched_at"],
            oldest_in_progress_at=row["oldest_in_progress_at"],
        )

    @staticmethod
    def _require(connection: sqlite3.Connection, delivery_id: str) -> RemediationJob:
        row = connection.execute(
            "SELECT * FROM remediation_jobs WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return _job(row)
