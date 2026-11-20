"""Collect reviews across configured brands with an explicit cost confirmation."""
import argparse
import pandas as pd
from collector import collect_brand, load_config, map_to_schema
from cleaner import clean_reviews
from database import count_reviews_for_brand, get_overview_stats, get_reviews, log_run, save_reviews, update_sentiment
from exporter import export_workbook
from sentiment import analyze_sentiment
from utils import setup_logging


def collect_all(per_brand=40, max_total=1500, brands=None, top_up=False, collector=None):
    """Collect sequentially, resume from SQLite, optionally top up active brands, then export."""
    logger = setup_logging()
    config = load_config()
    catalog = config["brands"]
    aliases = config.get("aliases", {})
    selected = list(catalog) if not brands else list(dict.fromkeys(aliases.get(name, name) for name in brands))
    collector = collector or collect_brand
    cap = int(config["apify"]["max_reviews_per_brand"])
    per_brand = min(max(1, int(per_brand)), cap)
    max_total = max(1, int(max_total))
    total_before = get_overview_stats()["total_reviews"]
    total_new = total_fetched = 0
    results = {brand: {"brand": brand, "fetched": 0, "new": 0,
                       "total_in_db": count_reviews_for_brand(brand), "status": "pending"}
               for brand in selected}
    active_for_topup = []

    def collect_one(brand, requested, label):
        nonlocal total_new, total_fetched
        result = results[brand]
        if brand not in catalog:
            result["status"] = "unknown brand"
            return 0
        if requested <= 0:
            result["status"] = "max total reached"
            return 0
        started = pd.Timestamp.utcnow().isoformat()
        try:
            if hasattr(collector, "last_error"):
                collector.last_error = ""
            raw = collector(brand, catalog[brand]["domain"], requested)
            raw = list(raw or [])[:requested]
            fetched = len(raw)
            result["fetched"] += fetched
            total_fetched += fetched
            error = getattr(collector, "last_error", "")
            if error:
                result["status"] = "failed: Apify collection error"
                log_run(brand, started, "failed", fetched, 0, "Apify collection error.")
                logger.error("Collection failed for %s.", brand)
                return fetched
            mapped = map_to_schema(raw, brand, catalog[brand]["category"], config["settings"].get("tech_keywords"))
            saved = save_reviews(mapped)
            result["new"] += saved["new"]
            total_new += saved["new"]
            result["total_in_db"] = count_reviews_for_brand(brand)
            status = "exhausted" if fetched < requested else "success"
            result["status"] = f"{label}: {status}"
            if fetched < requested:
                result["status"] += f" (returned {fetched} of {requested} requested)"
            log_run(brand, started, status, fetched, saved["new"], "")
            return fetched
        except Exception as error:
            result["status"] = "failed: collection processing error"
            log_run(brand, started, "failed", error_message="Collection processing error.")
            logger.error("Collection processing failed for %s (%s).", brand, type(error).__name__)
            return 0

    for brand in selected:
        total_now = total_before + total_new
        if total_now >= max_total:
            results[brand]["status"] = "max total reached"
            continue
        if brand not in catalog:
            results[brand]["status"] = "unknown brand"
            continue
        existing = count_reviews_for_brand(brand)
        if existing >= per_brand:
            results[brand]["status"] = "already at target"
            continue
        request = min(per_brand - existing, max_total - total_now)
        fetched = collect_one(brand, request, "initial")
        if top_up and fetched == request and results[brand]["status"].startswith("initial: success"):
            active_for_topup.append(brand)

    if top_up:
        for brand in active_for_topup:
            while total_before + total_new < max_total:
                current = count_reviews_for_brand(brand)
                remaining_for_brand = cap - current
                remaining_global = max_total - (total_before + total_new)
                request = min(remaining_for_brand, remaining_global)
                if request <= 0:
                    break
                fetched_before = results[brand]["fetched"]
                new_before = total_new
                fetched = collect_one(brand, request, "top-up")
                if fetched < request or total_new == new_before:
                    break
                if results[brand]["fetched"] == fetched_before:
                    break

    all_reviews = get_reviews(selected)
    cleaned, stats = clean_reviews(all_reviews)
    scored = analyze_sentiment(cleaned) if not cleaned.empty else cleaned.copy()
    update_sentiment(scored)
    stats["reviews_new_this_run"] = total_new
    stats["total_collected_this_run"] = total_fetched
    excel_path = export_workbook(cleaned, scored, stats)
    return {"brands": list(results.values()), "stats": stats, "excel_path": excel_path,
            "total_reviews_before": total_before, "total_reviews_new": total_new}


def main():
    parser = argparse.ArgumentParser(description="Collect Trustpilot reviews across brands")
    parser.add_argument("--per-brand", type=int, default=40)
    parser.add_argument("--max-total", type=int, default=1500)
    parser.add_argument("--brands", default="", help="Optional comma-separated configured brands")
    parser.add_argument("--top-up", action="store_true", help="Raise limits for brands that reached their requested limit")
    parser.add_argument("--yes", action="store_true", help="Confirm the estimated Apify spend")
    args = parser.parse_args()
    config = load_config()
    selected = [name.strip() for name in args.brands.split(",") if name.strip()] if args.brands else list(config["brands"])
    limit = min(max(1, args.per_brand), config["apify"]["max_reviews_per_brand"])
    review_cap = int(config["apify"]["max_reviews_per_brand"])
    review_estimate = min(max(1, args.max_total), (review_cap if args.top_up else limit) * len(selected))
    run_estimate = len(selected) + (len(selected) if args.top_up else 0)
    cost = review_estimate * 0.001 + run_estimate * 0.005
    print(f"Brands: {len(selected)}. Estimated maximum cost: about ${cost:.2f} "
          f"({review_estimate} reviews at $0.001 plus up to {run_estimate} runs at $0.005).")
    if not args.yes:
        print("No collection started. Re-run with --yes to proceed.")
        return 0
    result = collect_all(limit, args.max_total, selected, args.top_up)
    print("Brand | Fetched | New | Total in DB | Status")
    for row in result["brands"]:
        print(f"{row['brand']} | {row['fetched']} | {row['new']} | {row['total_in_db']} | {row['status']}")
    print(f"New unique reviews: {result['total_reviews_new']}")
    print(f"Excel: {result['excel_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
