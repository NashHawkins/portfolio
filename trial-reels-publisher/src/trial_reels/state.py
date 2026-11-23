"""SQLite job state for idempotency and crash recovery, plus a single-process lock."""
from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from .errors import LockError, StateError, redact

STATES = ("DISCOVERED", "VALIDATED", "CONTAINER_CREATED", "UPLOADING", "UPLOADED", "READY",
          "PUBLISHING", "PUBLISHED", "FAILED", "UNKNOWN")
# Labels only. Whether a job blocks is decided by UNCERTAIN_SQL / is_uncertain(), which also look
# at publish history (a FAILED job whose media_publish was sent may still be live).
BLOCKING_STATES = ("PUBLISHING", "PUBLISHED", "UNKNOWN")
# 1: base, 2: upload_* + publish_mode, 3: account_profile/account_username,
# 4: failure_phase + api_calls diagnostics + repair of FAILED-after-publish-attempt rows
# 5: upload_method + video_url (URL-based publishing); UNIQUE gains upload_method (table rebuild)
# 6: overrides table (one-time, user-confirmed exception to a single UNKNOWN blocker; jobs unchanged)
# 7: diagnostic_overrides + diagnostic_override_blockers (one-time exception to SEVERAL UNKNOWN blockers
#    for one approved video; only the account pause is relaxed, never duplicate protection)
# 8: jobs.attempt (UNIQUE gains attempt; table rebuild; existing rows are attempt 1) and
#    diagnostic_overrides.allowed_duplicate_job_key (a user-approved re-attempt of ONE UNKNOWN job's video)
SCHEMA_VERSION = 8

# diagnostic_override_blockers.blocker_container_status value for a PUBLISHED job of the same video that the
# user explicitly accepted posting over. Only rows carrying it are excused by the duplicate check.
ACCEPTED_PUBLISHED_DUPLICATE = "ACCEPTED_PUBLISHED_DUPLICATE"
# diagnostic_overrides.kind for a --test-mode attempt is TEST_MODE_PREFIX + its new job_key (one row per test).
TEST_MODE_PREFIX = "TEST_MODE:"

# Why a job is FAILED. Only these prove nothing was published.
FAILURE_PHASES = ("PRE_PUBLISH",               # failed before media_publish was ever sent
                  "PUBLISH_REJECTED",          # media_publish got a documented pre-publish rejection
                  "RECONCILED_NOT_PUBLISHED")  # Meta later proved it unpublished (EXPIRED / ERROR)
_PHASES_SQL = ", ".join(f"'{p}'" for p in FAILURE_PHASES)


def uncertain_sql(alias: str = "") -> str:
    """SQL predicate: this job's media_publish may have gone through and is not resolved."""
    a = f"{alias}." if alias else ""
    return (f"({a}state IN ('PUBLISHING', 'UNKNOWN') OR ({a}state = 'FAILED' AND {a}publish_attempts > 0 "
            f"AND ({a}failure_phase IS NULL OR {a}failure_phase NOT IN ({_PHASES_SQL}))))")


def blocking_sql(alias: str = "") -> str:
    """SQL predicate: this job's video is, or may be, live."""
    a = f"{alias}." if alias else ""
    return f"({a}state = 'PUBLISHED' OR {uncertain_sql(alias)})"


def is_uncertain(rec: "JobRecord") -> bool:
    if rec.state in ("PUBLISHING", "UNKNOWN"):
        return True
    return (rec.state == "FAILED" and (rec.publish_attempts or 0) > 0
            and rec.failure_phase not in FAILURE_PHASES)


def is_blocking(rec: "JobRecord") -> bool:
    return rec.state == "PUBLISHED" or is_uncertain(rec)


