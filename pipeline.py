"""Run collection and prepare review analysis for Excel or the UI."""
from datetime import datetime, timedelta, timezone
import pandas as pd
from collector import collect_brand, load_config, map_to_schema
from cleaner import clean_reviews
from database import (count_reviews_for_brand, get_reviews, latest_collection, log_run,
                      save_reviews, update_sentiment)
from exporter import export_workbook
from sentiment import analyze_sentiment, get_themes
from utils import setup_logging


def run_pipeline(brands: list[str], max_reviews_per_brand: int | None = None,
                 refresh: bool = False, progress_callback=None, *, event_callback=None,
                 collector_function=None) -> dict:
    """Collect one brand at a time, reuse sufficient recent cache, analyze, and export.

    If provided, ``progress_callback`` receives one event dictionary with the keys
    ``step``, ``brand``, ``index``, ``total`` and ``message``. Steps are ``cache``,
    ``collect``, ``map``, ``save``, ``brand``, ``clean``, ``sentiment`` and ``export``.
    The optional ``event_callback`` receives progress events; ``collector_function``
    replaces only collection so the web runner can poll Apify or provide demo data.
    """
    logger = setup_logging()
    config = load_config()
    brand_config = config["brands"]
    aliases = config.get("aliases", {})
    requested = list(dict.fromkeys(aliases.get(name, name) for name in brands))
    requested_limit = max_reviews_per_brand if max_reviews_per_brand is not None else config["apify"]["max_reviews_per_brand"]
    limit = max(1, min(int(requested_limit), int(config["apify"]["max_reviews_per_brand"])))
    errors, statuses, total_new, collected = [], {}, 0, 0
    collector_function = collector_function or collect_brand
    mapped_by_brand, existing_ids = {}, {}
    completed_brands = set()

    def emit(stage, kind, brand="", message="", cached=False, counts=None, item=None):
        if kind == "brand_done" and brand:
            if brand in completed_brands:
                return
            completed_brands.add(brand)
        if not event_callback:
            return
        event = {"stage": stage, "type": kind, "brand": brand, "message": message,
                 "cached": cached, "counts": counts or {}}
        if item is not None:
            event["item"] = item
        try:
            event_callback(event)
        except Exception as error:
            logger.warning("Pipeline event callback failed at %s: %s", stage, error)

    def sample_items(frame, limit=25):
        if frame is None or frame.empty:
            return []
        count = min(len(frame), limit)
        indexes = [0] if count == 1 else sorted({round(i * (len(frame) - 1) / (count - 1)) for i in range(count)})
        return [frame.iloc[index] for index in indexes]

    def item_payload(row, reason=None):
        payload = {
            "id": str(row.get("Review_ID", ""))[:16], "rating": row.get("Rating"),
            "snippet": str(row.get("Review_Text", ""))[:90],
            "language": str(row.get("Language", "Not stated")),
            "country": str(row.get("Country", "Not stated")),
            "label": str(row.get("Sentiment_Label", "")),
            "score": row.get("Sentiment_Score"),
            "flags": [flag for flag in str(row.get("Flags", "") or "").split(";") if flag],
        }
        if reason:
            payload["reason"] = reason
        return payload

    def progress(step, brand, index, message):
        if progress_callback:
            event = {"step": step, "brand": brand, "index": index, "total": len(requested), "message": message}
            try:
                progress_callback(event)
            except Exception as error:
                logger.warning("Progress callback failed at %s for %s: %s", step, brand, error)

    for index, brand in enumerate(requested, start=1):
        if brand not in brand_config:
            message = f"Unknown brand: {brand}"
            errors.append(message)
            statuses[brand] = message
            progress("brand", brand, index, message)
            emit("order", "brand_done", brand, message)
            continue

        prior = latest_collection(brand)
        recent = False
        if prior and not refresh:
            try:
                finished = datetime.fromisoformat(prior["finished_at"])
                recent = finished >= datetime.now(timezone.utc) - timedelta(days=config["apify"]["cache_days"])
            except (TypeError, ValueError):
                recent = False
        enough_saved = count_reviews_for_brand(brand) >= limit
        if recent and (enough_saved or prior["status"] == "exhausted"):
            statuses[brand] = f"Reused recent cache ({count_reviews_for_brand(brand)} in database)"
            emit("fetch", "stage_start", brand, "Using recent reviews from the local cache.", cached=True)
            cached_rows = get_reviews([brand])
            for row in sample_items(cached_rows):
                emit("fetch", "item", brand, "Cached review ready.", cached=True, item=item_payload(row))
            emit("fetch", "stage_done", brand, statuses[brand], cached=True,
                 counts={"fetched": len(cached_rows)})
            emit("fetch", "brand_done", brand, statuses[brand], cached=True,
                 counts={"fetched": len(cached_rows)})
            progress("cache", brand, index, statuses[brand])
            continue

        info = brand_config[brand]
        started = datetime.now(timezone.utc).isoformat()
        try:
            emit("fetch", "stage_start", brand, f"Starting the Trustpilot order for {brand}.")
            if hasattr(collector_function, "last_error"):
                collector_function.last_error = ""
            raw = collector_function(brand, info["domain"], limit)
            collected += len(raw)
            progress("collect", brand, index, f"Fetched {len(raw)} review(s)")
            collection_error = getattr(collector_function, "last_error", "")
            if collection_error:
                errors.append(f"{brand}: {collection_error}")
                statuses[brand] = f"failed: {collection_error}"
                log_run(brand, started, "failed", len(raw), 0, collection_error)
                emit("fetch", "warning", brand, "This brand could not be collected; the rest of the order will continue.")
                emit("fetch", "brand_done", brand, "Collection failed.")
                progress("brand", brand, index, statuses[brand])
                continue

            emit("fetch", "stage_done", brand, f"Received {len(raw)} review(s).", counts={"fetched": len(raw)})
            emit("prep", "stage_start", brand, "Mapping reviews into the research columns.")
            before = get_reviews([brand])
            existing_ids[brand] = set(before.get("Review_ID", pd.Series(dtype=str)).astype(str))
            mapped = map_to_schema(raw, brand, info["category"], config["settings"].get("tech_keywords"))
            mapped_by_brand[brand] = mapped
            for row in sample_items(mapped):
                emit("fetch", "item", brand, "A review ingredient arrived.", item=item_payload(row))
            for _, row in mapped.iterrows():
                if str(row["Review_ID"]) in existing_ids[brand]:
                    emit("prep", "item", brand, "This review already exists in the local collection.",
                         item=item_payload(row, reason="duplicate"))
            progress("map", brand, index, f"Mapped {len(mapped)} review(s)")
            counts = save_reviews(mapped)
            total_new += counts["new"]
            progress("save", brand, index, f"Saved {counts['new']} new review(s)")
            status = "exhausted" if len(raw) < limit else "success"
            message = f"No reviews found for {brand}" if not raw else f"Fetched {len(raw)}, added {counts['new']}"
            statuses[brand] = f"{status}: {message}"
            log_run(brand, started, status, len(raw), counts["new"], message if not raw else "")
            emit("prep", "stage_done", brand, "Reviews saved for cleaning and validation.",
                 counts={"fetched": len(raw), "duplicates": counts["ignored"]})
            emit("prep", "brand_done", brand, statuses[brand],
                 counts={"fetched": len(raw), "duplicates": counts["ignored"]})
            progress("brand", brand, index, statuses[brand])
        except Exception as error:
            message = f"{brand}: collection or preparation failed."
            errors.append(message)
            statuses[brand] = "failed: collection or preparation failed."
            log_run(brand, started, "failed", error_message="Collection or preparation failed.")
            logger.error("Brand processing failed for %s (%s).", brand, type(error).__name__)
            emit("prep", "warning", brand, "This brand could not be prepared; the rest of the order will continue.")
            emit("prep", "brand_done", brand, "Preparation failed.")
            progress("brand", brand, index, statuses[brand])

    emit("prep", "stage_start", "", "Removing duplicates and checking review text.")
    db_df = get_reviews(requested)
    cleaned, stats = clean_reviews(db_df)
    clean_ids = set(cleaned.get("Review_ID", pd.Series(dtype=str)).astype(str))
    minimum = int(config["settings"]["min_review_length"])
    for brand, mapped in mapped_by_brand.items():
        removed = []
        seen_text = set()
        for _, row in mapped.iterrows():
            text = str(row.get("Review_Text", "")).strip()
            review_id = str(row.get("Review_ID", ""))
            reason = None
            if not text or text == "Not stated":
                reason = "missing text"
            elif len(text) < minimum:
                reason = "too short"
            elif review_id in existing_ids.get(brand, set()) or text.lower() in seen_text:
                reason = "duplicate"
            elif review_id not in clean_ids:
                reason = "duplicate"
            if text:
                seen_text.add(text.lower())
            if reason:
                removed.append((row, reason))
        if len(removed) > 25:
            positions = [round(i * (len(removed) - 1) / 24) for i in range(25)]
            removed = [removed[position] for position in positions]
        for row, reason in removed:
            emit("prep", "item", brand, f"Removed review: {reason}.", item=item_payload(row, reason=reason))
    for brand in requested:
        kept_rows = cleaned[cleaned["Brand"] == brand] if not cleaned.empty else cleaned
        for row in sample_items(kept_rows):
            emit("prep", "item", brand, "Review passed cleaning and validation.", item=item_payload(row))
    emit("prep", "stage_done", "", "Cleaning and validation are complete.",
         counts={"fetched": stats["total_collected"], "duplicates": stats["duplicates_removed"],
                 "invalid": stats["invalid_removed"], "scored": 0})
    progress("clean", "", len(requested), f"Kept {len(cleaned)} unique review(s)")
    stats["total_collected_this_run"] = collected
    stats["reviews_new_this_run"] = total_new
    emit("cook", "stage_start", "", "Scoring review sentiment.",
         counts={"fetched": stats["total_collected"], "duplicates": stats["duplicates_removed"],
                 "invalid": stats["invalid_removed"]})
    scored = analyze_sentiment(cleaned)
    mismatch_count = int(scored.get("Rating_Sentiment_Mismatch", pd.Series(dtype=bool)).sum())
    stats["rating_sentiment_mismatch_count"] = mismatch_count
    stats["rating_sentiment_mismatch_rate"] = round(mismatch_count / len(scored), 4) if len(scored) else 0.0
    for brand in requested:
        brand_rows = scored[scored["Brand"] == brand] if not scored.empty else scored
        for _, row in brand_rows.iterrows():
            payload = item_payload(row)
            payload["label"] = str(row["Sentiment_Label"])
            payload["score"] = float(row["Sentiment_Score"])
            emit("cook", "item", brand, f"Review scored {payload['label']}.", item=payload)
    emit("cook", "stage_done", "", "Sentiment scoring is complete.",
         counts={"fetched": stats["total_collected"], "duplicates": stats["duplicates_removed"],
                 "invalid": stats["invalid_removed"], "scored": len(scored)})
    progress("sentiment", "", len(requested), f"Scored {len(scored)} review(s)")
    update_sentiment(scored)
    emit("plate", "stage_start", "", "Building charts and summary tables.")
    charts = analysis_for_ui(requested)
    plate_data = {name: frame.to_dict("records") for name, frame in charts.items()
                  if name in {"overall", "by_brand"} and isinstance(frame, pd.DataFrame)}
    emit("plate", "stage_done", "", "Summary charts are ready.", counts={"scored": len(scored)}, item=plate_data)
    emit("serve", "stage_start", "", "Preparing the Excel workbook.")
    try:
        excel_path = export_workbook(cleaned, scored, stats)
        emit("serve", "stage_done", "", "The workbook is ready to download.")
        progress("export", "", len(requested), "Excel workbook created")
    except Exception as error:
        errors.append(f"Excel export failed: {error}")
        excel_path = ""
    return {"excel_path": excel_path, "stats": stats, "brand_status": statuses,
            "errors": errors, "charts": charts}


