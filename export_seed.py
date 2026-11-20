"""Create a reviewer-name-free database seed from the local review database."""
import argparse
import sqlite3
from pathlib import Path

import pandas as pd

import database
from utils import BASE_DIR


def export_seed(source=None, target=None):
    source = Path(source or database.DB_PATH)
    target = Path(target or BASE_DIR / "seed" / "reviews_seed.db")
    if not source.is_file():
        raise FileNotFoundError(f"Source database not found: {source}")
    if source.resolve() == target.resolve():
        raise ValueError("The seed target must be a different file from the source database.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reviews)")}
        selected = [name for name in database.ALL_COLUMNS if name in columns]
        reviews = pd.read_sql_query(
            f"SELECT {', '.join(f'\"{name}\"' for name in selected)} FROM reviews", connection
        ) if selected else pd.DataFrame(columns=database.ALL_COLUMNS)
        try:
            runs = pd.read_sql_query(
                "SELECT run_id, brand, started_at, finished_at, status, reviews_fetched, reviews_new, error_message, apify_run_id "
                "FROM collection_runs", connection
            )
        except sqlite3.OperationalError:
            runs = pd.DataFrame()

    target.unlink(missing_ok=True)
    with database.use_database(target):
        database.init_db()
        database.save_reviews(reviews.reindex(columns=database.ALL_COLUMNS))
        connection = database._connect()
        try:
            with connection:
                for row in runs.to_dict("records"):
                    connection.execute(
                        "INSERT OR REPLACE INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                        tuple(row.get(key) for key in ("run_id", "brand", "started_at", "finished_at", "status",
                                                       "reviews_fetched", "reviews_new", "error_message", "apify_run_id")),
                    )
        finally:
            connection.close()
    return target


def main():
    parser = argparse.ArgumentParser(description="Export a reviewer-name-free SQLite seed.")
    parser.add_argument("--source", default=str(database.DB_PATH))
    parser.add_argument("--target", default=str(BASE_DIR / "seed" / "reviews_seed.db"))
    args = parser.parse_args()
    output = export_seed(args.source, args.target)
    print(f"Sanitized seed written to {output}")


if __name__ == "__main__":
    main()