# SQL predicate: a job whose account_id is missing or not a numeric IG user ID. We cannot tell
# which account such a job was published to, so it blocks its video on EVERY account.
UNKNOWN_ACCOUNT_SQL = ("({col} IS NULL OR trim({col}) = '' OR {col} GLOB '*[^0-9]*')")

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    publish_mode TEXT NOT NULL DEFAULT 'TRIAL' CHECK (publish_mode IN ('TRIAL', 'PUBLIC')),
    graduation_strategy TEXT,
    scheduled_for TEXT,
    state TEXT NOT NULL,
    container_id TEXT,
    upload_uri TEXT,
    media_id TEXT,
    permalink TEXT,
    last_error TEXT,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    publish_started_at TEXT,
    upload_sha256 TEXT,
    upload_size_bytes INTEGER,
    upload_remuxed INTEGER NOT NULL DEFAULT 0,
    account_profile TEXT,
    account_username TEXT,
    failure_phase TEXT,
    upload_method TEXT NOT NULL DEFAULT 'resumable' CHECK (upload_method IN ('resumable', 'url')),
    video_url TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, content_hash, source_path, publish_mode, upload_method, attempt)
);
"""

SCHEMA = JOBS_DDL.format(name="jobs") + """
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    at TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT
);
"""

API_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    step TEXT NOT NULL,              -- create_container | upload | poll | publish | ...
    phase TEXT NOT NULL,             -- PRE_PUBLISH | PUBLISH
    outcome TEXT NOT NULL,           -- OK | FAILED | REJECTED | UNCERTAIN
    classification TEXT,             -- why the outcome was judged so
    container_id TEXT,
    attempted_at TEXT NOT NULL,
    container_status_before TEXT,
    quota_usage_before INTEGER,
    quota_total_before INTEGER,
    http_status INTEGER,
    error_code INTEGER,
    error_subcode INTEGER,
    error_type TEXT,
    error_message TEXT,
    is_transient INTEGER,
    fbtrace_id TEXT,
    media_id TEXT
);
CREATE INDEX IF NOT EXISTS api_calls_job ON api_calls(job_key);
"""

# One row per user-confirmed exception. UNIQUE(blocker_job_id): a blocker can be overridden at most
# once, ever. The blocker job row is never modified; it stays UNKNOWN and keeps blocking everything
# except the single job named in new_job_key, and only until that job's media_publish is claimed.
OVERRIDES_DDL = """
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    blocker_job_id INTEGER NOT NULL UNIQUE,
    blocker_job_key TEXT NOT NULL,
    blocker_container_id TEXT NOT NULL,
    blocker_container_status TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_profile TEXT NOT NULL,
    account_username TEXT,
    content_hash TEXT NOT NULL,
    upload_method TEXT NOT NULL,
    video_url TEXT NOT NULL,
    new_job_key TEXT NOT NULL UNIQUE,
    confirmation TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    publish_claimed_at TEXT
);
"""

