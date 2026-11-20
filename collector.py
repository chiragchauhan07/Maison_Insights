"""Collect and map public Trustpilot reviews through Apify."""
import re
import time
from datetime import date
from datetime import timedelta
import pandas as pd
import yaml
from apify_client import ApifyClient
from utils import BASE_DIR, get_apify_token, make_review_id, parse_date, retry, setup_logging

SCHEMA_COLUMNS = ["Review_ID", "Date_Collected", "Platform", "Source_URL", "Brand",
                  "Product_or_Campaign", "Category", "Tech_Context", "Review_Date",
                  "Review_Text", "Language", "Country", "Rating"]


def load_config():
    with (BASE_DIR / "config.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def build_actor_input(domain, limit):
    """Build the current actor input (companyUrls and maxReviewsPerCompany)."""
    return {"companyUrls": [domain], "maxReviewsPerCompany": int(limit)}


def collect_brand(brand, domain, limit):
    """Run the configured Apify actor; return an empty list on failure."""
    logger = setup_logging()
    collect_brand.last_error = ""
    config = load_config()["apify"]
    safe_limit = max(1, min(int(limit), config["max_reviews_per_brand"]))
    if limit > 200:
        logger.warning("Requested limit exceeds 200 for %s; applying configured cap", brand)
    token = get_apify_token()
    if not token:
        collect_brand.last_error = "APIFY_TOKEN is not configured."
        logger.error("APIFY_TOKEN is not configured.")
        return []

    def start_actor():
        client = ApifyClient(token)
        return client.actor(config["actor_id"]).call(
            run_input=build_actor_input(domain, safe_limit),
            run_timeout=timedelta(seconds=config["timeout_seconds"]),
        )

    try:
        run = retry(start_actor, logger=logger)
        if not run:
            raise RuntimeError("Apify returned no run information")
        # Read results outside retry so a read error cannot start a second charged run.
        client = ApifyClient(token)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        logger.info("Collected %s items for %s", len(items), brand)
        return items
    except Exception as error:
        collect_brand.last_error = "Apify collection failed."
        logger.error("Collection failed for %s (%s).", brand, type(error).__name__)
        return []


def collect_brand_live(brand, domain, limit, progress_callback=None, poll_seconds=2, sleep=time.sleep,
                       monotonic=time.monotonic, client_factory=ApifyClient):
    """Start one actor run, poll its real status, and read its dataset once on success."""
    config = load_config()["apify"]
    token = get_apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN is not configured")
    client = client_factory(token)
    run = client.actor(config["actor_id"]).start(
        run_input=build_actor_input(domain, min(max(1, int(limit)), config["max_reviews_per_brand"])),
        run_timeout=timedelta(seconds=config["timeout_seconds"]),
    )
    if not run:
        raise RuntimeError("Apify did not return a run ID")

    run_id = run.id
    started = monotonic()
    last_status = getattr(run, "status", "READY") or "READY"
    terminal = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}

    def report(status, item_count=None):
        if progress_callback:
            elapsed = int(monotonic() - started)
            count_text = f"; {item_count} dataset item(s) so far" if item_count is not None else ""
            progress_callback({
                "status": status, "elapsed_seconds": elapsed, "item_count": item_count,
                "message": f"Apify status {status}; elapsed {elapsed}s{count_text}.",
            })

    report(last_status)
    deadline = started + int(config["timeout_seconds"]) + 30
    while last_status not in terminal:
        if monotonic() >= deadline:
            raise RuntimeError("Apify run exceeded the configured wait time")
        sleep(max(0.1, poll_seconds))
        run = client.run(run_id).get()
        if not run:
            raise RuntimeError("Apify run status could not be read")
        last_status = getattr(run, "status", "UNKNOWN") or "UNKNOWN"
        item_count = None
        dataset_id = getattr(run, "default_dataset_id", None)
        if dataset_id:
            try:
                dataset = client.dataset(dataset_id).get()
                item_count = getattr(dataset, "item_count", None) if dataset else None
            except Exception:
                item_count = None
        report(last_status, item_count)

    if last_status != "SUCCEEDED":
        raise RuntimeError(f"Apify run ended with status {last_status}")
    dataset_id = getattr(run, "default_dataset_id", None)
    if not dataset_id:
        raise RuntimeError("Completed Apify run has no result dataset")
    # A dataset read failure is final; never start a second, chargeable run here.
    return list(client.dataset(dataset_id).iterate_items())


def find_tech_keyword(text, keywords=None):
    """Return the first whole-word technology keyword found, preserving its configured spelling."""
    keywords = keywords if keywords is not None else load_config()["settings"].get("tech_keywords", [])
    for keyword in keywords:
        phrase = r"\s+".join(re.escape(part) for part in keyword.split())
        if keyword == "AR":
            pattern = rf"(?<!\w){phrase}(?!\w)"
            match = re.search(pattern, text)
        else:
            match = re.search(rf"(?<!\w){phrase}(?!\w)", text, flags=re.IGNORECASE)
        if match:
            return keyword
    return "Not stated"


def map_to_schema(raw_items, brand, category, tech_keywords=None):
    """Map actor items into required columns and one title extra; discard author data."""
    tech_keywords = tech_keywords or load_config()["settings"].get("tech_keywords", [])
    rows = []
    for item in raw_items:
        text = item.get("text") or "Not stated"
        source_url = item.get("reviewUrl") or "Not stated"
        raw_rating = item.get("rating")
        try:
            rating = int(raw_rating) if raw_rating not in (None, "") and float(raw_rating).is_integer() else "Not stated"
            if rating != "Not stated" and not 1 <= rating <= 5:
                rating = "Not stated"
        except (TypeError, ValueError, OverflowError):
            rating = "Not stated"
        rows.append({
            "Review_ID": make_review_id("Trustpilot", source_url, text),
            "Date_Collected": date.today().isoformat(), "Platform": "Trustpilot",
            "Source_URL": source_url, "Brand": brand,
            "Product_or_Campaign": item.get("product") or item.get("productName") or "Not stated",
            "Category": category, "Tech_Context": find_tech_keyword(text, tech_keywords),
            "Review_Date": parse_date(item.get("publishedDate")), "Review_Text": text,
            "Language": item.get("language") or "Not stated",
            "Country": item.get("country") or "Not stated", "Rating": rating,
            "Review_Title": item.get("title") or "Not stated",
            "Rating_Verified": item.get("verificationLevel") or "Not stated",
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS + ["Review_Title", "Rating_Verified"])
