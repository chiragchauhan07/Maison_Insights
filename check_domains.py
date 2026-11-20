"""Verify likely Trustpilot company domains with an explicitly confirmed run."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from collector import collect_brand, load_config
from utils import BASE_DIR


def run_domain_check(force=False, collector=None, output_path=None):
    """Check unchecked brand domains and save the findings outside the config."""
    collector = collector or collect_brand
    output_path = Path(output_path or BASE_DIR / "data" / "domain_check.json")
    config = load_config()
    previous = {}
    if output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    results = dict(previous)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for brand, info in config["brands"].items():
        if brand in results and not force:
            continue
        try:
            items = collector(brand, info["domain"], 1)
            collection_error = getattr(collector, "last_error", "")
            results[brand] = {
                "domain": info["domain"], "verified": bool(items) and not collection_error,
                "reviews_found": len(items), "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if collection_error:
                results[brand]["error"] = collection_error
        except Exception as error:
            results[brand] = {
                "domain": info["domain"], "verified": False,
                "reviews_found": 0, "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": str(error),
            }
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def main():
    parser = argparse.ArgumentParser(description="Check configured Trustpilot company domains")
    parser.add_argument("--yes", action="store_true", help="Confirm the estimated Apify spend")
    parser.add_argument("--force", action="store_true", help="Recheck previously checked brands")
    args = parser.parse_args()
    config = load_config()
    unchecked = [brand for brand in config["brands"] if args.force or not _already_checked(brand)]
    estimate = len(unchecked) * 0.01
    print(f"Estimated cost: about ${estimate:.2f} for {len(unchecked)} brand(s) (about $0.01 per brand).")
    if not args.yes:
        print("No checks started. Re-run with --yes to proceed.")
        return 0
    result = run_domain_check(force=args.force)
    print(f"Saved domain results for {len(result)} brand(s) to data/domain_check.json")
    return 0


def _already_checked(brand):
    path = BASE_DIR / "data" / "domain_check.json"
    if not path.exists():
        return False
    try:
        return brand in json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
