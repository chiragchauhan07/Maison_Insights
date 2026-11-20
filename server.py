"""FastAPI endpoints, FIFO job runner, and static research dashboard."""
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, RLock, Thread
from tempfile import TemporaryDirectory
import hashlib
import hmac
import json
import os
import shutil
import time
import uuid

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import database
from collector import collect_brand_live, load_config
from database import (add_daily_spend, count_ip_jobs, get_daily_spend, get_overview_stats,
                      hash_client_ip, record_ip_job, latest_collection, count_reviews_for_brand)
from pipeline import run_pipeline
from utils import BASE_DIR, get_apify_token, setup_logging

WEB_DIR = BASE_DIR / "web"
logger = setup_logging()
app = FastAPI(title="Luxury Review Intelligence", docs_url=None, redoc_url=None)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


@app.middleware("http")
async def security_headers_and_size_limit(request: Request, call_next):
    length = request.headers.get("content-length", "0")
    try:
        too_large = int(length) > 32_768
    except ValueError:
        too_large = True
    if too_large:
        response = JSONResponse({"detail": "Request body is too large."}, status_code=413)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


class JobRequest(BaseModel):
    brands: list[str] = Field(min_length=1, max_length=36)
    per_brand: int = Field(ge=1, le=100)
    refresh: bool = False
    demo: bool = False

    @field_validator("brands")
    @classmethod
    def validate_brand_names(cls, brands):
        if any(not brand.strip() or len(brand) > 80 for brand in brands):
            raise ValueError("Brand names must be non-empty and no longer than 80 characters.")
        return brands


def _config():
    return load_config()


def _env_int(name, default):
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _daily_budget():
    try:
        return max(0.0, float(os.getenv("DAILY_BUDGET_USD", _config()["server"]["daily_budget_usd"])))
    except (TypeError, ValueError):
        return float(_config()["server"]["daily_budget_usd"])


def _demo_enabled():
    return os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _vercel_queue_enabled():
    return os.getenv("VERCEL") == "1" and os.getenv("VERCEL_ENV") in {"production", "preview"}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _to_records(frame):
    """Convert a DataFrame to JSON-safe records without carrying author fields."""
    if frame is None or frame.empty:
        return []
    safe = frame.drop(columns=[column for column in frame.columns
                               if any(term in column.lower() for term in ("author", "profile", "reviewer"))], errors="ignore")
    return json.loads(safe.to_json(orient="records", date_format="iso", force_ascii=False))


def _is_cached(brand, limit, refresh=False):
    if refresh:
        return False
    config = _config()
    previous = latest_collection(brand)
    if not previous:
        return False
    try:
        recent = datetime.fromisoformat(previous["finished_at"]) >= datetime.now(timezone.utc) - timedelta(days=config["apify"]["cache_days"])
    except (TypeError, ValueError):
        return False
    return recent and (count_reviews_for_brand(brand) >= limit or previous["status"] == "exhausted")


