"""Explainable sentiment scores and basic theme counts."""
import logging
import re
from collections import Counter
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import yaml
from utils import BASE_DIR

LOGGER = logging.getLogger("luxury_sentiment")
STOPWORDS = set("a an the and or but is are was were i we they it this that with for to of in on my our very so as have has had".split())
THEMES = {"quality": ["quality", "craftsmanship", "durable", "beautiful"], "price": ["price", "expensive", "cost", "value"],
          "delivery": ["delivery", "shipping", "arrived", "parcel"], "service": ["service", "staff", "helpful", "support"],
          "authenticity": ["authentic", "genuine", "fake", "original"], "packaging": ["packaging", "box", "wrapped"],
          "returns/refund": ["return", "refund", "exchange", "reimburse"]}


def score_multilingual(texts):
    """Score non-English text with an optional lazy-loaded Transformers model."""
    try:
        from transformers import pipeline
        classifier = pipeline("text-classification", model="cardiffnlp/twitter-xlm-roberta-base-sentiment")
        result = classifier(list(texts), truncation=True)
        mapped = []
        for item in result:
            label = item["label"].lower()
            label = "Positive" if "positive" in label else "Negative" if "negative" in label else "Neutral"
            mapped.append((float(item["score"]), label, ""))
        return mapped
    except Exception as error:
        LOGGER.warning("Multilingual model unavailable; neutral fallback used for %s records (%s)", len(texts), error)
        return [(0.0, "Neutral", "multilingual_model_not_available") for _ in texts]


def analyze_sentiment(df):
    """Score title plus review text; optionally reconcile conflicting labels with stars."""
    cfg = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))["settings"]
    vader = SentimentIntensityAnalyzer()
    data = df.copy()
    if "Flags" not in data:
        data["Flags"] = ""
    multilingual_positions = ([i for i, language in enumerate(data.get("Language", []))
                               if language not in ("en", "Not stated", "")]
                              if cfg.get("multilingual_model", False) else [])
    def score_text(row):
        parts = []
        for column in ("Review_Title", "Review_Text"):
            value = row.get(column, "")
            if pd.isna(value):
                continue
            value = str(value).strip()
            if value and value != "Not stated":
                parts.append(value)
        return " ".join(parts)
    scoring_texts = [score_text(row) for _, row in data.iterrows()]
    multilingual = score_multilingual([scoring_texts[pos] for pos in multilingual_positions]) if multilingual_positions else []
    result_by_position = dict(zip(multilingual_positions, multilingual))
    scores, labels = [], []
    for pos, (_, row) in enumerate(data.iterrows()):
        if pos in result_by_position:
            score, label, flag = result_by_position[pos]
            if flag:
                data.at[row.name, "Flags"] = ";".join(filter(None, [str(row.get("Flags", "")), flag]))
        else:
            score = vader.polarity_scores(scoring_texts[pos])["compound"]
            label = "Positive" if score >= cfg["vader_positive"] else "Negative" if score <= cfg["vader_negative"] else "Neutral"
        flags = [flag for flag in str(data.at[row.name, "Flags"] or "").split(";") if flag and flag not in {"rating_sentiment_mismatch", "label_from_rating"}]
        try:
            rating = int(float(row.get("Rating")))
        except (TypeError, ValueError, OverflowError):
            rating = None
        mismatch = (rating is not None and rating <= 2 and label == "Positive") or (rating is not None and rating >= 4 and label == "Negative")
        if mismatch:
            flags.append("rating_sentiment_mismatch")
            if cfg.get("sentiment_method", "vader") == "vader_rating_aware":
                label = "Negative" if rating <= 2 else "Positive"
                flags.append("label_from_rating")
        data.at[row.name, "Flags"] = ";".join(dict.fromkeys(flags))
        scores.append(score)
        labels.append(label)
    data["Sentiment_Score"] = scores
    data["Sentiment_Label"] = labels
    data["Rating_Sentiment_Mismatch"] = ["rating_sentiment_mismatch" in str(flags).split(";") for flags in data["Flags"]]
    return data


def get_themes(df):
    """Return frequent positive and negative terms, preset themes and tech summary."""
    def terms(part):
        words = re.findall(r"[a-zA-Z]{3,}", " ".join(part["Review_Text"].astype(str)).lower())
        filtered = [word for word in words if word not in STOPWORDS]
        counts = Counter(filtered)
        counts.update(f"{left} {right}" for left, right in zip(filtered, filtered[1:]))
        return counts.most_common(15)
    positive = df[df.get("Sentiment_Label", pd.Series(index=df.index, dtype=str)) == "Positive"]
    negative = df[df.get("Sentiment_Label", pd.Series(index=df.index, dtype=str)) == "Negative"]
    theme_counts = {name: int(df["Review_Text"].astype(str).str.lower().apply(lambda text: any(term in text for term in words)).sum()) for name, words in THEMES.items()}
    tech = df[df.get("Tech_Context", "Not stated") != "Not stated"] if "Tech_Context" in df else df.iloc[0:0]
    tech_summary = tech["Sentiment_Label"].value_counts().to_dict() if not tech.empty else {}
    return {"positive_terms": terms(positive) if not positive.empty else [], "negative_terms": terms(negative) if not negative.empty else [],
            "preset_theme_counts": theme_counts, "technology_sentiment": tech_summary}
