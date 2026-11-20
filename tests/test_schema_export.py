import pandas as pd
from openpyxl import load_workbook
from collector import map_to_schema, SCHEMA_COLUMNS, build_actor_input
from utils import make_review_id, parse_date
from cleaner import clean_reviews
from sentiment import analyze_sentiment
import database
from exporter import export_workbook


def sample_item(text="Excellent craftsmanship and beautiful quality.", url="https://trustpilot.com/reviews/a"):
    return {"reviewId": "actor-id", "authorName": "Private Name", "rating": 5, "title": "Excellent",
            "text": text, "publishedDate": "2026-10-02T12:00:00Z", "country": "GB", "language": "en",
            "verificationLevel": "verified", "reviewUrl": url}


def test_review_id_and_date_are_stable():
    assert make_review_id("Trustpilot", "url", "text") == make_review_id("Trustpilot", "url", "text")
    assert parse_date("2026-10-02T12:00:00Z") == "2026-10-02"
    assert parse_date("bad date") == "Not stated"
    assert build_actor_input("gucci.com", 20) == {"companyUrls": ["gucci.com"], "maxReviewsPerCompany": 20}


def test_mapping_preserves_13_columns_and_drops_author():
    frame = map_to_schema([sample_item()], "Gucci", "Fashion")
    assert list(frame.columns[:13]) == SCHEMA_COLUMNS
    assert frame.loc[0, "Review_Text"] == sample_item()["text"]
    assert "Private Name" not in frame.to_json()
    assert frame.loc[0, "Review_Title"] == "Excellent"


def test_cleaning_removes_exact_near_duplicates_and_short_text():
    frame = map_to_schema([sample_item(), sample_item(url="https://trustpilot.com/reviews/b"),
                           sample_item("Excellent craftsmanship and beautiful quality!", "https://trustpilot.com/reviews/c"),
                           sample_item("Short", "https://trustpilot.com/reviews/d")], "Gucci", "Fashion")
    cleaned, stats = clean_reviews(frame)
    assert stats["duplicates_removed"] == 1
    assert stats["near_duplicates_flagged"] == 1
    assert stats["invalid_removed"] == 1
    assert len(cleaned) == 1


def test_vader_labels():
    frame = pd.DataFrame({"Review_Text": ["I absolutely love this wonderful product.", "I hate this terrible experience."],
                          "Language": ["en", "en"], "Flags": ["", ""]})
    scored = analyze_sentiment(frame)
    assert scored["Sentiment_Label"].tolist() == ["Positive", "Negative"]


def test_database_insert_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")
    frame = map_to_schema([sample_item()], "Gucci", "Fashion")
    assert database.save_reviews(frame) == {"new": 1, "ignored": 0}
    assert database.save_reviews(frame) == {"new": 0, "ignored": 1}


def test_export_has_required_column_order_and_six_sheets(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")
    frame = map_to_schema([sample_item()], "Gucci", "Fashion")
    cleaned, stats = clean_reviews(frame)
    scored = analyze_sentiment(cleaned)
    target = tmp_path / "report.xlsx"
    export_workbook(cleaned, scored, stats, target)
    book = load_workbook(target, read_only=True)
    assert book.sheetnames == ["Cleaned Data", "Sentiment Data", "Summary", "Sentiment Report", "Themes", "Collection Log"]
    assert [cell.value for cell in book["Cleaned Data"][1]] == SCHEMA_COLUMNS
