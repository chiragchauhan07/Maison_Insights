# Luxury Review Intelligence

This app is a focused workspace for public Trustpilot reviews from the 36 configured luxury brands. Select brands, follow collection and analysis in a live progress view, explore the results, then download a six-sheet Excel workbook. Reviewer names and profile links are discarded before data enters the database, job events, API responses, or workbook.

## Six-step flow

1. Select brands, a review limit, and whether to refresh the cache.
2. Reuse recent local reviews when the cache is sufficient; otherwise start one Apify actor run and poll its status.
3. Map reviews to the required 13 columns and store unique review IDs in SQLite.
4. Remove exact duplicates, flag near duplicates, and discard missing or too-short review text.
5. Score the review title and text with VADER; report conflicts between star ratings and labels.
6. Show interactive charts and reviews, then export the data and collection history to Excel.

## Files

- `server.py` — FastAPI app, API endpoints, safety limits, SSE events, and the single-worker job queue.
- `web/index.html` — accessible static screens, semantic content, and inline line-art illustrations.
- `web/css/styles.css` — responsive white and gray research workspace, reduced-motion rules, and layout.
- `web/js/app.js` — brand selection, job creation and resume, live progress, results, review filters, and downloads.
- `web/js/charts.js` — hand-built SVG sentiment charts and accessible text alternatives.
- `config.yaml` — exact brand list, categories, unverified domain guesses, aliases, and analysis settings.
- `collector.py` — Trustpilot Apify actor input, live run polling, mapping, and explicit technology matching.
- `pipeline.py` — cache reuse, cleaning, sentiment, chart data, workbook export, and SSE progress events.
- `database.py` — SQLite storage, integer ratings, cache history, daily spend, and hashed-IP rate-limit records.
- `cleaner.py` — review validation, exact and near-duplicate handling, and cleaning counters.
- `sentiment.py` — VADER scoring, optional multilingual fallback, mismatch flags, and themes.
- `exporter.py` — six-sheet Excel export with the 13 required columns and analysis fields.
- `utils.py` — token lookup, stable review IDs, date parsing, retries, and logging.
- `run_cli.py` — single-order backend CLI and isolated, no-network dry run.
- `collect_all.py` — confirmed sequential collection and optional cache top-up.
- `check_domains.py` — confirmed one-review domain checks saved outside the config.
- `export_seed.py` — whitelist-based SQLite seed export that drops any unapproved columns.
- `Dockerfile`, `render.yaml`, `.dockerignore` — one-worker container and Render deployment.
- `run_web.bat` — Windows launcher for the FastAPI app.
- `requirements.txt`, `requirements-dev.txt` — pinned runtime and development dependencies.
- `tests/` — API, data quality, pipeline, and workbook tests.

## Local setup

Use Python 3.11 or later. In PowerShell from the project folder:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Start the web app with `run_web.bat`, or run:

```powershell
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). The health endpoint is `/api/health`.

## Demo and real collection

The sample mode uses invented review data and makes no Apify request. Enable **Use sample data** in the analysis setup, open `http://127.0.0.1:8000/?demo=1`, or set `DEMO_MODE=1`.

For real collection, set `APIFY_TOKEN` in the process environment or in the project `.env` file, restart the server, and confirm the dashboard says **Apify connected**. Never commit `.env`. Normal startup keeps demo mode off; real web jobs use Apify's asynchronous `start()` and poll its run status. A dataset read error does not trigger another actor run. The API runs one job at a time. Cached orders do not start a run.

## Brands and collection tools

The canonical brand names, order, and categories in `config.yaml` are the supplied 36-brand list. Keep aliases for common spelling variants and keep a guessed Trustpilot domain marked `verified: false` until checked. The domain checker estimates cost and asks for confirmation before calling Apify:

```powershell
python check_domains.py
```

Bulk collection is also sequential and asks for confirmation; it can resume from SQLite and optionally top up to a target:

```powershell
python collect_all.py --per-brand 40 --max-total 1500 --top-up
```

The CLI supports normal backend runs and a separate no-network dry run:

```powershell
python run_cli.py --brands "Gucci,Rolex" --limit 20
python run_cli.py --brands "Gucci,Rolex" --dry-run
```

## Sentiment and data notes

English reviews are scored using the compound score for `Review_Title + Review_Text`; the stored text itself is left unchanged. Labels are Positive at 0.05 or above, Negative at -0.05 or below, and Neutral between those thresholds. `settings.sentiment_method: vader` retains the VADER label and adds `rating_sentiment_mismatch` if a 1–2 star review is Positive or a 4–5 star review is Negative. `vader_rating_aware` changes a mismatching label to match the rating (1–2 Negative, 3 Neutral, 4–5 Positive), retains the VADER score, and adds `label_from_rating`. The Summary sheet and API result include the mismatch count and rate. Non-English reviews use the optional multilingual model when installed; otherwise they receive a neutral fallback flag.