def analysis_for_ui(brands=None):
    """Return review and chart DataFrames so the UI never needs to read Excel."""
    config = load_config()
    aliases = config.get("aliases", {})
    selected = [aliases.get(brand, brand) for brand in brands] if brands else None
    all_reviews = get_reviews(selected)
    cleaned, _ = clean_reviews(all_reviews)
    scored = analyze_sentiment(cleaned) if not cleaned.empty else cleaned.copy()
    if not all_reviews.empty and not scored.empty:
        scored_by_id = scored.set_index("Review_ID")
        for column in ("Sentiment_Score", "Sentiment_Label"):
            if column not in all_reviews:
                all_reviews[column] = None
            mapped = all_reviews["Review_ID"].map(scored_by_id[column])
            all_reviews[column] = all_reviews[column].where(all_reviews[column].notna() & (all_reviews[column] != ""), mapped)

    def breakdown(group_columns):
        if scored.empty:
            return pd.DataFrame(columns=group_columns + ["Sentiment_Label", "Count", "Percent"])
        keys = group_columns + ["Sentiment_Label"]
        result = scored.groupby(keys, dropna=False).size().rename("Count").reset_index()
        if not group_columns:
            denominator = pd.DataFrame({"Total": [len(scored)]})
            result["Total"] = len(scored)
            result["Percent"] = (result["Count"] / result["Total"] * 100).round(2)
            return result.drop(columns="Total")
        denominator = scored.groupby(group_columns, dropna=False).size().rename("Total").reset_index()
        result = result.merge(denominator, on=group_columns, how="left")
        result["Percent"] = (result["Count"] / result["Total"] * 100).round(2)
        return result.drop(columns="Total")

    overall = breakdown([])
    by_brand = breakdown(["Brand"])
    by_category = breakdown(["Category"])
    by_platform = breakdown(["Platform"])
    rating_vs_sentiment = (
        scored.groupby(["Rating", "Sentiment_Label"], dropna=False).size().rename("Count").reset_index()
        if not scored.empty else pd.DataFrame(columns=["Rating", "Sentiment_Label", "Count"])
    )
    themes_result = get_themes(scored)
    theme_rows = ([{"Section": "Positive terms", "Name": name, "Count": count} for name, count in themes_result["positive_terms"]] +
                  [{"Section": "Negative terms", "Name": name, "Count": count} for name, count in themes_result["negative_terms"]] +
                  [{"Section": "Preset themes", "Name": name, "Count": count} for name, count in themes_result["preset_theme_counts"].items()])
    themes = pd.DataFrame(theme_rows, columns=["Section", "Name", "Count"])
    tech_sentiment = pd.DataFrame(
        [{"Sentiment_Label": label, "Count": count} for label, count in themes_result["technology_sentiment"].items()],
        columns=["Sentiment_Label", "Count"],
    )
    if not scored.empty and "Review_Date" in scored:
        dated = scored[scored["Review_Date"].astype(str).str.match(r"^\d{4}-\d{2}")].copy()
        dated["Month"] = dated["Review_Date"].str[:7]
        review_volume = dated.groupby("Month").size().rename("Reviews").reset_index()
    else:
        review_volume = pd.DataFrame(columns=["Month", "Reviews"])
    return {
        "reviews": all_reviews, "analysis_reviews": scored,
        "overall": overall, "by_brand": by_brand, "by_category": by_category,
        "by_platform": by_platform, "rating_vs_sentiment": rating_vs_sentiment,
        "review_volume": review_volume, "themes": themes,
        "tech_sentiment": tech_sentiment,
    }