class JobManager:
    """Keep job state, replayable events, queue order, and one active pipeline."""
    def __init__(self):
        self.jobs = {}
        self.queue = deque()
        self.active_job = None
        self.lock = RLock()
        self.queue_changed = Condition(self.lock)

    def queue_length(self):
        if database._postgres_configured():
            return database.count_open_jobs()
        with self.lock:
            return len(self.queue) + (1 if self.active_job else 0)

    def reserved_cost(self):
        if database._postgres_configured():
            return sum(database.get_reserved_costs())
        with self.lock:
            return sum(job["estimated_cost_usd"] for job in self.jobs.values()
                       if job["status"] in {"queued", "running"} and not job["demo"])

    def daily_budget_left(self):
        return max(0.0, round(_daily_budget() - get_daily_spend() - self.reserved_cost(), 4))

    def get_job(self, job_id, refresh=False):
        job = self.jobs.get(job_id)
        if database._postgres_configured() and (job is None or refresh):
            job = database.load_job(job_id)
            if job is not None:
                job["event_condition"] = Condition(RLock())
                self.jobs[job_id] = job
        return job

    def emit(self, job_id, stage, kind, brand="", message="", cached=False, counts=None, item=None):
        job = self.get_job(job_id)
        with job["event_condition"]:
            event = {
                "seq": len(job["events"]) + 1, "ts": _utc_now(), "stage": stage, "type": kind,
                "brand": brand, "message": message, "cached": bool(cached), "counts": counts or {},
            }
            if item is not None:
                event["item"] = item
            if database._postgres_configured():
                database.append_job_event(job_id, event)
            job["events"].append(event)
            job["event_condition"].notify_all()
            return event

    async def create(self, payload, requester_ip, admin=False, force_demo=False):
        config = _config()
        catalog = config["brands"]
        aliases = config.get("aliases", {})
        canonical = list(dict.fromkeys(aliases.get(name, name) for name in payload.brands))
        unknown = [name for name in canonical if name not in catalog]
        if unknown:
            raise HTTPException(400, "One or more selected brands are not configured.")
        server_cfg = config.get("server", {})
        max_brands = _env_int("MAX_BRANDS_PER_JOB", server_cfg.get("max_brands_per_job", 6))
        max_per_brand = min(_env_int("MAX_PER_BRAND", server_cfg.get("max_per_brand", 100)),
                            int(config["apify"]["max_reviews_per_brand"]))
        if len(canonical) > max_brands and not admin:
            raise HTTPException(400, f"Choose no more than {max_brands} brands per order.")
        if payload.per_brand > max_per_brand:
            raise HTTPException(400, f"Choose no more than {max_per_brand} reviews per brand.")

        demo = bool(force_demo or payload.demo or _demo_enabled())
        if _vercel_queue_enabled() and not database._postgres_configured():
            raise HTTPException(503, "Postgres storage is not connected to this Vercel project yet.")
        cached = {brand for brand in canonical if _is_cached(brand, payload.per_brand, payload.refresh)}
        uncached = [brand for brand in canonical if brand not in cached]
        if not demo and uncached and not get_apify_token():
            raise HTTPException(503, "Apify is not configured. Add APIFY_TOKEN to the environment or .env, or use demo mode.")

        cost = 0.0 if demo else (len(uncached) * 0.005 + len(uncached) * payload.per_brand * 0.001)
        ip_hash = hash_client_ip(requester_ip)
        if not admin:
            hourly_limit = _env_int("MAX_JOBS_PER_IP_PER_HOUR", server_cfg.get("max_jobs_per_ip_per_hour", 5))
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            if count_ip_jobs(ip_hash, since) >= hourly_limit:
                raise HTTPException(429, "You have reached the hourly order limit. Please try again later.")

        with self.queue_changed:
            left = max(0.0, _daily_budget() - get_daily_spend() - self.reserved_cost())
            if cost > left and not admin:
                raise HTTPException(402, f"This order is estimated at ${cost:.2f}, above today's remaining Apify budget of ${left:.2f}.")
            if self.queue_length() >= 50 and not admin:
                raise HTTPException(503, "The analysis queue is full. Please try again later.")
            job_id = uuid.uuid4().hex
            position = self.queue_length() + 1
            event_condition = Condition(RLock())
            job = {
                "id": job_id, "brands": canonical, "per_brand": payload.per_brand,
                "refresh": payload.refresh, "demo": demo, "cached_brands": cached,
                "status": "queued", "events": [], "event_condition": event_condition,
                "estimated_cost_usd": cost, "queue_position": position,
                "created_at": _utc_now(), "finished": False, "result": None,
                "reviews": [], "excel_path": "", "billable_runs": 0,
            }
            self.jobs[job_id] = job
            if database._postgres_configured():
                database.save_job(job)
            if not _vercel_queue_enabled():
                self.queue.append(job_id)
            if not admin:
                record_ip_job(ip_hash, job_id)
            self.emit(job_id, "order", "stage_start", message=f"Order accepted for {len(canonical)} brand(s).")
            self.emit(job_id, "order", "progress", message=f"Estimated cost ${cost:.2f}; queue position {position}.",
                      counts={"fetched": 0, "queue_position": position})
            self.emit(job_id, "order", "stage_done", message="Analysis request accepted.")
            if position > 1:
                self.emit(job_id, "order", "progress", message=f"You're #{position} in line.")
            if _vercel_queue_enabled():
                try:
                    from vercel.queue import QueueClient
                    queue = QueueClient(region=os.getenv("QUEUE_REGION", "bom1"))
                    await queue.send("luxury-review-jobs", {"job_id": job_id},
                                     retention=timedelta(days=7), idempotency_key=job_id)
                except Exception as error:
                    logger.error("Could not queue job %s (%s).", job_id[:8], type(error).__name__)
                    job["status"] = "failed"
                    job["result"] = {"stats": {}, "brand_status": {}, "errors": ["The analysis could not be queued. Please try again later."], "charts": {}, "sample_reviews": [], "demo": bool(job["demo"])}
                    job["finished"] = True
                    database.update_job(job_id, status="failed", result=job["result"], finished=True)
                    self.emit(job_id, "serve", "error", message="The analysis could not be queued. Please try again later.")
                    raise HTTPException(503, "The analysis queue is unavailable. Please try again later.") from error
            else:
                thread = Thread(target=self._run_job, args=(job_id,), daemon=True, name=f"review-job-{job_id[:8]}")
                thread.start()
            return {"job_id": job_id, "estimated_cost_usd": round(cost, 4),
                    "estimated_seconds": 2 if demo else max(2, 70 * len(uncached) + 2 * len(cached)),
                    "queue_position": position}

    def _demo_collector(self, job_id, brand, domain, limit):
        examples = [
            {"rating": 5, "title": "Beautiful quality", "text": "Beautiful quality and excellent craftsmanship. I love it.",
             "publishedDate": "2026-05-12T12:00:00Z", "country": "GB", "language": "en", "verificationLevel": "verified"},
            {"rating": 2, "title": "Slow delivery", "text": "The delivery was slow and customer service did not help.",
             "publishedDate": "2026-05-19T12:00:00Z", "country": "US", "language": "en", "verificationLevel": "verified"},
            {"rating": 4, "title": "Good experience", "text": "A lovely purchase and a very pleasant experience.",
             "publishedDate": "2026-06-03T12:00:00Z", "country": "FR", "language": "fr", "verificationLevel": "verified"},
            {"rating": 1, "title": "Disappointing", "text": "Very disappointing service; the refund took too long.",
             "publishedDate": "2026-06-08T12:00:00Z", "country": "CA", "language": "en", "verificationLevel": "verified"},
        ]
        self.emit(job_id, "fetch", "progress", brand, "Sample review collection started.", counts={"fetched": 0})
        for tick in range(3):
            time.sleep(float(os.getenv("DEMO_STEP_DELAY_SECONDS", "0.35")))
            self.emit(job_id, "fetch", "progress", brand,
                      ["Preparing sample reviews…", "Collecting sample reviews…", "Mapping sample review data…"][tick],
                      counts={"fetched": min(len(examples), limit) * (tick + 1) // 3})
        rows = []
        for index, example in enumerate(examples[:limit], 1):
            rows.append({**example, "reviewId": f"demo-{brand}-{index}",
                         "reviewUrl": f"https://www.trustpilot.com/reviews/demo-{brand.lower().replace(' ', '-')}-{index}"})
        return rows

    def process_queued_job(self, job_id):
        """Load a durable Vercel queue item and run it in the daemon process."""
        job = database.load_job(job_id)
        if not job:
            logger.warning("Queue message refers to missing job %s.", str(job_id)[:8])
            return
        job["event_condition"] = Condition(RLock())
        self.jobs[job_id] = job
        self._run_job(job_id, claimed=True)

    def _run_job(self, job_id, claimed=False):
        job = self.get_job(job_id)
        if not claimed:
            with self.queue_changed:
                while not self.queue or self.queue[0] != job_id or self.active_job is not None:
                    self.queue_changed.wait()
                self.queue.popleft()
                self.active_job = job_id
                job["status"] = "running"
                self.queue_changed.notify_all()
        else:
            with self.queue_changed:
                self.active_job = job_id
                job["status"] = "running"
        if database._postgres_configured():
            database.update_job(job_id, status="running")
        self.emit(job_id, "order", "progress", message="Your analysis has entered the collection queue.")
        try:
            def collection_function(brand, domain, limit):
                if job["demo"]:
                    return self._demo_collector(job_id, brand, domain, limit)

                started = [False]
                def poll_event(info):
                    if not started[0]:
                        job["billable_runs"] += 1
                        if database._postgres_configured():
                            database.update_job(job_id, billable_runs=job["billable_runs"])
                        started[0] = True
                    self.emit(job_id, "fetch", "progress", brand, info.get("message", "Apify is working."),
                              counts={"fetched": info.get("item_count") or 0,
                                      "elapsed_seconds": info.get("elapsed_seconds", 0)})

                return collect_brand_live(brand, domain, limit, progress_callback=poll_event)

            def pipeline_event(event):
                self.emit(job_id, event["stage"], event["type"], event.get("brand", ""),
                          event.get("message", ""), event.get("cached", False),
                          event.get("counts"), event.get("item"))
                if job["demo"] and event["type"] == "stage_start":
                    time.sleep(float(os.getenv("DEMO_STAGE_DELAY_SECONDS", "0.45")))

            if job["demo"]:
                with TemporaryDirectory(prefix="luxury-demo-") as temporary_dir:
                    with database.use_database(Path(temporary_dir) / "reviews.db"):
                        result = run_pipeline(job["brands"], job["per_brand"], job["refresh"],
                                              event_callback=pipeline_event,
                                              collector_function=collection_function)
            else:
                result = run_pipeline(job["brands"], job["per_brand"], job["refresh"],
                                      event_callback=pipeline_event,
                                      collector_function=collection_function)
            charts = result.pop("charts", {})
            chart_json = {name: _to_records(frame) for name, frame in charts.items() if isinstance(frame, pd.DataFrame)}
            review_rows = chart_json.get("reviews", [])
            for row in review_rows:
                for key in list(row):
                    if any(term in key.lower() for term in ("author", "profile", "reviewer")):
                        row.pop(key, None)
            job["reviews"] = review_rows
            job["excel_path"] = result.get("excel_path", "")
            if database._postgres_configured() and job["excel_path"]:
                if not os.getenv("BLOB_READ_WRITE_TOKEN"):
                    raise RuntimeError("Vercel Blob read/write token is not configured")
                from vercel import blob
                workbook = Path(job["excel_path"])
                uploaded = __import__("asyncio").run(blob.put_blob(
                    f"analysis-reports/{job_id}.xlsx", workbook.read_bytes(), access="private",
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ttl_days=30,
                ))
                job["excel_path"] = uploaded.url
            raw_status = result.get("brand_status", {})
            safe_status = {brand: ("Collection failed; see the server log." if str(message).startswith("failed:") else message)
                           for brand, message in raw_status.items()}
            safe_errors = ["One or more brands could not be completed. See the server log for details." for _ in result.get("errors", [])]
            job["result"] = {
                "stats": result.get("stats", {}), "brand_status": safe_status, "demo": bool(job["demo"]),
                "errors": safe_errors, "charts": {name: records for name, records in chart_json.items() if name not in {"reviews", "analysis_reviews"}},
                "sample_reviews": review_rows[:10],
            }
            if database._postgres_configured():
                database.update_job(job_id, reviews=review_rows, excel_path=job["excel_path"], result=job["result"])
            if not job["demo"]:
                collected = int(result.get("stats", {}).get("total_collected_this_run", 0))
                actual_estimate = job["billable_runs"] * 0.005 + collected * 0.001
                add_daily_spend(actual_estimate)
            self.emit(job_id, "serve", "done", message="Analysis complete. Your workbook is ready.",
                      counts={"fetched": int(result.get("stats", {}).get("total_collected_this_run", 0)),
                              "scored": int(result.get("stats", {}).get("final_unique", 0))})
            job["status"] = "done"
            if database._postgres_configured():
                database.update_job(job_id, status="done")
        except Exception as error:
            logger.error("Job %s failed (%s).", job_id[:8], type(error).__name__)
            job["status"] = "failed"
            job["result"] = {"stats": {}, "brand_status": {}, "errors": ["The analysis could not finish. Please try again later."], "charts": {}, "sample_reviews": [], "demo": bool(job["demo"])}
            if database._postgres_configured():
                database.update_job(job_id, status="failed", result=job["result"])
            self.emit(job_id, "serve", "error", message="The analysis could not finish. Please try again later.")
        finally:
            with job["event_condition"]:
                job["finished"] = True
                if database._postgres_configured():
                    database.update_job(job_id, finished=True)
                job["event_condition"].notify_all()
            with self.queue_changed:
                self.active_job = None
                self.queue_changed.notify_all()


manager = JobManager()


@app.on_event("startup")
def seed_database_if_available():
    """Copy an optional ignored seed database only when the local cache is absent."""
    if os.getenv("VERCEL") == "1" and not os.getenv("DATABASE_URL"):
        logger.warning("Vercel Postgres is not connected yet; API data routes will remain unavailable.")
        return
    if os.getenv("VERCEL") == "1" or database._postgres_configured():
        database.init_db()
        return
    database_path = BASE_DIR / "data" / "reviews.db"
    seed_path = BASE_DIR / "seed" / "reviews_seed.db"
    if not database_path.exists() and seed_path.is_file():
        database_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed_path, database_path)


@app.get("/api/health")
def health():
    return {"ok": True, "database_configured": bool(os.getenv("DATABASE_URL")) or not os.getenv("VERCEL")}


@app.get("/api/status")
def status():
    demo_mode = _demo_enabled()
    if os.getenv("VERCEL") == "1" and not os.getenv("DATABASE_URL"):
        return {"apify_configured": False, "demo_mode": demo_mode,
                "daily_budget_left_usd": _daily_budget(), "queue_length": 0,
                "max_per_brand": min(_env_int("MAX_PER_BRAND", _config()["server"].get("max_per_brand", 100)),
                                     _config()["apify"]["max_reviews_per_brand"]),
                "max_brands_per_job": _env_int("MAX_BRANDS_PER_JOB", _config()["server"].get("max_brands_per_job", 6)),
                "storage_configured": False}
    return {"apify_configured": False if demo_mode else bool(get_apify_token()), "demo_mode": demo_mode,
            "daily_budget_left_usd": manager.daily_budget_left(), "queue_length": manager.queue_length(),
            "max_per_brand": min(_env_int("MAX_PER_BRAND", _config()["server"].get("max_per_brand", 100)),
                                 _config()["apify"]["max_reviews_per_brand"]),
            "max_brands_per_job": _env_int("MAX_BRANDS_PER_JOB", _config()["server"].get("max_brands_per_job", 6))}


@app.get("/api/brands")
def brands():
    config = _config()
    overview = get_overview_stats() if (os.getenv("VERCEL") != "1" or os.getenv("DATABASE_URL")) else {"per_brand": {}}
    return [{"name": name, "category": info["category"], "domain": info["domain"],
             "verified": bool(info.get("verified", False)),
             "cached_reviews": overview["per_brand"].get(name, {}).get("review_count", 0),
             "cache_exhausted": overview["per_brand"].get(name, {}).get("cache_exhausted", False),
             "last_collected": overview["per_brand"].get(name, {}).get("last_collected", "Not collected")}
            for name, info in config["brands"].items()]


@app.post("/api/jobs")
async def create_job(payload: JobRequest, request: Request, x_admin_key: str | None = Header(default=None)):
    configured_admin = os.getenv("ADMIN_KEY", "")
    admin = bool(configured_admin and x_admin_key and hmac.compare_digest(configured_admin, x_admin_key))
    client_ip = request.client.host if request.client else "unknown"
    return await manager.create(payload, client_ip, admin=admin)


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, request: Request, last_event_id: int | None = Query(default=None)):
    job = manager.get_job(job_id, refresh=True)
    if not job:
        raise HTTPException(404, "Order not found.")
    header_id = request.headers.get("last-event-id")
    cursor = last_event_id or 0
    if header_id:
        try:
            cursor = max(cursor, int(header_id))
        except ValueError:
            pass

    def stream():
        nonlocal cursor
        while True:
            if database._postgres_configured():
                pending = database.read_job_events(job_id, cursor)
                if pending:
                    for event in pending:
                        cursor = event["seq"]
                        yield f"id: {cursor}\nevent: pipeline\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    continue
                latest = manager.get_job(job_id, refresh=True)
                if latest and latest["finished"]:
                    break
                time.sleep(2)
                yield ": analysis is running\n\n"
                continue
            with job["event_condition"]:
                pending = [event for event in job["events"] if event["seq"] > cursor]
                finished = job["finished"]
                if not pending and not finished:
                    job["event_condition"].wait(timeout=12)
                    pending = [event for event in job["events"] if event["seq"] > cursor]
                    finished = job["finished"]
            if pending:
                for event in pending:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: pipeline\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            elif finished:
                break
            else:
                yield ": analysis is running\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    job = manager.get_job(job_id, refresh=True)
    if not job:
        raise HTTPException(404, "Order not found.")
    if not job["finished"]:
        raise HTTPException(409, "This analysis is still running.")
    return JSONResponse(content=json.loads(json.dumps(job["result"], default=str)))


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = manager.get_job(job_id, refresh=True)
    if not job:
        raise HTTPException(404, "Order not found.")
    return {"job_id": job_id, "status": job["status"], "brands": job["brands"],
            "demo": job["demo"], "queue_position": job["queue_position"]}