Technology context uses explicit whole-word terms only. `online` and `website` are intentionally excluded. The API and report never include reviewer names or profile links. Trustpilot is the only source in this version; Amazon and Reddit are future possibilities. Domain guesses are not verified claims.

## Apify budget

The planning estimate is approximately `$0.005` per actor run plus `$0.001` per requested review. The actual charge is controlled by Apify and can differ. The app caps reviews per brand, brands per order, concurrent jobs, hourly jobs per hashed client IP, and daily estimated spend. A refresh can consume credit even when a prior cache exists.

## Deploy on Vercel (frontend) and Render (backend)

This project uses Vercel for its static frontend and Render for the FastAPI backend. The backend is a long-running, single-worker service: it keeps the FIFO job queue and live SSE events in process memory and writes the review cache to SQLite. Deploying that process as a Vercel Function would make jobs/events unreliable and its local filesystem is not durable. `vercel.json` therefore publishes only `web/` and forwards `/api/*` to the Render service, preserving same-origin API calls in the browser.

### 1. Deploy the backend to Render

1. Push this repository to GitHub and open the repository in Render's **New + → Blueprint** flow.
2. Select `render.yaml` and create the `luxury-review-intelligence` web service. The Dockerfile starts one Uvicorn worker.
3. Set `APIFY_TOKEN` as a secret in the Render service's Environment settings. Keep `DEMO_MODE=1` while validating the deployment; change it to `0` only when you want real Trustpilot collection and have checked the Apify account budget.
4. Wait for the deployment to finish, then open `https://<your-render-service>.onrender.com/api/health`. It should return `{"ok":true}`. Copy the exact service hostname from Render.

The Blueprint currently uses Render's free plan. Free services can sleep and their filesystem is ephemeral, so SQLite cache/history and unfinished jobs are lost when the service restarts. For a persistent production cache, use a Render plan that supports a persistent disk, attach one at `/app/data` (the SQLite database is `data/reviews.db`), and keep one service instance. The workbook is created for the duration of a job and can be downloaded before the service restarts. A local seed database can be created with `python export_seed.py`; inspect it before committing.

### 2. Connect and deploy the Vercel frontend

1. Open `vercel.json` and replace `YOUR-RENDER-SERVICE` in the `/api/:path*` rewrite destination with the exact Render hostname, without `https://` or a trailing slash. For example, `https://luxury-review-intelligence-ab12.onrender.com/api/:path*`.
2. Commit and push that hostname change to GitHub (or make the edit before the first push if you have not pushed yet).
3. In Vercel, choose **Add New → Project**, import `chiragchauhan07/Percepta`, and deploy from the `main` branch. Keep the project root at the repository root. The checked-in config selects the `web/` output directory; no build command or API environment variable is needed.
4. Open the Vercel deployment URL. The page should load, **Trustpilot** connection status should resolve, and a demo analysis should finish. With `DEMO_MODE=1`, the UI uses sample reviews; switch to `0` in Render after setting `APIFY_TOKEN` to enable fresh collection.

The Vercel rewrite keeps browser API requests and EventSource updates on the Vercel hostname. Do not replace it with a direct browser-to-Render URL unless you also configure backend CORS for the Vercel domain. Every push to the connected GitHub branch triggers a new Vercel deployment.

### 3. Deployment smoke checks

- Vercel home page and `/css/styles.css`, `/js/app.js`, and `/logo5.png` load successfully.
- `https://<your-vercel-domain>/api/health` returns `{"ok":true}` through the rewrite.
- The UI reports demo mode while `DEMO_MODE=1`; start one sample analysis and confirm progress events complete and the results appear.
- After entering `APIFY_TOKEN` in Render and setting `DEMO_MODE=0`, refresh the connection status and verify the UI reports Apify connected before placing a small real order.

Never put `APIFY_TOKEN` in Vercel, frontend JavaScript, `vercel.json`, or GitHub. Keep it only as a Render secret. Review the Apify budget before testing real collection.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `APIFY_TOKEN` | Apify credential; never returned by the API or logged | unset |
| `DAILY_BUDGET_USD` | Maximum estimated daily Apify spend | `1.50` |
| `MAX_BRANDS_PER_JOB` | Maximum brands in one order | `6` |
| `MAX_PER_BRAND` | Maximum requested reviews per brand | `100` |
| `MAX_JOBS_PER_IP_PER_HOUR` | Hourly order limit per hashed forwarded IP | `5` |
| `ADMIN_KEY` | Optional private header key that bypasses order caps | unset |
| `DEMO_MODE` | Force sample data without Apify (`1` or `0`) | `0` |

## Excel report

Sheets are `Cleaned Data`, `Sentiment Data`, `Summary`, `Sentiment Report`, `Themes`, and `Collection Log`. The first 13 columns in the data sheets follow the required schema exactly. `Rating` is numeric when stated, not text. The sentiment sheet adds the title, score, label, flags, verification detail, and mismatch indicator.
