"""SQLite for local work and PostgreSQL for durable Vercel deployments."""
import hashlib
import json
import os
import sqlite3
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

import pandas as pd

from utils import BASE_DIR
from collector import SCHEMA_COLUMNS, load_config

DB_PATH = BASE_DIR / "data" / "reviews.db"
_DB_PATH_OVERRIDE = ContextVar("db_path_override", default=None)
_SCHEMA_READY = set()
_SCHEMA_LOCK = RLock()
EXTRA_COLUMNS = ["Review_Title", "Sentiment_Score", "Sentiment_Label", "Is_Valid", "Flags", "Rating_Verified"]
ALL_COLUMNS = SCHEMA_COLUMNS + EXTRA_COLUMNS


@contextmanager
def _connection_context(connection):
    """Commit or roll back the connection, then release it in both runtimes."""
    try:
        with connection as active:
            yield active
    finally:
        connection.close()


def _postgres_enabled():
    return _postgres_configured() and _DB_PATH_OVERRIDE.get() is None


def _postgres_configured():
    return bool(__import__("os").getenv("DATABASE_URL"))


def _connect():
    """Open the configured store; isolated demo contexts always remain SQLite."""
    if _postgres_enabled():
        import psycopg
        from psycopg.rows import tuple_row

        return _connection_context(psycopg.connect(__import__("os").environ["DATABASE_URL"], row_factory=tuple_row,
                                                   connect_timeout=10))
    path = _DB_PATH_OVERRIDE.get() or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return _connection_context(sqlite3.connect(path))


def _persistent_connect():
    """Open the shared durable store even while a demo uses an isolated SQLite cache."""
    if not _postgres_configured():
        return _connect()
    import psycopg
    from psycopg.rows import tuple_row

    return _connection_context(psycopg.connect(__import__("os").environ["DATABASE_URL"], row_factory=tuple_row,
                                               connect_timeout=10))


def _execute(connection, sql, params=()):
    if _postgres_enabled():
        sql = sql.replace("?", "%s")
    return connection.execute(sql, params)


def _persistent_execute(connection, sql, params=()):
    if _postgres_configured():
        sql = sql.replace("?", "%s")
    return connection.execute(sql, params)


def _read_sql_query(sql, params=()):
    with _connect() as connection:
        if not _postgres_enabled():
            return pd.read_sql_query(sql, connection, params=list(params))
        cursor = _execute(connection, sql, params)
        columns = [item.name for item in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=columns)


@contextmanager
def use_database(path):
    """Use a separate local SQLite file for isolated demo analysis."""
    token = _DB_PATH_OVERRIDE.set(Path(path))
    try:
        yield
    finally:
        _DB_PATH_OVERRIDE.reset(token)


def init_db():
    """Create review, collection history, usage, rate-limit, and durable job tables."""
    is_postgres = _postgres_enabled()
    database_key = ("postgres", os.getenv("DATABASE_URL")) if is_postgres else ("sqlite", str(_DB_PATH_OVERRIDE.get() or DB_PATH))
    with _SCHEMA_LOCK:
        if database_key in _SCHEMA_READY:
            return
        _create_schema(is_postgres)
        _SCHEMA_READY.add(database_key)


