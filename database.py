"""SQLite storage and collection run history."""
import sqlite3
import hashlib
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from utils import BASE_DIR
from collector import SCHEMA_COLUMNS, load_config

DB_PATH = BASE_DIR / "data" / "reviews.db"
_DB_PATH_OVERRIDE = ContextVar("db_path_override", default=None)
EXTRA_COLUMNS = ["Review_Title", "Sentiment_Score", "Sentiment_Label", "Is_Valid", "Flags", "Rating_Verified"]
ALL_COLUMNS = SCHEMA_COLUMNS + EXTRA_COLUMNS


def _connect():
    path = _DB_PATH_OVERRIDE.get() or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


@contextmanager
def use_database(path):
    """Use a separate SQLite file in the current thread, then restore its prior database."""
    token = _DB_PATH_OVERRIDE.set(Path(path))
    try:
        yield
    finally:
        _DB_PATH_OVERRIDE.reset(token)


def init_db():
    """Create the review table and run history table."""
    definitions = [f'"{name}" INTEGER' if name == "Rating" else f'"{name}" TEXT' for name in ALL_COLUMNS]
    definitions[0] = '"Review_ID" TEXT PRIMARY KEY'
    with _connect() as connection:
        connection.execute(f"CREATE TABLE IF NOT EXISTS reviews ({', '.join(definitions)})")
        connection.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY, brand TEXT, started_at TEXT, finished_at TEXT,
            status TEXT, reviews_fetched INTEGER, reviews_new INTEGER,
            error_message TEXT, apify_run_id TEXT)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS api_daily_usage (
            utc_date TEXT PRIMARY KEY, spent_usd REAL NOT NULL DEFAULT 0)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS api_ip_jobs (
            job_id TEXT PRIMARY KEY, ip_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_api_ip_jobs ON api_ip_jobs(ip_hash, created_at)")
        old_columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(reviews)")}
        unsafe_columns = [name for name in old_columns
                          if any(term in name.lower() for term in ("author", "profile", "reviewer"))]
        if old_columns.get("Rating") != "INTEGER" or unsafe_columns or set(old_columns) - set(ALL_COLUMNS):
            connection.execute("ALTER TABLE reviews RENAME TO reviews_old_rating")
            connection.execute(f"CREATE TABLE reviews ({', '.join(definitions)})")
            columns = ", ".join(f'"{name}"' for name in ALL_COLUMNS)
            projection = ", ".join(
                ("CASE WHEN trim(CAST(\"Rating\" AS TEXT)) IN ('1','2','3','4','5') THEN CAST(\"Rating\" AS INTEGER) ELSE 'Not stated' END"
                 if name == "Rating" else f'"{name}"') if name in old_columns
                else ("'Not stated'" if name in SCHEMA_COLUMNS else "''")
                for name in ALL_COLUMNS
            )
            connection.execute(f"INSERT INTO reviews ({columns}) SELECT {projection} FROM reviews_old_rating")
            connection.execute("DROP TABLE reviews_old_rating")


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
    with _connect() as connection:
        before = connection.total_changes
        placeholders = ",".join("?" for _ in ALL_COLUMNS)
        names = ",".join(f'"{name}"' for name in ALL_COLUMNS)
        connection.executemany(f"INSERT OR IGNORE INTO reviews ({names}) VALUES ({placeholders})",
                               values)
        inserted = connection.total_changes - before
    return {"new": inserted, "ignored": len(data) - inserted}


def get_reviews(brands=None):
    """Load all reviews, or only the requested brands."""
    init_db()
    with _connect() as connection:
        if brands:
            marks = ",".join("?" for _ in brands)
            return pd.read_sql_query(f'SELECT * FROM reviews WHERE Brand IN ({marks})', connection, params=list(brands))
        return pd.read_sql_query("SELECT * FROM reviews", connection)


def count_reviews_for_brand(brand):
    """Return the number of stored rows for one canonical brand."""
    init_db()
    with _connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM reviews WHERE Brand=?", (brand,)).fetchone()
    return int(row[0])


def latest_collection(brand):
    """Return the latest successful or exhausted collection result for one brand."""
    init_db()
    with _connect() as connection:
        row = connection.execute(
            "SELECT finished_at, status, reviews_fetched FROM collection_runs "
            "WHERE brand=? AND status IN ('success','exhausted') ORDER BY finished_at DESC LIMIT 1", (brand,)
        ).fetchone()
    if not row:
        return None
    return {"finished_at": row[0], "status": row[1], "reviews_fetched": row[2] or 0}


def get_overview_stats():
    """Return total reviews and per-brand counts with the latest collection date."""
    init_db()
    config_brands = load_config()["brands"]
    with _connect() as connection:
        counts = dict(connection.execute("SELECT Brand, COUNT(*) FROM reviews GROUP BY Brand").fetchall())
        total = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        last_dates = dict(connection.execute(
            "SELECT brand, MAX(finished_at) FROM collection_runs WHERE status IN ('success','exhausted') GROUP BY brand"
        ).fetchall())
    per_brand = {
        brand: {"review_count": int(counts.get(brand, 0)), "last_collected": last_dates.get(brand, "Not collected")}
        for brand in config_brands
    }
    return {"total_reviews": int(total), "per_brand": per_brand}


def update_sentiment(df):
    """Write cleaned validity, flags and sentiment fields back to SQLite."""
    init_db()
    fields = ["Review_ID", "Sentiment_Score", "Sentiment_Label", "Is_Valid", "Flags"]
    with _connect() as connection:
        for row in df.to_dict("records"):
            connection.execute("UPDATE reviews SET Sentiment_Score=?, Sentiment_Label=?, Is_Valid=?, Flags=? WHERE Review_ID=?",
                               (row.get("Sentiment_Score"), row.get("Sentiment_Label"), str(row.get("Is_Valid", True)),
                                row.get("Flags", ""), row["Review_ID"]))


def log_run(brand, started_at, status, reviews_fetched=0, reviews_new=0,
            error_message="", apify_run_id="", finished_at=None, run_id=None):
    """Add one collection attempt to the run history."""
    import uuid
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute("INSERT OR REPLACE INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                           (run_id or str(uuid.uuid4()), brand, started_at, finished_at or now,
                            status, reviews_fetched, reviews_new, error_message, apify_run_id))


