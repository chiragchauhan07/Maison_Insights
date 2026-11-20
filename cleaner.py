"""Validate reviews, remove duplicates and standardise metadata."""
import yaml
import pandas as pd
from rapidfuzz import fuzz
from langdetect import detect, DetectorFactory
from collector import SCHEMA_COLUMNS
from utils import BASE_DIR, parse_date

DetectorFactory.seed = 0


def clean_reviews(df):
    """Return usable unique reviews and simple collection counters."""
    config = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
    settings = config["settings"]
    aliases = config.get("aliases", {})
    total = len(df)
    data = df.copy()
    for column in SCHEMA_COLUMNS:
        if column not in data:
            data[column] = "Not stated"
    for column in ["Flags", "Sentiment_Label", "Review_Title", "Rating_Verified"]:
        if column not in data:
            data[column] = ""
    data["Flags"] = data["Flags"].fillna("").astype(str)
    data["Rating"] = data["Rating"].map(_rating_value)
    data["Brand"] = data["Brand"].replace(aliases)
    data["Review_Date"] = data["Review_Date"].map(parse_date)
    exact_before = len(data)
    data = data.drop_duplicates(subset=["Review_ID"], keep="first")
    data = data.drop_duplicates(subset=["Brand", "Review_Text"], keep="first")
    duplicates_removed = exact_before - len(data)
    near = 0
    kept = []
    for idx, row in data.iterrows():
        normalized = str(row["Review_Text"]).strip().lower()
        possible = next((other for other in kept if row["Brand"] == other["Brand"] and
                         fuzz.ratio(normalized, str(other["Review_Text"]).strip().lower()) >= settings["near_duplicate_threshold"]), None)
        if possible is not None:
            data.at[idx, "Flags"] = _append_flag(data.at[idx, "Flags"], "near_duplicate")
            near += 1
        else:
            kept.append(row)
    data = data.drop(index=[idx for idx, row in data.iterrows() if "near_duplicate" in str(data.at[idx, "Flags"])])
    invalid = 0
    valid_rows = []
    for _, row in data.iterrows():
        text = str(row["Review_Text"]).strip()
        if text in ("", "Not stated", "None") or len(text) < settings["min_review_length"]:
            invalid += 1
            continue
        if row["Source_URL"] in ("", "Not stated", None):
            row["Flags"] = _append_flag(row["Flags"], "missing_source_url")
        for column, flag in [("Rating", "missing_rating"), ("Review_Date", "missing_date"), ("Country", "missing_country")]:
            if row[column] in ("", "Not stated", None):
                row["Flags"] = _append_flag(row["Flags"], flag)
        if row["Language"] in ("", "Not stated", None):
            try:
                row["Language"] = detect(text)
            except Exception:
                row["Language"] = "Not stated"
        row["Is_Valid"] = True
        valid_rows.append(row)
    clean = pd.DataFrame(valid_rows)
    if clean.empty:
        clean = pd.DataFrame(columns=SCHEMA_COLUMNS + ["Review_Title", "Rating_Verified", "Flags", "Is_Valid"])
    stats = {
        "total_collected": total, "duplicates_removed": duplicates_removed,
        "near_duplicates_flagged": near, "invalid_removed": invalid,
        "final_unique": len(clean), "per_platform": clean["Platform"].value_counts().to_dict(),
        "per_brand": clean["Brand"].value_counts().to_dict(),
    }
    return clean, stats


def _append_flag(current, flag):
    flags = [part for part in str(current or "").split(";") if part]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _rating_value(value):
    try:
        number = float(value)
        return int(number) if number.is_integer() and 1 <= number <= 5 else "Not stated"
    except (TypeError, ValueError, OverflowError):
        return "Not stated"
