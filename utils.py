"""Small shared helpers for the review pipeline."""
import hashlib
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
import os
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent


def get_apify_token():
    """Return the token from process variables, app .env, then its parent .env."""
    environment_token = os.environ.get("APIFY_TOKEN", "").strip()
    if environment_token:
        return environment_token
    for env_path in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
        values = dotenv_values(env_path)
        token = str(values.get("APIFY_TOKEN") or "").strip()
        if token:
            return token
    return None


def setup_logging():
    """Log to the collection file and console without exposing secrets."""
    logger = logging.getLogger("luxury_sentiment")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        if not (os.getenv("VERCEL") or os.getenv("DATABASE_URL")):
            log_dir = BASE_DIR / "logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(log_dir / "collection.log", encoding="utf-8")
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except OSError:
                pass
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
    return logger


def make_review_id(platform, url, text):
    """Create a stable ID from the platform, source URL and original text."""
    source = "|".join(str(value or "") for value in (platform, url, text))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def parse_date(value):
    """Return a date as YYYY-MM-DD, or Not stated when it cannot be parsed."""
    if value is None or str(value).strip() in ("", "Not stated"):
        return "Not stated"
    try:
        if isinstance(value, (datetime, date)):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.date().isoformat() if isinstance(parsed, datetime) else parsed.isoformat()
    except (TypeError, ValueError):
        return "Not stated"


def retry(fn, tries=3, logger=None):
    """Retry a function with short exponential delays, then re-raise."""
    logger = logger or logging.getLogger("luxury_sentiment")
    for attempt in range(tries):
        try:
            return fn()
        except Exception as error:
            if attempt == tries - 1:
                logger.error("Operation failed after %s tries (%s).", tries, type(error).__name__)
                raise
            delay = 2 ** attempt
            logger.warning("Attempt %s failed; retrying in %s second(s) (%s).", attempt + 1, delay, type(error).__name__)
            time.sleep(delay)