def get_collection_log():
    """Return collection history for the workbook."""
    init_db()
    with _connect() as connection:
        return pd.read_sql_query("SELECT * FROM collection_runs ORDER BY started_at", connection)


def _utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def get_daily_spend():
    """Return Apify cost recorded for the current UTC day."""
    init_db()
    with _connect() as connection:
        row = connection.execute("SELECT spent_usd FROM api_daily_usage WHERE utc_date=?", (_utc_day(),)).fetchone()
    return float(row[0]) if row else 0.0


def add_daily_spend(amount):
    """Add estimated Apify cost to today's UTC budget record."""
    init_db()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO api_daily_usage(utc_date, spent_usd) VALUES (?, ?) "
            "ON CONFLICT(utc_date) DO UPDATE SET spent_usd=spent_usd+excluded.spent_usd",
            (_utc_day(), max(0.0, float(amount))),
        )


def hash_client_ip(ip_address):
    """Store a one-way identifier for rate limiting without retaining raw IPs."""
    return hashlib.sha256(str(ip_address or "unknown").encode("utf-8")).hexdigest()


def count_ip_jobs(ip_hash, since):
    """Count accepted jobs from an IP hash since an ISO timestamp."""
    init_db()
    with _connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM api_ip_jobs WHERE ip_hash=? AND created_at>=?", (ip_hash, since)).fetchone()
    return int(row[0])


def record_ip_job(ip_hash, job_id):
    """Record one accepted job for hourly rate limiting."""
    init_db()
    with _connect() as connection:
        connection.execute("INSERT OR IGNORE INTO api_ip_jobs(job_id, ip_hash, created_at) VALUES (?,?,?)",
                           (job_id, ip_hash, datetime.now(timezone.utc).isoformat()))
