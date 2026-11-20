"""Plain command-line entry point for the backend."""
import argparse
from pipeline import run_pipeline
import pipeline
import database
from utils import BASE_DIR

SAMPLE = [
    {"reviewId": "sample1", "authorName": "Removed", "rating": 5, "title": "Excellent quality",
     "text": "Excellent quality and beautiful craftsmanship.", "publishedDate": "2026-10-02T12:00:00Z",
     "country": "GB", "language": "en", "verificationLevel": "verified", "reviewUrl": "https://www.trustpilot.com/reviews/sample1"},
    {"reviewId": "sample2", "authorName": "Removed", "rating": 1, "title": "Poor service",
     "text": "Poor service and extremely late delivery.", "publishedDate": "2026-10-03T12:00:00Z",
     "country": "US", "language": "en", "verificationLevel": "verified", "reviewUrl": "https://www.trustpilot.com/reviews/sample2"},
]


def main():
    parser = argparse.ArgumentParser(description="Luxury brand review sentiment backend")
    parser.add_argument("--brands", required=True, help="Comma-separated configured brand names")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Use built-in sample records; never call Apify")
    args = parser.parse_args()
    if args.dry_run:
        # Keep invented sample records out of the real research database.
        database.DB_PATH = BASE_DIR / "data" / "dry_run_reviews.db"
        pipeline.collect_brand = lambda brand, domain, limit: [
            {**item, "reviewUrl": item["reviewUrl"].replace("sample", brand.lower().replace(" ", "-"))}
            for item in SAMPLE
        ]
    result = run_pipeline([name.strip() for name in args.brands.split(",") if name.strip()],
                          max_reviews_per_brand=args.limit, refresh=args.refresh or args.dry_run)
    print("Brand status:")
    for brand, status in result["brand_status"].items():
        print(f"  {brand}: {status}")
    print(f"New database records this run: {result['stats'].get('reviews_new_this_run', 0)}")
    print(f"Final unique reviews: {result['stats'].get('final_unique', 0)}")
    if result["errors"]:
        print("Messages:")
        for error in result["errors"]:
            print(f"  {error}")
    print(f"Excel: {result['excel_path'] or 'not created'}")


if __name__ == "__main__":
    main()
