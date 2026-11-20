import sqlite3

import pandas as pd
import yaml
from openpyxl import load_workbook

import database
import pipeline
import sentiment
from cleaner import clean_reviews
from collector import find_tech_keyword, map_to_schema
from exporter import export_workbook


def _item(rating, title, text, url):
    return {"rating": rating, "title": title, "text": text,
            "reviewUrl": f"https://trustpilot.com/reviews/{url}",
            "publishedDate": "2026-10-01T00:00:00Z", "language": "en", "country": "GB"}


def _set_sentiment_mode(monkeypatch, tmp_path, mode):
    config = yaml.safe_load((sentiment.BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["settings"]["sentiment_method"] = mode
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(sentiment, "BASE_DIR", tmp_path)


def test_vader_scores_title_and_text_and_flags_rating_mismatch(monkeypatch, tmp_path):
    _set_sentiment_mode(monkeypatch, tmp_path, "vader")
    frame = pd.DataFrame([
        {"Review_Title": "Wonderful service", "Review_Text": "The delivery was late and frustrating.", "Language": "en", "Rating": 1, "Flags": ""},
        {"Review_Title": "Terrible quality", "Review_Text": "The product was acceptable.", "Language": "en", "Rating": 5, "Flags": ""},
    ])
    scored = sentiment.analyze_sentiment(frame)
    assert scored["Sentiment_Score"].tolist() == [0.2023, -0.2023]
    assert scored["Sentiment_Label"].tolist() == ["Positive", "Negative"]
    assert scored["Rating_Sentiment_Mismatch"].tolist() == [True, True]
    assert all("rating_sentiment_mismatch" in value for value in scored["Flags"])
    assert all("label_from_rating" not in value for value in scored["Flags"])
    assert scored["Review_Text"].tolist() == frame["Review_Text"].tolist()


def test_vader_rating_aware_relabels_and_keeps_vader_score(monkeypatch, tmp_path):
    _set_sentiment_mode(monkeypatch, tmp_path, "vader_rating_aware")
    frame = pd.DataFrame([{"Review_Title": "Wonderful", "Review_Text": "Late delivery was frustrating.",
                           "Language": "en", "Rating": 1, "Flags": ""}])
    scored = sentiment.analyze_sentiment(frame)
    assert scored.loc[0, "Sentiment_Label"] == "Negative"
    assert scored.loc[0, "Sentiment_Score"] == sentiment.SentimentIntensityAnalyzer().polarity_scores("Wonderful Late delivery was frustrating.")["compound"]
    assert scored.loc[0, "Flags"] == "rating_sentiment_mismatch;label_from_rating"


def test_technology_context_uses_only_explicit_whole_word_terms():
    for text in ("shopping online", "visit our website", "the happy customer"): 
        assert find_tech_keyword(text) == "Not stated"
    assert find_tech_keyword("I used the app") == "app"
    assert find_tech_keyword("mobile app was useful") == "mobile app"
    assert find_tech_keyword("artificial intelligence helped") == "artificial intelligence"
    assert find_tech_keyword("augmented reality was useful") == "augmented reality"
    assert find_tech_keyword("the ar filter failed") == "Not stated"
    assert find_tech_keyword("the AR filter failed") == "AR"


def test_rating_is_integer_in_mapping_database_and_excel(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")
    frame = map_to_schema([_item("5", "Lovely", "Excellent craftsmanship and service.", "int-rating")], "Gucci", "Fashion")
    assert frame.loc[0, "Rating"] == 5 and pd.api.types.is_integer_dtype(frame["Rating"])
    cleaned, stats = clean_reviews(frame)
    scored = sentiment.analyze_sentiment(cleaned)
    database.save_reviews(frame)
    with sqlite3.connect(database.DB_PATH) as connection:
        rating_type = next(row[2] for row in connection.execute("PRAGMA table_info(reviews)") if row[1] == "Rating")
        rating_value = connection.execute("SELECT Rating FROM reviews").fetchone()[0]
    assert rating_type == "INTEGER" and rating_value == 5
    path = export_workbook(cleaned, scored, stats, tmp_path / "report.xlsx")
    workbook = load_workbook(path, data_only=True)
    sheet = workbook["Sentiment Data"]
    rating_col = [cell.value for cell in sheet[1]].index("Rating") + 1
    assert sheet.cell(2, rating_col).value == 5
    assert isinstance(sheet.cell(2, rating_col).value, int)
    assert workbook["Summary"][1][0].value == "Metric"


def test_old_rating_text_column_is_migrated_and_personal_columns_are_removed(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    definitions = [f'"{name}" TEXT' for name in database.ALL_COLUMNS]
    definitions[0] = '"Review_ID" TEXT PRIMARY KEY'
    definitions[database.ALL_COLUMNS.index("Rating")] = '"Rating" TEXT'
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE reviews ({', '.join(definitions)}, reviewer_name TEXT, profile_url TEXT)")
        columns = database.ALL_COLUMNS + ["reviewer_name", "profile_url"]
        placeholders = ",".join("?" for _ in columns)
        values = ["rid" if column == "Review_ID" else "5" if column == "Rating" else
                  "SENTINEL_REVIEWER_NAME" if column == "reviewer_name" else "https://example.invalid/profile"
                  if column == "profile_url" else "Not stated" for column in columns]
        connection.execute(f"INSERT INTO reviews VALUES ({placeholders})", values)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    with sqlite3.connect(path) as connection:
        info = list(connection.execute("PRAGMA table_info(reviews)"))
        row = connection.execute("SELECT Rating FROM reviews WHERE Review_ID='rid'").fetchone()
    assert next(column[2] for column in info if column[1] == "Rating") == "INTEGER"
    assert row == (5,)
    assert not any(any(term in column[1].lower() for term in ("author", "profile", "reviewer")) for column in info)


def test_pipeline_emits_brand_done_once_and_reports_mismatches(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")
    monkeypatch.setattr(pipeline, "collect_brand", lambda *args: [
        _item(1, "Wonderful", "This was absolutely wonderful and delightful.", "one"),
        _item(5, "Awful", "This was awful, dreadful, and disappointing.", "two"),
    ])
    monkeypatch.setattr(pipeline, "export_workbook", lambda *args: str(tmp_path / "report.xlsx"))
    events = []
    result = pipeline.run_pipeline(["Gucci"], 5, event_callback=events.append)
    assert len([event for event in events if event["type"] == "brand_done" and event["brand"] == "Gucci"]) == 1
    assert result["stats"]["rating_sentiment_mismatch_count"] == 2
    assert result["stats"]["rating_sentiment_mismatch_rate"] == 1.0
