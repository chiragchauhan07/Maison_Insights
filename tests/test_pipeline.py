import pandas as pd

import check_domains
import collect_all as collect_all_module
import database
import pipeline
from collector import map_to_schema


def actor_item(text="A wonderful experience with excellent quality.", url="https://trustpilot.com/reviews/test"):
    return {"rating": 5, "title": "Lovely", "text": text,
            "publishedDate": "2026-10-01T12:00:00Z", "country": "GB", "language": "en",
            "verificationLevel": "verified", "reviewUrl": url}


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reviews.db")


def test_cached_collection_requires_enough_database_reviews(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    database.save_reviews(map_to_schema([actor_item()], "Gucci", "Fashion"))
    now = pd.Timestamp.now(tz="UTC").isoformat()
    database.log_run("Gucci", now, "success", reviews_fetched=1, reviews_new=1, finished_at=now)
    calls = []

    def fake_collect(brand, domain, limit):
        calls.append(limit)
        return [actor_item(url=f"https://trustpilot.com/reviews/new{i}") for i in range(limit)]

    monkeypatch.setattr(pipeline, "collect_brand", fake_collect)
    monkeypatch.setattr(pipeline, "export_workbook", lambda *args: "report.xlsx")
    result = pipeline.run_pipeline(["Gucci"], max_reviews_per_brand=2)
    assert calls == [2]
    assert result["stats"]["reviews_new_this_run"] == 2


def test_exhausted_brand_is_cacheable_below_requested_limit(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_collect(brand, domain, limit):
        calls.append(limit)
        return [actor_item()]

    monkeypatch.setattr(pipeline, "collect_brand", fake_collect)
    monkeypatch.setattr(pipeline, "export_workbook", lambda *args: "report.xlsx")
    pipeline.run_pipeline(["Gucci"], max_reviews_per_brand=3)
    pipeline.run_pipeline(["Gucci"], max_reviews_per_brand=3)
    assert calls == [3]
    assert database.latest_collection("Gucci")["status"] == "exhausted"


def test_domain_check_uses_mock_and_skips_existing_results(tmp_path):
    calls = []

    def fake_collect(brand, domain, limit):
        calls.append((brand, domain, limit))
        return [actor_item()] if brand == "Gucci" else []

    path = tmp_path / "domain_check.json"
    first = check_domains.run_domain_check(collector=fake_collect, output_path=path)
    first_call_count = len(calls)
    second = check_domains.run_domain_check(collector=fake_collect, output_path=path)
    assert first["Gucci"]["verified"] is True
    assert first["Gucci"]["reviews_found"] == 1
    assert len(calls) == first_call_count
    assert second == first


def test_bulk_collection_is_mocked_and_resumable(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_collect(brand, domain, limit):
        calls.append((brand, limit))
        return [actor_item(url=f"https://trustpilot.com/reviews/{brand}-{i}") for i in range(limit)]

    monkeypatch.setattr(collect_all_module, "export_workbook", lambda *args: "report.xlsx")
    result = collect_all_module.collect_all(2, 2, ["Gucci", "Rolex"], collector=fake_collect)
    assert calls == [("Gucci", 2)]
    assert result["total_reviews_new"] == 2
    assert result["brands"][1]["status"] == "max total reached"
    result2 = collect_all_module.collect_all(2, 10, ["Gucci"], collector=fake_collect)
    assert result2["brands"][0]["status"] == "already at target"
    assert len(calls) == 1


def test_bulk_top_up_raises_limit_until_target(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    calls = []

    def fake_collect(brand, domain, limit):
        calls.append(limit)
        return [actor_item(url=f"https://trustpilot.com/reviews/topup-{len(calls)}-{i}") for i in range(limit)]

    monkeypatch.setattr(collect_all_module, "export_workbook", lambda *args: "report.xlsx")
    result = collect_all_module.collect_all(1, 3, ["Gucci"], top_up=True, collector=fake_collect)
    assert calls == [1, 2]
    assert result["total_reviews_new"] == 3


def test_config_has_exact_brand_order_and_categories():
    from collector import load_config
    config = load_config()
    assert list(config["brands"]) == [
        "Louis Vuitton", "Gucci", "Dior", "Chanel", "Hermès", "Prada", "Saint Laurent", "Balenciaga",
        "Burberry", "Fendi", "Versace", "Valentino", "Givenchy", "Bottega Veneta", "Loewe", "Ralph Lauren",
        "Raymond Luxury", "Rolex", "Cartier", "Tiffany & Co.", "Bulgari", "Omega", "Patek Philippe", "TAG Heuer",
        "Van Cleef & Arpels", "Estée Lauder", "Dior Beauty", "Chanel Beauty", "La Mer", "Tom Ford Beauty",
        "Charlotte Tilbury", "Armani Beauty", "Hourglass", "Sisley", "Forest Essentials", "Kama Ayurveda",
    ]
    assert {info["category"] for info in config["brands"].values()} == {"Fashion", "Watches & Jewellery", "Beauty"}
    assert all(info["verified"] is False for info in config["brands"].values())
    forbidden = {"Celine", "Swarovski", "Lancôme", "Guerlain", "Clarins", "Jo Malone London",
                 "Yves Saint Laurent Beauty", "Christian Louboutin", "Alexander McQueen", "Dolce & Gabbana"}
    assert not forbidden.intersection(config["brands"])


def test_progress_callback_receives_documented_event_dict(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "collect_brand", lambda *args: [actor_item()])
    monkeypatch.setattr(pipeline, "export_workbook", lambda *args: "report.xlsx")
    events = []
    pipeline.run_pipeline(["Gucci"], max_reviews_per_brand=1, progress_callback=events.append)
    assert events
    assert all(set(event) == {"step", "brand", "index", "total", "message"} for event in events)


def test_analysis_for_ui_returns_all_chart_dataframes(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    data = pipeline.analysis_for_ui([])
    expected = {"reviews", "analysis_reviews", "overall", "by_brand", "by_category", "by_platform",
                "rating_vs_sentiment", "review_volume", "themes", "tech_sentiment"}
    assert expected <= data.keys()
    assert all(isinstance(data[key], pd.DataFrame) for key in expected)