def _create_schema(is_postgres):
    definitions = [f'"{name}" INTEGER' if name == "Rating" else f'"{name}" TEXT' for name in ALL_COLUMNS]
    definitions[0] = '"Review_ID" TEXT PRIMARY KEY'
    with _connect() as connection:
        if is_postgres:
            # Vercel may cold-start several function instances simultaneously.
            # Serialize DDL so concurrent CREATE TABLE IF NOT EXISTS statements
            # cannot race while PostgreSQL registers the table's composite type.
            _execute(connection, "SELECT pg_advisory_xact_lock(704196885201468731)")
        _execute(connection, f"CREATE TABLE IF NOT EXISTS reviews ({', '.join(definitions)})")
        _execute(connection, """CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY, brand TEXT, started_at TEXT, finished_at TEXT,
            status TEXT, reviews_fetched INTEGER, reviews_new INTEGER,
            error_message TEXT, apify_run_id TEXT)""")
        _execute(connection, """CREATE TABLE IF NOT EXISTS api_daily_usage (
            utc_date TEXT PRIMARY KEY, spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0)""")
        _execute(connection, """CREATE TABLE IF NOT EXISTS api_ip_jobs (
            job_id TEXT PRIMARY KEY, ip_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")
        _execute(connection, "CREATE INDEX IF NOT EXISTS idx_api_ip_jobs ON api_ip_jobs(ip_hash, created_at)")
        _execute(connection, """CREATE TABLE IF NOT EXISTS analysis_jobs (
            job_id TEXT PRIMARY KEY, status TEXT NOT NULL, job_json TEXT NOT NULL,
            result_json TEXT, reviews_json TEXT, excel_path TEXT NOT NULL DEFAULT '',
            finished INTEGER NOT NULL DEFAULT 0, event_seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        _execute(connection, """CREATE TABLE IF NOT EXISTS analysis_events (
            job_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(job_id, seq))""")
        if not is_postgres:
            old_columns = {row[1]: row[2].upper() for row in _execute(connection, "PRAGMA table_info(reviews)")}
            unsafe_columns = [name for name in old_columns
                              if any(term in name.lower() for term in ("author", "profile", "reviewer"))]
            if old_columns.get("Rating") != "INTEGER" or unsafe_columns or set(old_columns) - set(ALL_COLUMNS):
                _execute(connection, "ALTER TABLE reviews RENAME TO reviews_old_rating")
                _execute(connection, f"CREATE TABLE reviews ({', '.join(definitions)})")
                columns = ", ".join(f'"{name}"' for name in ALL_COLUMNS)
                projection = ", ".join(
                    ("CASE WHEN trim(CAST(\"Rating\" AS TEXT)) IN ('1','2','3','4','5') THEN CAST(\"Rating\" AS INTEGER) ELSE 'Not stated' END"
                     if name == "Rating" else f'"{name}"') if name in old_columns
                    else ("'Not stated'" if name in SCHEMA_COLUMNS else "''")
                    for name in ALL_COLUMNS
                )
                _execute(connection, f"INSERT INTO reviews ({columns}) SELECT {projection} FROM reviews_old_rating")
                _execute(connection, "DROP TABLE reviews_old_rating")


def save_reviews(df):
    """Insert rows once and report inserted versus ignored counts."""
    init_db()
    if df is None or df.empty:
        return {"new": 0, "ignored": 0}
    data = df.copy()
    for column in ALL_COLUMNS:
        if column not in data:
            data[column] = "Not stated" if column in SCHEMA_COLUMNS else ""
    data = data[ALL_COLUMNS].fillna("Not stated")
    values = []
    for row in data.to_dict("records"):
        cooked = []
        for column in ALL_COLUMNS:
            value = row.get(column)
            if pd.isna(value):
                value = "Not stated" if column in SCHEMA_COLUMNS else ""
            if column == "Rating" and value not in ("", "Not stated", None):
                try:
                    number = int(float(value))
                    value = number if 1 <= number <= 5 else "Not stated"
                except (TypeError, ValueError, OverflowError):
                    value = "Not stated"
            elif value is None:
                value = "Not stated" if column in SCHEMA_COLUMNS else ""
            cooked.append(value)
        values.append(cooked)
    names = ",".join(f'"{name}"' for name in ALL_COLUMNS)
    marks = ",".join("?" for _ in ALL_COLUMNS)
    inserted = 0
    with _connect() as connection:
        for values_row in values:
            cursor = _execute(connection, f'INSERT INTO reviews ({names}) VALUES ({marks}) ON CONFLICT ("Review_ID") DO NOTHING',
                              values_row)
            inserted += max(0, cursor.rowcount)
    return {"new": inserted, "ignored": len(data) - inserted}


def get_reviews(brands=None):
    """Load all reviews, or only the requested brands."""
    init_db()
    if brands:
        marks = ",".join("?" for _ in brands)
        return _read_sql_query(f'SELECT * FROM reviews WHERE "Brand" IN ({marks})', list(brands))
    return _read_sql_query("SELECT * FROM reviews")


def count_reviews_for_brand(brand):
    init_db()
    with _connect() as connection:
        row = _execute(connection, 'SELECT COUNT(*) FROM reviews WHERE "Brand"=?', (brand,)).fetchone()
    return int(row[0])


def latest_collection(brand):
    init_db()
    with _connect() as connection:
        row = _execute(connection, "SELECT finished_at, status, reviews_fetched FROM collection_runs "
                        "WHERE brand=? AND status IN ('success','exhausted') ORDER BY finished_at DESC LIMIT 1",
                        (brand,)).fetchone()
    if not row:
        return None
    return {"finished_at": row[0], "status": row[1], "reviews_fetched": row[2] or 0}


def get_overview_stats():
    init_db()
    config_brands = load_config()["brands"]
    with _connect() as connection:
        counts = dict(_execute(connection, 'SELECT "Brand", COUNT(*) FROM reviews GROUP BY "Brand"').fetchall())
        total = _execute(connection, "SELECT COUNT(*) FROM reviews").fetchone()[0]
        runs = _execute(connection, "SELECT brand, status, finished_at FROM collection_runs "
                         "WHERE status IN ('success','exhausted') ORDER BY finished_at DESC").fetchall()
    last_dates, last_statuses = {}, {}
    for brand, status, finished_at in runs:
        if brand not in last_dates:
            last_dates[brand] = finished_at
            last_statuses[brand] = status
    per_brand = {brand: {"review_count": int(counts.get(brand, 0)),
                         "last_collected": last_dates.get(brand, "Not collected"),
                         "cache_exhausted": last_statuses.get(brand) == "exhausted"}
                 for brand in config_brands}
    return {"total_reviews": int(total), "per_brand": per_brand}


def update_sentiment(df):
    init_db()
    with _connect() as connection:
        for row in df.to_dict("records"):
            _execute(connection, 'UPDATE reviews SET "Sentiment_Score"=?, "Sentiment_Label"=?, "Is_Valid"=?, "Flags"=? WHERE "Review_ID"=?',
                     (row.get("Sentiment_Score"), row.get("Sentiment_Label"), str(row.get("Is_Valid", True)),
                      row.get("Flags", ""), row["Review_ID"]))


def log_run(brand, started_at, status, reviews_fetched=0, reviews_new=0,
            error_message="", apify_run_id="", finished_at=None, run_id=None):
    import uuid
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        _execute(connection, """INSERT INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (run_id) DO UPDATE SET brand=excluded.brand, started_at=excluded.started_at,
            finished_at=excluded.finished_at, status=excluded.status, reviews_fetched=excluded.reviews_fetched,
            reviews_new=excluded.reviews_new, error_message=excluded.error_message, apify_run_id=excluded.apify_run_id""",
            (run_id or str(uuid.uuid4()), brand, started_at, finished_at or now, status,
             reviews_fetched, reviews_new, error_message, apify_run_id))


def get_collection_log():
    init_db()
    return _read_sql_query("SELECT * FROM collection_runs ORDER BY started_at")


def _utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def get_daily_spend():
    init_db()
    with _connect() as connection:
        row = _execute(connection, "SELECT spent_usd FROM api_daily_usage WHERE utc_date=?", (_utc_day(),)).fetchone()
    return float(row[0]) if row else 0.0


def add_daily_spend(amount):
    init_db()
    with _connect() as connection:
        _execute(connection, "INSERT INTO api_daily_usage(utc_date, spent_usd) VALUES (?, ?) "
                 "ON CONFLICT(utc_date) DO UPDATE SET spent_usd=api_daily_usage.spent_usd+excluded.spent_usd",
                 (_utc_day(), max(0.0, float(amount))))


def hash_client_ip(ip_address):
    return hashlib.sha256(str(ip_address or "unknown").encode("utf-8")).hexdigest()


def count_ip_jobs(ip_hash, since):
    init_db()
    with _connect() as connection:
        row = _execute(connection, "SELECT COUNT(*) FROM api_ip_jobs WHERE ip_hash=? AND created_at>=?",
                        (ip_hash, since)).fetchone()
    return int(row[0])


def record_ip_job(ip_hash, job_id):
    init_db()
    with _connect() as connection:
        _execute(connection, "INSERT INTO api_ip_jobs(job_id, ip_hash, created_at) VALUES (?,?,?) "
                 "ON CONFLICT(job_id) DO NOTHING", (job_id, ip_hash, datetime.now(timezone.utc).isoformat()))


def save_job(job):
    """Persist the serializable part of a job before it is published to the queue."""
    init_db()
    record = {key: value for key, value in job.items() if key not in {"event_condition", "events"}}
    record["cached_brands"] = sorted(record.get("cached_brands", []))
    with _persistent_connect() as connection:
        _persistent_execute(connection, "INSERT INTO analysis_jobs(job_id,status,job_json,created_at) VALUES (?,?,?,?)",
                            (job["id"], job["status"], json.dumps(record, ensure_ascii=False, default=str), job["created_at"]))


def load_job(job_id):
    init_db()
    with _persistent_connect() as connection:
        row = _persistent_execute(connection, "SELECT job_json,result_json,reviews_json,excel_path,status,finished "
                                   "FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = json.loads(row[0])
    job.update({"result": json.loads(row[1]) if row[1] else None,
                "reviews": json.loads(row[2]) if row[2] else [], "excel_path": row[3],
                "status": row[4], "finished": bool(row[5]), "events": []})
    job["cached_brands"] = set(job.get("cached_brands", []))
    return job


def update_job(job_id, *, status=None, result=None, reviews=None, excel_path=None, finished=None,
               billable_runs=None):
    """Update durable job fields without clobbering concurrently appended events."""
    init_db()
    fields, params = [], []
    if status is not None:
        fields.append("status=?"); params.append(status)
    if result is not None:
        fields.append("result_json=?"); params.append(json.dumps(result, ensure_ascii=False, default=str))
    if reviews is not None:
        fields.append("reviews_json=?"); params.append(json.dumps(reviews, ensure_ascii=False, default=str))
    if excel_path is not None:
        fields.append("excel_path=?"); params.append(str(excel_path))
    if finished is not None:
        fields.append("finished=?"); params.append(1 if finished else 0)
    if billable_runs is not None:
        fields.append("job_json=json_set(job_json, '$.billable_runs', ?)"); params.append(int(billable_runs))
    if fields:
        params.append(job_id)
        with _persistent_connect() as connection:
            if _postgres_configured() and billable_runs is not None:
                fields[-1] = "job_json=jsonb_set(job_json::jsonb, '{billable_runs}', to_jsonb(%s::integer))::text"
            _persistent_execute(connection, f"UPDATE analysis_jobs SET {', '.join(fields)} WHERE job_id=?", tuple(params))


def append_job_event(job_id, event):
    """Assign a persistent monotonic sequence number, allowing SSE replay across instances."""
    init_db()
    with _persistent_connect() as connection:
        row = _persistent_execute(connection, "UPDATE analysis_jobs SET event_seq=event_seq+1 WHERE job_id=? RETURNING event_seq",
                                   (job_id,)).fetchone()
        if not row:
            return None
        event["seq"] = int(row[0])
        _persistent_execute(connection, "INSERT INTO analysis_events(job_id,seq,payload) VALUES (?,?,?)",
                            (job_id, event["seq"], json.dumps(event, ensure_ascii=False, default=str)))
    return event


def read_job_events(job_id, after=0):
    init_db()
    with _persistent_connect() as connection:
        rows = _persistent_execute(connection, "SELECT payload FROM analysis_events WHERE job_id=? AND seq>? ORDER BY seq",
                                   (job_id, after)).fetchall()
    return [json.loads(row[0]) for row in rows]


def count_open_jobs():
    init_db()
    with _persistent_connect() as connection:
        row = _persistent_execute(connection, "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued','running')").fetchone()
    return int(row[0])


def get_reserved_costs():
    init_db()
    with _persistent_connect() as connection:
        rows = _persistent_execute(connection, "SELECT job_json FROM analysis_jobs WHERE status IN ('queued','running')").fetchall()
    return [float(json.loads(row[0]).get("estimated_cost_usd", 0)) for row in rows]
