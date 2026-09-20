import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    devin_session_id TEXT,
    devin_session_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class DeliveryStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    DISPATCHED = "dispatched"
    FAILED = "failed"


@dataclass(frozen=True)
class Claim:
    """Outcome of attempting to claim a delivery for dispatch."""

    acquired: bool
    status: DeliveryStatus
    devin_session_id: str | None = None
    devin_session_url: str | None = None


class DeliveryStore:
    """SQLite-backed record of which webhook deliveries have dispatched a Devin session.

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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def claim(self, delivery_id: str) -> Claim:
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO deliveries (delivery_id, status) VALUES (?, ?)",
                    (delivery_id, DeliveryStatus.IN_PROGRESS.value),
                )
                return Claim(acquired=True, status=DeliveryStatus.IN_PROGRESS)
            except sqlite3.IntegrityError:
                pass

            # A previously failed dispatch stays retryable; anything else is already handled.
            cursor = connection.execute(
                "UPDATE deliveries SET status = ?, updated_at = datetime('now') "
                "WHERE delivery_id = ? AND status = ?",
                (DeliveryStatus.IN_PROGRESS.value, delivery_id, DeliveryStatus.FAILED.value),
            )
            if cursor.rowcount == 1:
                return Claim(acquired=True, status=DeliveryStatus.IN_PROGRESS)

            row = connection.execute(
                "SELECT status, devin_session_id, devin_session_url FROM deliveries "
                "WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            return Claim(
                acquired=False,
                status=DeliveryStatus(row["status"]),
                devin_session_id=row["devin_session_id"],
                devin_session_url=row["devin_session_url"],
            )

    def mark_dispatched(self, delivery_id: str, session_id: str, session_url: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE deliveries SET status = ?, devin_session_id = ?, devin_session_url = ?, "
                "updated_at = datetime('now') WHERE delivery_id = ?",
                (DeliveryStatus.DISPATCHED.value, session_id, session_url, delivery_id),
            )

    def mark_failed(self, delivery_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE deliveries SET status = ?, updated_at = datetime('now') "
                "WHERE delivery_id = ?",
                (DeliveryStatus.FAILED.value, delivery_id),
            )
