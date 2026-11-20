import sqlite3

import database
from export_seed import export_seed


def test_seed_export_whitelists_review_columns_and_preserves_cache_history(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "seed" / "reviews_seed.db"
    with sqlite3.connect(source) as connection:
        definitions = [f'"{name}" TEXT' for name in database.ALL_COLUMNS]
        definitions[0] = '"Review_ID" TEXT PRIMARY KEY'
        definitions[database.ALL_COLUMNS.index("Rating")] = '"Rating" INTEGER'
        connection.execute(f"CREATE TABLE reviews ({', '.join(definitions)}, author_name TEXT, author_profile_url TEXT)")
        columns = database.ALL_COLUMNS + ["author_name", "author_profile_url"]
        values = ["seed-id" if name == "Review_ID" else 5 if name == "Rating" else
                  "SENTINEL_REVIEWER_NAME" if name == "author_name" else "https://example.invalid/profile"
                  if name == "author_profile_url" else "Not stated" for name in columns]
        connection.execute(f"INSERT INTO reviews VALUES ({','.join('?' for _ in columns)})", values)
        connection.execute("""CREATE TABLE collection_runs (
            run_id TEXT PRIMARY KEY, brand TEXT, started_at TEXT, finished_at TEXT, status TEXT,
            reviews_fetched INTEGER, reviews_new INTEGER, error_message TEXT, apify_run_id TEXT)""")
        connection.execute("INSERT INTO collection_runs VALUES ('run-1','Gucci','start','finish','success',1,1,'','')")

    assert export_seed(source, target) == target
    with sqlite3.connect(target) as connection:
        columns = [row[1].lower() for row in connection.execute("PRAGMA table_info(reviews)")]
        row = connection.execute("SELECT Rating, Review_ID FROM reviews").fetchone()
        run_count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert row == (5, "seed-id") and run_count == 1
    assert not any(any(term in column for term in ("author", "profile", "reviewer")) for column in columns)
    assert "SENTINEL_REVIEWER_NAME" not in target.read_bytes().decode("latin1", errors="ignore")
