"""FastAPI route tests use a mocked actor collector and never call Apify."""
import io
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

import database
import server
from collector import SCHEMA_COLUMNS, load_config, map_to_schema
from collector import collect_brand_live


def review(brand="Gucci", rating=5, text="The craftsmanship is beautiful and the service felt wonderful.", url="first"):
    return {"rating": rating, "title": "Wonderful craftsmanship", "text": text,
            "publishedDate": "2026-10-01T12:00:00Z", "country": "GB", "language": "en",
            "verificationLevel": "verified", "reviewUrl": f"https://www.trustpilot.com/reviews/{brand}-{url}",
            "authorName": "SENTINEL_REVIEWER_NAME", "authorProfileUrl": "https://example.invalid/profile"}


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")
    monkeypatch.setattr(server, "manager", server.JobManager())
    monkeypatch.setattr(server, "get_apify_token", lambda: "test-token")
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.50")
    monkeypatch.setenv("MAX_BRANDS_PER_JOB", "6")
    monkeypatch.setenv("MAX_PER_BRAND", "100")
    monkeypatch.setenv("MAX_JOBS_PER_IP_PER_HOUR", "5")
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.setenv("DEMO_STAGE_DELAY_SECONDS", "0")
    monkeypatch.setenv("DEMO_STEP_DELAY_SECONDS", "0")
    with TestClient(server.app) as client:
        yield client


def fake_actor(monkeypatch, calls=None):
    calls = calls if calls is not None else []

    def collect(brand, domain, limit, progress_callback=None):
        calls.append(brand)
        if progress_callback:
            progress_callback({"status": "SUCCEEDED", "elapsed_seconds": 1,
                               "item_count": 2, "message": "Apify status SUCCEEDED; elapsed 1s."})
        return [
            review(brand, 5, "The craftsmanship is beautiful and delivery was wonderful.", "positive"),
            review(brand, 1, "The service was dreadful and the delivery was very disappointing.", "negative"),
        ][:limit]

    monkeypatch.setattr(server, "collect_brand_live", collect)
    return calls


def test_homepage_serves_the_research_workspace_not_a_kitchen(api_client):
    response = api_client.get("/")
    assert response.status_code == 200
    assert "Brand intelligence" in response.text
    assert "Collection activity" in response.text
    assert "THE REVIEW KITCHEN" not in response.text
    assert "chef" not in response.text.lower()
def create_order(client, brands=None, per_brand=2, **extra):
    return client.post("/api/jobs", json={"brands": brands or ["Gucci"],
                                          "per_brand": per_brand, **extra})


def wait_done(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}/result")
        if response.status_code != 409:
            return response
        time.sleep(0.025)
    raise AssertionError("The mocked order did not finish in time.")


def test_job_validation_rejects_too_many_unknown_and_over_budget(api_client, monkeypatch):
    names = list(load_config()["brands"])
    assert create_order(api_client, names[:7], 1).status_code == 400
    assert create_order(api_client, ["Not a configured brand"], 1).status_code == 400
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.0001")
    response = create_order(api_client, ["Gucci"], 1)
    assert response.status_code == 402
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.50")
    assert create_order(api_client, ["Gucci"], 101).status_code == 422
    assert api_client.get("/api/health").headers["x-content-type-options"] == "nosniff"


def test_sse_contains_all_pipeline_stages_and_reconnect_replays(api_client, monkeypatch):
    calls = fake_actor(monkeypatch)
    accepted = create_order(api_client, ["Gucci", "Rolex"], per_brand=2)
    assert accepted.status_code == 200
    job_id = accepted.json()["job_id"]
    assert wait_done(api_client, job_id).status_code == 200
    stream = api_client.get(f"/api/jobs/{job_id}/events")
    events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    stages = [event["stage"] for event in events]
    assert stages[0] == "order"
    for stage in ("fetch", "prep", "cook", "plate", "serve"):
        assert stage in stages
    first_occurrence = list(dict.fromkeys(stages))
    assert first_occurrence[:6] == ["order", "fetch", "prep", "cook", "plate", "serve"]
    for brand in ("Gucci", "Rolex"):
        assert sum(event["type"] == "brand_done" and event["brand"] == brand for event in events) == 1
    last = events[2]["seq"]
    replay = api_client.get(f"/api/jobs/{job_id}/events?last_event_id={last}")
    replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    assert replayed and min(event["seq"] for event in replayed) > last
    assert calls == ["Gucci", "Rolex"]