# UNIQUE(kind): each approved diagnostic can be used once, ever. The blockers it tolerates are listed in
# the child table; those job rows are never modified.
DIAGNOSTIC_DDL = """
CREATE TABLE IF NOT EXISTS diagnostic_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    account_profile TEXT NOT NULL,
    account_username TEXT,
    content_hash TEXT NOT NULL,
    publish_mode TEXT NOT NULL,
    upload_method TEXT NOT NULL,
    new_job_key TEXT NOT NULL UNIQUE,
    confirmation TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    publish_claimed_at TEXT,
    allowed_duplicate_job_key TEXT
);
CREATE TABLE IF NOT EXISTS diagnostic_override_blockers (
    override_id INTEGER NOT NULL REFERENCES diagnostic_overrides(id),
    blocker_job_id INTEGER NOT NULL,
    blocker_job_key TEXT NOT NULL,
    blocker_container_id TEXT,
    blocker_container_status TEXT,
    PRIMARY KEY (override_id, blocker_job_id)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobRecord:
    job_key: str
    account_id: str
    source_path: str
    relative_path: str
    content_hash: str
    size_bytes: int
    publish_mode: str                 # TRIAL | PUBLIC
    graduation_strategy: Optional[str]  # None for PUBLIC
    scheduled_for: Optional[str]
    state: str
    container_id: Optional[str]
    upload_uri: Optional[str]
    media_id: Optional[str]
    permalink: Optional[str]
    last_error: Optional[str]
    publish_attempts: int
    publish_started_at: Optional[str]
    upload_sha256: Optional[str]      # bytes actually sent (differs from content_hash if remuxed)
    upload_size_bytes: Optional[int]
    upload_remuxed: int               # 1 if a temporary fast-start copy was uploaded
    account_profile: Optional[str]    # "test" / "personal" / None (default .env or pre-profile record)
    account_username: Optional[str]   # @username Meta returned when the job was verified
    created_at: str
    updated_at: str
    failure_phase: Optional[str] = None  # set on every FAILED written since schema v4
    id: Optional[int] = None
    upload_method: str = "resumable"     # resumable | url (schema v5; older rows are resumable)
    video_url: Optional[str] = None      # url method: the public URL Meta was told to fetch
    attempt: int = 1                     # >1 only for a user-approved re-attempt of the same video (v8)


_COLUMNS = [f for f in JobRecord.__dataclass_fields__]


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit mode: every statement is committed immediately (isolation_level=None)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self.backup_path: Optional[Path] = None
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        """Bring an older state DB up to date without dropping any rows or events.

        A timestamped backup copy is written next to the DB before the first schema change.
        Every step is idempotent; the v4 data repair runs inside one transaction.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        add = [(c, d) for c, d in (("upload_sha256", "TEXT"), ("upload_size_bytes", "INTEGER"),
                                   ("upload_remuxed", "INTEGER NOT NULL DEFAULT 0"),
                                   ("account_profile", "TEXT"), ("account_username", "TEXT"),
                                   ("failure_phase", "TEXT"))
               if c not in have]
        needs_rebuild = "publish_mode" not in have or "upload_method" not in have or "attempt" not in have
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.migration_report: List[str] = []
        if (add or needs_rebuild or version < SCHEMA_VERSION) and self._has_rows():
            self.backup_path = self._backup()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for col, ddl in add:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
            for stmt in (API_CALLS_DDL + ";" + OVERRIDES_DDL + ";" + DIAGNOSTIC_DDL).strip().split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            diag = {r["name"] for r in self.conn.execute("PRAGMA table_info(diagnostic_overrides)")}
            if "allowed_duplicate_job_key" not in diag:
                self.conn.execute("ALTER TABLE diagnostic_overrides ADD COLUMN allowed_duplicate_job_key TEXT")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if needs_rebuild:
            self._rebuild_jobs_table()
        if version < 4:
            self.migration_report = self.repair_legacy_failed()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_content ON jobs(account_id, content_hash)")

    def repair_legacy_failed(self) -> List[str]:
        """v4 data repair. Idempotent and transactional; never deletes rows or events.

        * FAILED, no failure_phase, publish_attempts == 0  -> stays FAILED; annotated PRE_PUBLISH
          (media_publish was never sent, so nothing can have been published).
        * FAILED, no failure_phase, publish_attempts  > 0  -> UNKNOWN (media_publish was sent and the
          old code marked any 4xx reply FAILED; the reply is not proof of rejection).
        Timestamps (created_at, updated_at, publish_started_at) are preserved; an event explains it.
        """
        report = []
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, job_key, account_id, container_id, publish_attempts, publish_started_at, "
                "last_error FROM jobs WHERE state = 'FAILED' AND failure_phase IS NULL ORDER BY id").fetchall()
            for r in rows:
                if (r["publish_attempts"] or 0) == 0:
                    cur = self.conn.execute(
                        "UPDATE jobs SET failure_phase = 'PRE_PUBLISH' WHERE id = ? AND state = 'FAILED' "
                        "AND failure_phase IS NULL AND publish_attempts = 0", (r["id"],))
                    if cur.rowcount == 1:
                        self._event(r["job_key"], "FAILED", "schema v4 migration: confirmed pre-publication "
                                    "failure (media_publish was never sent); state unchanged", at=now)
                        report.append(f"job #{r['id']}: FAILED kept (pre-publication failure)")
                else:
                    cur = self.conn.execute(
                        "UPDATE jobs SET state = 'UNKNOWN' WHERE id = ? AND state = 'FAILED' "
                        "AND failure_phase IS NULL AND publish_attempts > 0", (r["id"],))
                    if cur.rowcount == 1:
                        first_line = (r["last_error"] or "").splitlines()[0][:300] if r["last_error"] else "none"
                        self._event(r["job_key"], "UNKNOWN",
                                    f"schema v4 migration: reclassified FAILED -> UNKNOWN. media_publish was "
                                    f"sent (publish_attempts={r['publish_attempts']}, publish_started_at="
                                    f"{r['publish_started_at']}, container {r['container_id']}) and the "
                                    f"recorded reply does not prove Meta rejected it: {first_line}", at=now)
                        report.append(f"job #{r['id']}: FAILED -> UNKNOWN (container {r['container_id']}, "
                                      f"account {r['account_id']})")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return report

    def _rebuild_jobs_table(self) -> None:
        """The UNIQUE constraint gained publish_mode (v2) and upload_method (v5); SQLite can only
        change it by rebuilding the table. Every row is copied with its id and all values; columns
        an old DB lacks take their defaults (publish_mode 'TRIAL', upload_method 'resumable'), which
        is what those rows were. Atomic: on any error nothing changes."""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")]  # 'attempt' defaults to 1
        col_list = ", ".join(cols)
        before = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DROP TABLE IF EXISTS jobs_new")
            self.conn.execute(JOBS_DDL.format(name="jobs_new"))
            self.conn.execute(f"INSERT INTO jobs_new ({col_list}) SELECT {col_list} FROM jobs")
            after = self.conn.execute("SELECT COUNT(*) FROM jobs_new").fetchone()[0]
            if after != before:
                raise StateError(f"table rebuild copied {after} of {before} rows; aborted")
            self.conn.execute("DROP TABLE jobs")
            self.conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
            self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _has_rows(self) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None

    def _backup(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        n = 1
        while dest.exists():
            dest = self.path.with_name(f"{self.path.name}.bak-{stamp}-{n}")
            n += 1
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # self-contained single file, no -wal/-shm
        finally:
            target.close()
        for side in ("-wal", "-shm"):  # leftovers from the brief WAL phase; the .bak file is complete
            leftover = dest.with_name(dest.name + side)
            if leftover.exists():
                leftover.unlink()
        os.chmod(dest, 0o600)
        return dest

    def close(self) -> None:
        self.conn.close()

    def get(self, job_key: str) -> Optional[JobRecord]:
        row = self.conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE job_key = ?",
                                (job_key,)).fetchone()
        return JobRecord(**dict(row)) if row else None

    def all(self) -> List[JobRecord]:
        rows = self.conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs ORDER BY id").fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def account_blockers(self, account_id: str) -> List[JobRecord]:
        """Unresolved publication attempts that pause ALL publishing to this account (any video),
        plus unresolved jobs whose account is unknown (they might belong to this account)."""
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="account_id")
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE (account_id = ? OR {unknown}) "
            f"AND {uncertain_sql()} ORDER BY id", (account_id,)).fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def uncertain_jobs(self) -> List[JobRecord]:
        rows = self.conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE {uncertain_sql()} "
                                 "ORDER BY id").fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def log_api_call(self, *, job_key: str, account_id: str, content_hash: str, step: str, phase: str,
                     outcome: str, err=None, classification: Optional[str] = None,
                     container_id: Optional[str] = None, container_status_before: Optional[str] = None,
                     quota_usage_before: Optional[int] = None, quota_total_before: Optional[int] = None,
                     media_id: Optional[str] = None, attempted_at: Optional[str] = None) -> None:
        """Structured diagnostics for one API step. Messages are redacted; tokens never stored."""
        g = (lambda name: getattr(err, name, None)) if err is not None else (lambda name: None)
        msg = g("meta_message") or (str(err) if err is not None else None)
        transient = g("is_transient")
        self.conn.execute(
            "INSERT INTO api_calls (job_key, account_id, content_hash, step, phase, outcome, classification, "
            "container_id, attempted_at, container_status_before, quota_usage_before, quota_total_before, "
            "http_status, error_code, error_subcode, error_type, error_message, is_transient, fbtrace_id, "
            "media_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_key, account_id, content_hash, step, phase, outcome, classification, container_id,
             attempted_at or utcnow(), container_status_before, quota_usage_before, quota_total_before,
             g("http_status"), g("code"), g("subcode"), g("error_type"),
             redact(msg)[:2000] if msg else None, None if transient is None else int(bool(transient)),
             g("fbtrace_id"), media_id))

    def api_calls(self, job_key: str) -> List[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM api_calls WHERE job_key = ? ORDER BY id", (job_key,)).fetchall()]

    def find_blocking(self, account_id: str, content_hash: str) -> List[JobRecord]:
        """Jobs that are, or may be, live with the same video bytes on THIS account (any mode,
        any path), plus jobs whose account is unknown (conservatively: could be this account).

        Duplicate scope is (instagram_user_id, source_file_sha256): the same video may be posted
        once per account, never twice to one account.
        """
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="account_id")
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE content_hash = ? "
            f"AND (account_id = ? OR {unknown}) AND {blocking_sql()} ORDER BY id",
            (content_hash, account_id)).fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def unknown_account_jobs(self) -> List[JobRecord]:
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="account_id")
        rows = self.conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE {unknown} ORDER BY id").fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def insert(self, *, job_key: str, account_id: str, source_path: str, relative_path: str,
               content_hash: str, size_bytes: int, graduation_strategy: Optional[str],
               publish_mode: str = "TRIAL", scheduled_for: Optional[str] = None,
               state: str = "VALIDATED", account_profile: Optional[str] = None,
               account_username: Optional[str] = None, upload_method: str = "resumable",
               video_url: Optional[str] = None, attempt: int = 1) -> JobRecord:
        now = utcnow()
        try:
            self.conn.execute(
                "INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, "
                "size_bytes, publish_mode, graduation_strategy, scheduled_for, state, account_profile, "
                "account_username, upload_method, video_url, attempt, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_key, account_id, source_path, relative_path, content_hash, size_bytes,
                 publish_mode, graduation_strategy, scheduled_for, state, account_profile,
                 account_username, upload_method, video_url, attempt, now, now))
        except sqlite3.IntegrityError:
            raise StateError("A job for this account and file already exists.")
        self._event(job_key, state, "created")
        return self.get(job_key)  # type: ignore[return-value]

    def update(self, job_key: str, state: str, *, detail: Optional[str] = None,
               expect_state: Optional[str] = None, **fields) -> JobRecord:
        if state not in STATES:
            raise ValueError(state)
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown columns {unknown}")
        if "id" in fields:
            raise ValueError("job id is immutable")
        if state == "FAILED" and fields.get("failure_phase") not in FAILURE_PHASES:
            # A FAILED label must always say why nothing can have been published.
            raise ValueError(f"FAILED requires failure_phase in {FAILURE_PHASES}")
        if state != "FAILED" and "failure_phase" not in fields:
            fields["failure_phase"] = None
        if "last_error" in fields and fields["last_error"]:
            fields["last_error"] = redact(fields["last_error"])[:2000]
        sets = ["state = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
        args = [state, utcnow()] + list(fields.values())
        sql = f"UPDATE jobs SET {', '.join(sets)} WHERE job_key = ?"
        args.append(job_key)
        if expect_state is not None:  # compare-and-set guards concurrent transitions
            sql += " AND state = ?"
            args.append(expect_state)
        cur = self.conn.execute(sql, args)
        if cur.rowcount != 1:
            raise StateError(f"State transition to {state} rejected (job changed concurrently?).")
        self._event(job_key, state, detail)
        return self.get(job_key)  # type: ignore[return-value]

    def begin_publish(self, job_key: str) -> JobRecord:
        """Atomically READY -> PUBLISHING and bump the attempt counter. Committed before the API call.

        The same single statement re-checks, atomically:
          * duplicate scope: no other job with the same (account, sha256) is or may be live;
          * account block: no other job on this account has an unresolved publication attempt.
        """
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="o.account_id")
        cur = self.conn.execute(
            "UPDATE jobs SET state='PUBLISHING', publish_attempts = publish_attempts + 1, "
            "publish_started_at = ?, updated_at = ?, failure_phase = NULL "
            "WHERE job_key = ? AND state = 'READY' "
            f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key "
            f"AND o.content_hash = jobs.content_hash AND (o.account_id = jobs.account_id OR {unknown}) "
            f"AND {blocking_sql('o')}) "
            f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key "
            f"AND (o.account_id = jobs.account_id OR {unknown}) AND {uncertain_sql('o')})",
            (utcnow(), utcnow(), job_key))
        if cur.rowcount != 1:
            raise StateError("Refusing to publish: job is not READY, this video is already (or may be) "
                             "published to this account, or the account has an unresolved publication "
                             "attempt.")
        self._event(job_key, "PUBLISHING", "media_publish about to be sent")
        return self.get(job_key)  # type: ignore[return-value]

    def begin_retry_publish(self, job_key: str, container_id: str, max_attempts_before: int) -> JobRecord:
        """Atomically UNKNOWN -> PUBLISHING for a user-approved re-send of media_publish on the SAME
        container. Committed before the request leaves.

        The single statement requires: the job is still UNKNOWN, still bound to ``container_id``, has not
        been retried yet (publish_attempts <= max_attempts_before), and no OTHER job on this account (or
        with an unknown account) is unresolved or holds the same video. publish_started_at is left at the
        ORIGINAL attempt time, so evidence windows still cover the first attempt.
        """
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="o.account_id")
        now = utcnow()
        cur = self.conn.execute(
            "UPDATE jobs SET state='PUBLISHING', publish_attempts = publish_attempts + 1, updated_at = ?, "
            "failure_phase = NULL "
            "WHERE job_key = ? AND state = 'UNKNOWN' AND container_id = ? AND publish_attempts <= ? "
            f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key "
            f"AND o.content_hash = jobs.content_hash AND (o.account_id = jobs.account_id OR {unknown}) "
            f"AND {blocking_sql('o')}) "
            f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key "
            f"AND (o.account_id = jobs.account_id OR {unknown}) AND {uncertain_sql('o')})",
            (now, job_key, container_id, max_attempts_before))
        if cur.rowcount != 1:
            raise StateError("Refusing to retry: the job is no longer UNKNOWN on that container, it was "
                             "already retried, or another job on this account is unresolved or holds the "
                             "same video.")
        self._event(job_key, "PUBLISHING", f"MANUAL RETRY: media_publish about to be re-sent for existing "
                                           f"container {container_id} (user-confirmed; no new container, "
                                           f"no upload)", at=now)
        return self.get(job_key)  # type: ignore[return-value]

    # ------------------------------------------------------------ one-time override (schema v6)
    def record_override(self, *, kind: str, blocker: "JobRecord", blocker_container_status: str,
                        account_profile: str, account_username: Optional[str], upload_method: str,
                        video_url: str, new_job_key: str, confirmation: str) -> dict:
        """Write the user's confirmed exception BEFORE any Meta write. Fails if this blocker was ever
        overridden before (UNIQUE), so the exception can be used for one new job only."""
        now = utcnow()
        try:
            cur = self.conn.execute(
                "INSERT INTO overrides (kind, blocker_job_id, blocker_job_key, blocker_container_id, "
                "blocker_container_status, account_id, account_profile, account_username, content_hash, "
                "upload_method, video_url, new_job_key, confirmation, confirmed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, blocker.id, blocker.job_key, blocker.container_id, blocker_container_status,
                 blocker.account_id, account_profile, account_username, blocker.content_hash, upload_method,
                 video_url, new_job_key, confirmation, now))
        except sqlite3.IntegrityError:
            raise StateError(f"An override for job #{blocker.id} was already recorded; it can be used once only.")
        return self.get_override(cur.lastrowid)  # type: ignore[return-value]

    def get_override(self, override_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM overrides WHERE id = ?", (override_id,)).fetchone()
        return dict(row) if row else None

    def override_for_blocker(self, blocker_job_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM overrides WHERE blocker_job_id = ?", (blocker_job_id,)).fetchone()
        return dict(row) if row else None

    def overrides(self) -> List[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM overrides ORDER BY id").fetchall()]

    def begin_override_publish(self, job_key: str, override_id: int) -> "JobRecord":
        """Like begin_publish, READY -> PUBLISHING committed before the request leaves, but the ONE
        blocker named by an unclaimed override bound to this exact job is not counted. Every other
        duplicate/account check still applies. The job claim and the override claim commit together."""
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="o.account_id")
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            ov = self.conn.execute(
                "SELECT blocker_job_key FROM overrides WHERE id = ? AND new_job_key = ? "
                "AND publish_claimed_at IS NULL", (override_id, job_key)).fetchone()
            if ov is None:
                raise StateError("Refusing to publish: the override is not bound to this job or was already used.")
            skip = ov["blocker_job_key"]
            cur = self.conn.execute(
                "UPDATE jobs SET state='PUBLISHING', publish_attempts = publish_attempts + 1, "
                "publish_started_at = ?, updated_at = ?, failure_phase = NULL "
                "WHERE job_key = ? AND state = 'READY' AND publish_attempts = 0 "
                f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key AND o.job_key <> ? "
                f"AND o.content_hash = jobs.content_hash AND (o.account_id = jobs.account_id OR {unknown}) "
                f"AND {blocking_sql('o')}) "
                f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key AND o.job_key <> ? "
                f"AND (o.account_id = jobs.account_id OR {unknown}) AND {uncertain_sql('o')})",
                (now, now, job_key, skip, skip))
            if cur.rowcount != 1:
                raise StateError("Refusing to publish: job is not READY, or another job (besides the overridden "
                                 "one) holds this video or is unresolved on this account.")
            self.conn.execute("UPDATE overrides SET publish_claimed_at = ? WHERE id = ?", (now, override_id))
            self._event(job_key, "PUBLISHING", f"media_publish about to be sent under override #{override_id} "
                                               f"(blocker {skip[:12]}… left unchanged)", at=now)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(job_key)  # type: ignore[return-value]

    # ------------------------------------------------------------ diagnostic override (schema v7)
    def record_diagnostic_override(self, *, kind: str, account_id: str, account_profile: str,
                                   account_username: Optional[str], content_hash: str, publish_mode: str,
                                   upload_method: str, new_job_key: str, confirmation: str,
                                   blockers: List[tuple], allowed_duplicate_job_key: Optional[str] = None) -> dict:
        """blockers: (JobRecord, container_status_read_from_meta). One transaction; fails if this kind
        was ever recorded before (UNIQUE), so the diagnostic is single-use.
        allowed_duplicate_job_key: the ONE blocker whose (same) video may be attempted again; it must be
        one of ``blockers``."""
        if allowed_duplicate_job_key is not None and allowed_duplicate_job_key not in {r.job_key for r, _ in blockers}:
            raise StateError("The allowed duplicate must be one of the override's recorded blockers.")
        if any(st == ACCEPTED_PUBLISHED_DUPLICATE and r.state != "PUBLISHED" for r, st in blockers):
            raise StateError("Only a PUBLISHED job can be recorded as an accepted published duplicate.")
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO diagnostic_overrides (kind, account_id, account_profile, account_username, "
                "content_hash, publish_mode, upload_method, new_job_key, confirmation, confirmed_at, "
                "allowed_duplicate_job_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (kind, account_id, account_profile, account_username, content_hash, publish_mode, upload_method,
                 new_job_key, confirmation, now, allowed_duplicate_job_key))
            oid = cur.lastrowid
            for rec, status in blockers:
                self.conn.execute(
                    "INSERT INTO diagnostic_override_blockers (override_id, blocker_job_id, blocker_job_key, "
                    "blocker_container_id, blocker_container_status) VALUES (?,?,?,?,?)",
                    (oid, rec.id, rec.job_key, rec.container_id, status))
            self.conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            raise StateError(f"The one-time diagnostic override {kind!r} was already recorded; it can be used once only.")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_diagnostic_override(oid)  # type: ignore[return-value]

    def get_diagnostic_override(self, override_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM diagnostic_overrides WHERE id = ?", (override_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["blockers"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM diagnostic_override_blockers WHERE override_id = ? ORDER BY blocker_job_id",
            (override_id,)).fetchall()]
        d["blocker_job_keys"] = [b["blocker_job_key"] for b in d["blockers"]]
        return d

    def diagnostic_override_by_kind(self, kind: str) -> Optional[dict]:
        row = self.conn.execute("SELECT id FROM diagnostic_overrides WHERE kind = ?", (kind,)).fetchone()
        return self.get_diagnostic_override(row["id"]) if row else None

    def diagnostic_overrides(self) -> List[dict]:
        return [self.get_diagnostic_override(r["id"]) for r in
                self.conn.execute("SELECT id FROM diagnostic_overrides ORDER BY id").fetchall()]

    def begin_diagnostic_publish(self, job_key: str, override_id: int) -> "JobRecord":
        """READY -> PUBLISHING, committed before the request leaves. The ACCOUNT-PAUSE check skips only
        the blockers recorded for this unclaimed override bound to this exact job. The duplicate check
        (same video on this account) is applied in full, except for the override's single
        allowed_duplicate_job_key (only while that job is still UNKNOWN) and a job recorded on the override as
        ACCEPTED_PUBLISHED_DUPLICATE (only while it is PUBLISHED)."""
        unknown = UNKNOWN_ACCOUNT_SQL.format(col="o.account_id")
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            ok = self.conn.execute(
                "SELECT 1 FROM diagnostic_overrides WHERE id = ? AND new_job_key = ? AND publish_claimed_at IS NULL",
                (override_id, job_key)).fetchone()
            if ok is None:
                raise StateError("Refusing to publish: the diagnostic override is not bound to this job or was "
                                 "already used.")
            skip = "(SELECT blocker_job_key FROM diagnostic_override_blockers WHERE override_id = ?)"
            cur = self.conn.execute(
                "UPDATE jobs SET state='PUBLISHING', publish_attempts = publish_attempts + 1, "
                "publish_started_at = ?, updated_at = ?, failure_phase = NULL "
                "WHERE job_key = ? AND state = 'READY' AND publish_attempts = 0 "
                f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key "
                f"AND o.content_hash = jobs.content_hash AND (o.account_id = jobs.account_id OR {unknown}) "
                f"AND {blocking_sql('o')} "
                "AND NOT (o.state = 'UNKNOWN' AND o.job_key = COALESCE((SELECT allowed_duplicate_job_key FROM "
                "diagnostic_overrides WHERE id = ?), '')) "
                "AND NOT (o.state = 'PUBLISHED' AND o.job_key IN (SELECT blocker_job_key FROM "
                "diagnostic_override_blockers WHERE override_id = ? AND blocker_container_status = ?))) "
                f"AND NOT EXISTS (SELECT 1 FROM jobs o WHERE o.job_key <> jobs.job_key AND o.job_key NOT IN {skip} "
                f"AND (o.account_id = jobs.account_id OR {unknown}) AND {uncertain_sql('o')})",
                (now, now, job_key, override_id, override_id, ACCEPTED_PUBLISHED_DUPLICATE, override_id))
            if cur.rowcount != 1:
                raise StateError("Refusing to publish: job is not READY, this video is already (or may be) on this "
                                 "account, or an unresolved job not covered by the override exists.")
            self.conn.execute("UPDATE diagnostic_overrides SET publish_claimed_at = ? WHERE id = ?", (now, override_id))
            self._event(job_key, "PUBLISHING", f"media_publish about to be sent under diagnostic override "
                                               f"#{override_id} (blocker jobs left unchanged)", at=now)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(job_key)  # type: ignore[return-value]

    # ------------------------------------------------------------ manual test mode (--test-mode)
    def next_attempt(self, account_id: str, content_hash: str, source_path: str, publish_mode: str,
                     upload_method: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(attempt) FROM jobs WHERE account_id = ? AND content_hash = ? AND source_path = ? "
            "AND publish_mode = ? AND upload_method = ?",
            (account_id, content_hash, source_path, publish_mode, upload_method)).fetchone()
        return (row[0] or 0) + 1

    def begin_test_publish(self, job_key: str, override_id: int) -> "JobRecord":
        """READY -> PUBLISHING for a --test-mode job, committed before the request leaves. The user confirmed
        this exact attempt (a TEST_MODE: row bound to this job), so account locks and duplicate checks are NOT
        applied; the job must still be brand new (READY, never attempted) and the row unclaimed."""
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            ok = self.conn.execute(
                "SELECT 1 FROM diagnostic_overrides WHERE id = ? AND new_job_key = ? AND publish_claimed_at IS NULL "
                "AND kind = ?", (override_id, job_key, TEST_MODE_PREFIX + job_key)).fetchone()
            if ok is None:
                raise StateError("Refusing to publish: the test-mode confirmation is not bound to this job or was "
                                 "already used.")
            cur = self.conn.execute(
                "UPDATE jobs SET state='PUBLISHING', publish_attempts = publish_attempts + 1, "
                "publish_started_at = ?, updated_at = ?, failure_phase = NULL "
                "WHERE job_key = ? AND state = 'READY' AND publish_attempts = 0", (now, now, job_key))
            if cur.rowcount != 1:
                raise StateError("Refusing to publish: test-mode job is not READY or was already attempted.")
            self.conn.execute("UPDATE diagnostic_overrides SET publish_claimed_at = ? WHERE id = ?", (now, override_id))
            self._event(job_key, "PUBLISHING", f"media_publish about to be sent in TEST MODE (#{override_id}; "
                                               "account locks and duplicate checks skipped by the user)", at=now)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(job_key)  # type: ignore[return-value]

    def note(self, job_key: str, detail: str) -> None:
        """Append an informational event (state unchanged)."""
        rec = self.get(job_key)
        if rec is not None:
            self._event(job_key, rec.state, detail)

    def get_by_id(self, job_id: int) -> Optional[JobRecord]:
        row = self.conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRecord(**dict(row)) if row else None

    def _event(self, job_key: str, state: str, detail: Optional[str], at: Optional[str] = None) -> None:
        self.conn.execute("INSERT INTO events (job_key, at, state, detail) VALUES (?,?,?,?)",
                          (job_key, at or utcnow(), state, redact(detail)[:2000] if detail else None))

    def events(self, job_key: str) -> List[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, at, state, detail FROM events WHERE job_key = ? ORDER BY id", (job_key,)).fetchall()]


@contextmanager
def process_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock so two CLI runs can't publish at the same time."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockError("Another publish.py run is in progress. Wait for it to finish.")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