@app.get("/api/jobs/{job_id}/reviews")
def job_reviews(job_id: str, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                sentiment: str | None = Query(default=None, pattern="^(Positive|Neutral|Negative)$"),
                brand: str | None = Query(default=None, max_length=80)):
    job = manager.get_job(job_id, refresh=True)
    if not job:
        raise HTTPException(404, "Order not found.")
    rows = list(job.get("reviews", []))
    if sentiment:
        rows = [row for row in rows if row.get("Sentiment_Label") == sentiment]
    if brand:
        rows = [row for row in rows if row.get("Brand") == brand]
    start = (page - 1) * page_size
    return {"page": page, "page_size": page_size, "total": len(rows), "reviews": rows[start:start + page_size]}


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str):
    job = manager.get_job(job_id, refresh=True)
    if not job:
        raise HTTPException(404, "Order not found.")
    path_value = job.get("excel_path", "")
    if not job["finished"] or not path_value:
        raise HTTPException(404, "The workbook is not ready yet.")
    if str(path_value).startswith("https://"):
        try:
            from vercel import blob
            download = await blob.get_blob(path_value, access="private")
        except Exception as error:
            logger.error("Workbook download failed for job %s (%s).", job_id[:8], type(error).__name__)
            raise HTTPException(502, "The workbook could not be downloaded right now.") from error
        return Response(content=download.body,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=maison-insights-report.xlsx"})
    path = Path(path_value)
    if not path.is_file():
        raise HTTPException(404, "The workbook is not ready yet.")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=path.name)


@app.get("/")
def index():
    index_path = WEB_DIR / "index.html"
    if not index_path.is_file():
        return JSONResponse({"ok": False, "message": "Frontend files are missing."}, status_code=500)
    return FileResponse(index_path, media_type="text/html")


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