def test_cached_brand_reuses_local_reviews_without_apify(api_client, monkeypatch, tmp_path):
    row = map_to_schema([review("Gucci", url="cached")], "Gucci", "Fashion")
    database.save_reviews(row)
    now = datetime.now(timezone.utc).isoformat()
    database.log_run("Gucci", now, "success", reviews_fetched=1, reviews_new=1, finished_at=now)
    monkeypatch.setattr(server, "get_apify_token", lambda: (_ for _ in ()).throw(AssertionError("token lookup should not run")))
    monkeypatch.setattr(server, "collect_brand_live", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Apify must not run")))
    accepted = create_order(api_client, ["Gucci"], per_brand=1)
    assert accepted.status_code == 200
    job_id = accepted.json()["job_id"]
    assert wait_done(api_client, job_id).status_code == 200
    events = api_client.get(f"/api/jobs/{job_id}/events").text
    assert '"cached": true' in events
    assert "local cache" in events.lower()


def test_download_has_expected_columns_numeric_rating_and_no_author_data(api_client, monkeypatch):
    fake_actor(monkeypatch)
    accepted = create_order(api_client, ["Gucci"], per_brand=2)
    job_id = accepted.json()["job_id"]
    result_response = wait_done(api_client, job_id)
    assert result_response.status_code == 200
    assert "rating_sentiment_mismatch_count" in result_response.json()["stats"]
    assert "rating_sentiment_mismatch_rate" in result_response.json()["stats"]
    review_response = api_client.get(f"/api/jobs/{job_id}/reviews")
    assert review_response.status_code == 200
    stream = api_client.get(f"/api/jobs/{job_id}/events").text
    downloaded = api_client.get(f"/api/jobs/{job_id}/download")
    assert downloaded.status_code == 200
    book = load_workbook(io.BytesIO(downloaded.content), data_only=True)
    assert book.sheetnames == ["Cleaned Data", "Sentiment Data", "Summary", "Sentiment Report", "Themes", "Collection Log"]
    sentiment = book["Sentiment Data"]
    columns = [cell.value for cell in sentiment[1]]
    assert columns[:13] == SCHEMA_COLUMNS
    assert columns[13:] == ["Review_Title", "Sentiment_Score", "Sentiment_Label", "Is_Valid", "Flags",
                            "Rating_Verified", "Rating_Sentiment_Mismatch"]
    rating_index = columns.index("Rating") + 1
    assert sentiment.cell(2, rating_index).value == 5
    assert isinstance(sentiment.cell(2, rating_index).value, int)
    combined = result_response.text + review_response.text + stream + downloaded.content.decode("latin1")
    assert "SENTINEL_REVIEWER_NAME" not in combined
    assert "authorProfileUrl" not in combined and "authorName" not in combined
    with sqlite3.connect(database.DB_PATH) as connection:
        stored = connection.execute("SELECT * FROM reviews").fetchall()
        names = [column[1].lower() for column in connection.execute("PRAGMA table_info(reviews)")]
    assert not any("author" in name or "profile" in name for name in names)
    assert "SENTINEL_REVIEWER_NAME" not in repr(stored)


def test_proxy_forwarded_ip_is_used_for_hourly_rate_limit(api_client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("MAX_JOBS_PER_IP_PER_HOUR", "1")
    first = api_client.post("/api/jobs", json={"brands": ["Gucci"], "per_brand": 1, "demo": True},
                            headers={"X-Forwarded-For": "198.51.100.10"})
    second = api_client.post("/api/jobs", json={"brands": ["Rolex"], "per_brand": 1, "demo": True},
                             headers={"X-Forwarded-For": "198.51.100.10"})
    third = api_client.post("/api/jobs", json={"brands": ["Dior"], "per_brand": 1, "demo": True},
                            headers={"X-Forwarded-For": "203.0.113.20"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert third.status_code == 200


def test_dataset_read_error_never_starts_a_second_actor_run(monkeypatch):
    import collector

    monkeypatch.setattr(collector, "get_apify_token", lambda: "test-token")
    starts = []

    class Dataset:
        def get(self):
            return type("Info", (), {"item_count": 1})()

        def iterate_items(self):
            raise OSError("mock dataset read error")

    class FakeClient:
        def actor(self, actor_id):
            return type("Actor", (), {"start": lambda self, **kwargs: (starts.append(kwargs) or type(
                "Run", (), {"id": "run-1", "status": "SUCCEEDED", "default_dataset_id": "dataset-1"})())})()

        def run(self, run_id):
            return type("RunInfo", (), {"get": lambda self: type(
                "Run", (), {"status": "SUCCEEDED", "default_dataset_id": "dataset-1"})()})()

        def dataset(self, dataset_id):
            return Dataset()

    with pytest.raises(OSError, match="mock dataset read error"):
        collect_brand_live("Gucci", "gucci.com", 1, poll_seconds=0.1,
                           sleep=lambda _: None, client_factory=lambda token: FakeClient())
    assert len(starts) == 1


def test_security_headers_and_request_body_size_limit(api_client):
    response = api_client.get("/api/health")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    oversized = api_client.post("/api/jobs", content=b" " * 32769,
                                headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413

