"""Create the six-sheet Excel report and optional charts."""
from datetime import datetime
import pandas as pd
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter
from utils import BASE_DIR
from collector import SCHEMA_COLUMNS
from database import get_collection_log
from sentiment import get_themes


def export_workbook(clean_df, sentiment_df, stats, path=None):
    """Write cleaned data, analysis and collection history to Excel."""
    output_dir = BASE_DIR / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = path or output_dir / f"luxury_sentiment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    mismatch_count = int(stats.get("rating_sentiment_mismatch_count",
                                  sentiment_df.get("Rating_Sentiment_Mismatch", pd.Series(dtype=bool)).sum()))
    mismatch_rate = float(stats.get("rating_sentiment_mismatch_rate",
                                   mismatch_count / len(sentiment_df) if len(sentiment_df) else 0.0))
    summary = [{"Metric": "Total collected", "Value": stats.get("total_collected", 0)},
               {"Metric": "Duplicates removed", "Value": stats.get("duplicates_removed", 0)},
               {"Metric": "Invalid removed", "Value": stats.get("invalid_removed", 0)},
               {"Metric": "Final unique", "Value": stats.get("final_unique", 0)},
               {"Metric": "Rating / sentiment mismatches", "Value": mismatch_count},
               {"Metric": "Rating / sentiment mismatch rate", "Value": mismatch_rate}]
    summary += [{"Metric": f"Platform: {key}", "Value": value} for key, value in stats.get("per_platform", {}).items()]
    summary += [{"Metric": f"Brand: {key}", "Value": value} for key, value in stats.get("per_brand", {}).items()]
    sentiment_report = _sentiment_report(sentiment_df)
    themes = get_themes(sentiment_df)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        clean_df.reindex(columns=SCHEMA_COLUMNS).to_excel(writer, sheet_name="Cleaned Data", index=False)
        extras = ["Review_Title", "Sentiment_Score", "Sentiment_Label", "Is_Valid", "Flags",
                  "Rating_Verified", "Rating_Sentiment_Mismatch"]
        sentiment_df.reindex(columns=SCHEMA_COLUMNS + extras).to_excel(writer, sheet_name="Sentiment Data", index=False)
        pd.DataFrame(summary).to_excel(writer, sheet_name="Summary", index=False)
        sentiment_report.to_excel(writer, sheet_name="Sentiment Report", index=False)
        theme_rows = ([{"Section": "Positive terms", "Name": name, "Count": count} for name, count in themes["positive_terms"]] +
                      [{"Section": "Negative terms", "Name": name, "Count": count} for name, count in themes["negative_terms"]] +
                      [{"Section": "Preset themes", "Name": name, "Count": count} for name, count in themes["preset_theme_counts"].items()] +
                      [{"Section": "Technology sentiment", "Name": name, "Count": count} for name, count in themes["technology_sentiment"].items()])
        pd.DataFrame(theme_rows, columns=["Section", "Name", "Count"]).to_excel(writer, sheet_name="Themes", index=False)
        get_collection_log().to_excel(writer, sheet_name="Collection Log", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            for column in sheet.columns:
                letter = get_column_letter(column[0].column)
                width = min(60, max(12, max((len(str(cell.value or "")) for cell in column[:100]), default=10) + 2))
                sheet.column_dimensions[letter].width = width
            if sheet.title in ("Cleaned Data", "Sentiment Data"):
                review_col = next((cell.column for cell in sheet[1] if cell.value == "Review_Text"), None)
                if review_col:
                    for cells in sheet.iter_cols(min_col=review_col, max_col=review_col, min_row=2):
                        for cell in cells:
                            cell.alignment = Alignment(wrap_text=True, vertical="top")
    return str(path)


def _sentiment_report(df):
    if df.empty or "Sentiment_Label" not in df:
        return pd.DataFrame(columns=["Group", "Name", "Sentiment", "Count", "Percent"])
    rows = []
    for group, column in [("Overall", None), ("Platform", "Platform"), ("Brand", "Brand"), ("Category", "Category")]:
        if column is None:
            groups = [("All", df)]
        else:
            groups = list(df.groupby(column, dropna=False))
        for name, part in groups:
            counts = part["Sentiment_Label"].value_counts()
            for label in ["Positive", "Neutral", "Negative"]:
                count = int(counts.get(label, 0))
                rows.append({"Group": group, "Name": name, "Sentiment": label, "Count": count,
                             "Percent": round(count / len(part) * 100, 2) if len(part) else 0})
    return pd.DataFrame(rows)
