/* Workspace dashboard: collection jobs, SSE progress, charts, filters, and export. */
(function () {
  const $ = (selector) => document.querySelector(selector);
  const state = { brands: [], status: {}, selected: new Set(), category: "All categories", search: "", jobId: null,
    source: null, results: null, page: 1, sentiment: "", eventIds: new Set(), finishedBrands: new Set(), stage: 0 };
  let completed = false;
  const stageNames = { order: "Queued", fetch: "Collecting reviews", prep: "Cleaning reviews", cook: "Scoring sentiment", plate: "Building summaries", serve: "Preparing workbook" };
  const stageOrder = ["fetch", "prep", "cook", "plate", "serve"];
  const stagePercent = { order: 3, fetch: 18, prep: 38, cook: 62, plate: 82, serve: 94 };

  async function api(path, options) {
    const response = await fetch(path, options);
    const type = response.headers.get("content-type") || "";
    const body = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) throw new Error(body && typeof body === "object" ? body.detail || body.message || "Request failed." : body || "Request failed.");
    return body;
  }
  function toast(message, isError = false) {
    const node = $("#toast"); $("#toast-message").textContent = String(message);
    node.classList.toggle("error", isError); node.hidden = false;
    clearTimeout(toast.timer);
    if (!isError) toast.timer = setTimeout(() => { node.hidden = true; }, 3600);
  }
  function showDialog(id) { const dialog = $(id); if (dialog && !dialog.open) dialog.showModal(); }
  function setConnection(status) {
    state.status = status || {};
    const connected = Boolean(status.apify_configured), demo = Boolean(status.demo_mode);
    const card = $("#connection-card"); card.classList.toggle("connected", connected); card.classList.toggle("demo", demo);
    $("#connection-label").textContent = connected ? "Apify connected" : demo ? "Demo mode active" : "Apify not connected";
    $("#connection-description").textContent = connected ? "Fresh Trustpilot collection is available." : demo ? "Sample reviews are active. No API credit is used." : "Fresh reviews need an Apify token. Cached data still works.";
    const pill = $("#top-status"); pill.classList.toggle("connected", connected); pill.classList.toggle("demo", demo);
    pill.querySelector("span").textContent = connected ? "Apify connected" : demo ? "Sample mode" : "Apify setup needed";
    $("#service-alert").hidden = connected || demo;
    $("#dialog-status").textContent = connected ? "Apify is connected for this server." : demo ? "The server is currently configured for sample data." : "No Apify token is available to this server process.";
    $("#demo-toggle").disabled = demo;
    if (demo) $("#demo-toggle").checked = true;
    $("#limit-cap").textContent = `Maximum ${status.max_per_brand || 100}`;
    $("#per-brand").max = String(status.max_per_brand || 100);
    $("#per-brand").value = String(Math.min(Number($("#per-brand").value || 20), Number(status.max_per_brand || 100)));
    drawBrands(); updateOrder();
  }
  function filteredBrands() {
    const search = state.search.trim().toLocaleLowerCase();
    return state.brands.filter((brand) => (state.category === "All categories" || brand.category === state.category) &&
      (!search || brand.name.toLocaleLowerCase().includes(search)));
  }
  function drawBrands() {
    const list = $("#brand-list"); if (!list) return; list.replaceChildren();
    const rows = filteredBrands();
    $("#brand-list-empty").hidden = rows.length > 0;
    rows.forEach((brand) => {
      const label = document.createElement("label"); label.className = "brand-option";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = brand.name; input.checked = state.selected.has(brand.name);
      const dot = document.createElement("i"); dot.className = "brand-mark-small";
      const name = document.createElement("span"); name.className = "brand-option-name"; name.textContent = brand.name;
      label.title = `${brand.category} · ${brand.verified ? "Verified Trustpilot domain" : `Domain not verified: ${brand.domain || "not set"}`} · ${brand.cached_reviews || 0} cached reviews`;
      label.classList.toggle("selected", input.checked);
      input.addEventListener("change", () => {
        if (input.checked && state.selected.size >= Number(state.status.max_brands_per_job || 6)) { input.checked = false; toast(`You can select up to ${state.status.max_brands_per_job || 6} brands per analysis.`, true); return; }
        input.checked ? state.selected.add(brand.name) : state.selected.delete(brand.name);
        label.classList.toggle("selected", input.checked); $("#selection-count").textContent = `${state.selected.size} selected`;
        drawSelectedBrands(); updateOrder();
      });
      label.append(input, dot, name);
      if (!brand.verified) { const mark = document.createElement("span"); mark.className = "unverified-mark"; mark.setAttribute("aria-hidden", "true"); mark.textContent = "i"; label.appendChild(mark); }
      if (brand.cached_reviews > 0) { const cache = document.createElement("span"); cache.className = "brand-cache"; cache.textContent = `${brand.cached_reviews} cached`; label.appendChild(cache); }
      list.appendChild(label);
    });
    $("#selection-count").textContent = `${state.selected.size} selected`;
    drawSelectedBrands();
  }
  function drawSelectedBrands() {
    const list = $("#ticket-brands"), chosen = state.brands.filter((brand) => state.selected.has(brand.name));
    list.replaceChildren();
    if (!chosen.length) {
      const empty = document.createElement("p"); empty.className = "empty-ticket";
      empty.textContent = "Choose one or more brands to get started."; list.appendChild(empty); return;
    }
    chosen.forEach((brand) => {
      const chip = document.createElement("span"); chip.className = "selected-brand";
      const name = document.createElement("span"); name.textContent = brand.name;
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×";
      remove.setAttribute("aria-label", `Remove ${brand.name}`);
      remove.addEventListener("click", () => {
        state.selected.delete(brand.name); drawBrands(); updateOrder();
        const input = Array.from(document.querySelectorAll(".brand-option input")).find((node) => node.value === brand.name);
        if (input) input.focus();
      });
      chip.append(name, remove); list.appendChild(chip);
    });
  }
  function getEstimate() {
    const limit = Number($("#per-brand").value || 20), demo = $("#demo-toggle").checked || Boolean(state.status.demo_mode), refresh = $("#refresh-toggle").checked;
    const picked = state.brands.filter((brand) => state.selected.has(brand.name));
    const cached = picked.filter((brand) => !refresh && (brand.cached_reviews >= limit || brand.cache_exhausted));
    const fresh = picked.length - cached.length;
    const count = picked.reduce((sum, brand) => sum + (cached.includes(brand) ? Math.min(limit, brand.cached_reviews) : limit), 0);
    return { limit, picked, cached, fresh, count, cost: demo ? 0 : fresh ? fresh * (.005 + limit * .001) : 0 };
  }
  function updateOrder() {
    if (!state.status) return;
    const order = getEstimate(), demo = $("#demo-toggle").checked || Boolean(state.status.demo_mode);
    $("#selection-count").textContent = `${state.selected.size} selected`;
    $("#limit-display").textContent = order.limit;
    $("#estimate-reviews").textContent = order.count.toLocaleString();
    $("#estimate-cost").textContent = `$${order.cost.toFixed(2)}`;
    const max = Number(state.status.max_brands_per_job || 6), overBudget = !$("#demo-toggle").checked && order.cost > Number(state.status.daily_budget_left_usd || 0);
    let note = "Select at least one brand to begin.";
    if (state.selected.size > max) note = `Select no more than ${max} brands per analysis.`;
    else if (overBudget) note = `Estimated spend is above today's remaining budget ($${Number(state.status.daily_budget_left_usd || 0).toFixed(2)}). Reduce the review count or use cached reviews.`;
    else if (!demo && !state.status.apify_configured && order.fresh) note = "Apify is not set up for fresh reviews. Add APIFY_TOKEN to .env and restart, or select cached brands / sample data.";
    else if (state.selected.size) note = `${order.fresh ? `${order.fresh} brand${order.fresh === 1 ? "" : "s"} will be collected` : "Using cached reviews"}${order.cached.length ? ` · ${order.cached.length} brand${order.cached.length === 1 ? "" : "s"} from cache` : ""}.`;
    const noteNode = $("#order-note"); noteNode.textContent = note; noteNode.classList.toggle("error", (overBudget || (!state.status.apify_configured && !$("#demo-toggle").checked && order.fresh)) && state.selected.size > 0);
    const budget = $("#budget-warning"); budget.hidden = !overBudget;
    if (overBudget) budget.textContent = `Estimated spend exceeds today's remaining budget of $${Number(state.status.daily_budget_left_usd || 0).toFixed(2)}.`;
    const canStart = order.picked.length > 0 && order.picked.length <= max && !overBudget && (demo || order.fresh === 0 || state.status.apify_configured);
    $("#start-analysis").disabled = !canStart;
  }
  function appendActivity(message, kind = "info", brand = "") {
    const list = $("#activity-feed");
    if (list.querySelector(".empty-state")) list.replaceChildren();
    const row = document.createElement("li"); row.dataset.kind = kind;
    const text = document.createElement("span"); text.textContent = brand ? `${brand} · ${message}` : message;
    const time = document.createElement("time"); time.dateTime = new Date().toISOString(); time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    row.append(text, time); list.prepend(row); while (list.children.length > 30) list.lastElementChild.remove();
  }
  function drawRunBrands(brands, statuses = {}) {
    const list = $("#run-brands"); list.replaceChildren();
    brands.forEach((name) => {
      const row = document.createElement("div"); row.className = "run-brand"; row.dataset.brand = name;
      const dot = document.createElement("i"); dot.className = "run-brand-dot";
      const label = document.createElement("strong"); label.textContent = name;
      const status = document.createElement("span"); status.textContent = statuses[name] || "In queue";
      row.append(dot, label, status); list.appendChild(row);
    });
  }
  function updateRunState(label, stateName) {
    const node = $("#run-state"); node.className = `run-state ${stateName || "idle"}`;
    node.querySelector("span").textContent = label;
  }
  function stageProgress(event) {
    const stage = event.stage || "order", index = stageOrder.indexOf(stage);
    if (index >= 0) state.stage = Math.max(state.stage, index);
    let percent = stagePercent[stage] || 3;
    if (event.type === "done") percent = 100;
    $("#progress-fill").style.transform = `scaleX(${percent / 100})`;
    $("#progress-percent").textContent = `${percent}%`;
    $(".progress-track").setAttribute("aria-valuenow", String(percent));
    $("#progress-label").textContent = event.message || stageNames[stage] || "Working";
    document.querySelectorAll("[data-progress-stage]").forEach((node) => {
      const position = stageOrder.indexOf(node.dataset.progressStage);
      node.classList.toggle("active", position === index && event.type !== "done");
      node.classList.toggle("done", position < index || event.type === "done");
    });
    if (event.brand) {
      const row = Array.from(document.querySelectorAll(".run-brand")).find((item) => item.dataset.brand === event.brand);
      if (row && !state.finishedBrands.has(event.brand)) { row.classList.add("running"); row.querySelector("span").textContent = stageNames[stage] || "In progress"; }
    }
    if (event.type === "brand_done" && event.brand && !state.finishedBrands.has(event.brand)) {
      state.finishedBrands.add(event.brand);
      const row = Array.from(document.querySelectorAll(".run-brand")).find((item) => item.dataset.brand === event.brand);
      if (row) { row.classList.remove("running"); row.classList.add(event.message && /failed/i.test(event.message) ? "failed" : "done"); row.querySelector("span").textContent = event.message || "Complete"; }
    }
    if (event.type === "done") { $("#progress-fill").style.transform = "scaleX(1)"; $("#progress-percent").textContent = "100%"; }
  }
  function handleEvent(event) {
    if (event.seq && state.eventIds.has(event.seq)) return;
    if (event.seq) state.eventIds.add(event.seq);
    stageProgress(event);
    const message = event.message || stageNames[event.stage] || "Update received.";
    if (event.type === "warning" || event.type === "error") appendActivity(message, event.type, event.brand);
    else if (event.type !== "item" || ["prep", "cook"].includes(event.stage)) appendActivity(message, event.type, event.brand);
    if (event.type === "progress" || event.type === "stage_start") updateRunState("In progress", "running");
    if (event.type === "error") {
      updateRunState("Needs attention", "error"); $("#run-error").textContent = message; $("#run-error").hidden = false;
    }
    if (event.type === "done") {
      updateRunState("Complete", "complete"); appendActivity("Analysis complete. Your Excel report is ready.", "success");
      completed = true; $("#download-run").hidden = false; $("#view-findings").hidden = false; stopStream(); loadResult(state.jobId);
    }
  }
  async function startAnalysis() {
    const estimate = getEstimate();
    if (!estimate.picked.length) return;
    $("#start-analysis").disabled = true; $("#run-error").hidden = true; $("#download-run").hidden = true;
    $("#view-findings").hidden = true; $("#reopen-progress").hidden = false; completed = false;
    const progressDialog = $("#progress-dialog"); if (!progressDialog.open) progressDialog.showModal();
    $("#progress-fill").style.transform = "scaleX(0)"; $("#progress-percent").textContent = "0%"; $(".progress-track").setAttribute("aria-valuenow", "0"); $("#progress-label").textContent = "Submitting analysis…";
    $("#activity-feed").replaceChildren(); $("#results-panel").hidden = true; state.finishedBrands.clear(); state.eventIds.clear(); state.stage = 0;
    drawRunBrands(estimate.picked.map((brand) => brand.name));
    try {
      const response = await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brands: estimate.picked.map((brand) => brand.name), per_brand: estimate.limit,
          refresh: $("#refresh-toggle").checked, demo: $("#demo-toggle").checked || Boolean(state.status.demo_mode) }) });
      state.jobId = response.job_id; localStorage.setItem("luxury-review-job", state.jobId);
      localStorage.setItem("luxury-review-seq", "0"); history.replaceState(null, "", `${location.pathname}${location.search}#job=${encodeURIComponent(state.jobId)}`);
      $("#run-summary").textContent = `${estimate.picked.length} brand${estimate.picked.length === 1 ? "" : "s"} · up to ${estimate.limit} reviews each`;
      updateRunState("Queued", "running"); appendActivity(`Analysis queued · position ${response.queue_position || 1}.`, "info");
      connectEvents(state.jobId, true);
    } catch (error) { updateRunState("Not started", "error"); $("#run-error").textContent = error.message; $("#run-error").hidden = false; $("#start-analysis").disabled = false; }
  }
  function stopStream() { if (state.source) { state.source.close(); state.source = null; } }
  function connectEvents(jobId, replay) {
    stopStream(); const cursor = replay ? 0 : Number(localStorage.getItem("luxury-review-seq") || 0);
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events?last_event_id=${cursor}`); state.source = source;
    source.onopen = () => updateRunState("Connected", "running");
    source.addEventListener("pipeline", async (message) => {
      let event; try { event = JSON.parse(message.data); } catch (_) { return; }
      localStorage.setItem("luxury-review-seq", String(event.seq || message.lastEventId || 0)); handleEvent(event);
    });
    source.onerror = () => {
      if (!state.source) return;
      updateRunState("Reconnecting", "running");
      if (state.jobId === jobId) setTimeout(async () => {
        try { const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`); if (job.status === "done") { stopStream(); await loadResult(jobId); }
          else if (job.status === "failed") { stopStream(); updateRunState("Needs attention", "error"); }
        } catch (_) { stopStream(); updateRunState("Unavailable", "error"); }
      }, 1800);
    };
  }
  function percentPositive(rows) { const row = rows.find((item) => item.Sentiment_Label === "Positive"); return Math.round(Number(row && row.Percent) || 0); }
  function renderResults(result) {
    state.results = result; const charts = result.charts || {}, stats = result.stats || {}, overall = charts.overall || [];
    ReviewCharts.donut($("#overall-donut"), overall, $("#donut-legend"));
    ReviewCharts.stackedBars($("#brand-bars"), charts.by_brand || [], "Brand");
    ReviewCharts.stackedBars($("#category-bars"), charts.by_category || [], "Category");
    ReviewCharts.stackedBars($("#platform-bars"), charts.by_platform || [], "Platform");
    ReviewCharts.ratingMatrix($("#rating-check"), charts.rating_vs_sentiment || []);
    ReviewCharts.themes($("#theme-chips"), charts.themes || []); ReviewCharts.tech($("#tech-chart"), charts.tech_sentiment || []);
    const cards = [["Unique reviews", stats.final_unique || 0], ["Collected this run", stats.total_collected_this_run ?? stats.total_collected ?? 0],
      ["Positive sentiment", `${percentPositive(overall)}%`], ["Rating conflicts", stats.rating_sentiment_mismatch_count || 0]];
    const summary = $("#summary-cards"); summary.replaceChildren();
    cards.forEach(([label, value]) => { const card = document.createElement("article"); card.className = "result-metric";
      const title = document.createElement("span"), metric = document.createElement("strong"); title.textContent = label;
      metric.textContent = typeof value === "number" ? value.toLocaleString() : String(value); card.append(title, metric); summary.appendChild(card); });
    const brandCount = Object.keys(result.brand_status || {}).length;
    $("#results-subtitle").textContent = `${Number(stats.final_unique || 0).toLocaleString()} unique reviews across ${brandCount} selected brand${brandCount === 1 ? "" : "s"}.`;
    $("#result-cleaning-note").textContent = `${Number(stats.duplicates_removed || 0).toLocaleString()} duplicates removed · ${Number(stats.invalid_removed || 0).toLocaleString()} invalid reviews removed.`;
    $("#mismatch-note").textContent = `${stats.rating_sentiment_mismatch_count || 0} conflicts · ${((Number(stats.rating_sentiment_mismatch_rate) || 0) * 100).toFixed(1)}% of scored reviews. A conflict means the sentiment label and star rating disagree.`;
    $("#demo-badge").hidden = !result.demo;
    const filter = $("#review-brand"); filter.replaceChildren(new Option("All brands", ""));
    state.brands.filter((brand) => Object.hasOwn(result.brand_status || {}, brand.name)).forEach((brand) => filter.add(new Option(brand.name, brand.name)));
    state.page = 1; state.sentiment = ""; $("#review-sentiment").value = ""; loadReviews(true);
    $("#results-panel").hidden = false; $("#nav-results").hidden = false; $("#nav-review-count").textContent = stats.final_unique || 0;
    if (!$("#application").hidden && !$("#progress-dialog").open) $("#results-panel").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  async function loadResult(jobId) {
    try { renderResults(await api(`/api/jobs/${encodeURIComponent(jobId)}/result`)); }
    catch (error) { $("#run-error").hidden = false; $("#run-error").textContent = `The analysis finished, but results could not be loaded: ${error.message}`; }
  }
  async function loadReviews(clear) {
    if (!state.jobId) return;
    try {
      const query = new URLSearchParams({ page: String(state.page), page_size: "10" });
      if (state.sentiment) query.set("sentiment", state.sentiment); if ($("#review-brand").value) query.set("brand", $("#review-brand").value);
      const data = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/reviews?${query}`); const list = $("#review-table"); if (clear) list.replaceChildren();
      data.reviews.forEach((row) => list.appendChild(makeReview(row)));
      $("#reviews-empty").hidden = data.total > 0; $("#reviews-empty").textContent = data.total ? "" : "No reviews match these filters.";
      $("#review-count-note").textContent = `${Number(data.total).toLocaleString()} reviews match the current filters.`;
      $("#load-more").hidden = state.page * 10 >= data.total;
    } catch (_) { $("#load-more").hidden = true; }
  }
  function makeReview(row) {
    const details = document.createElement("details"); details.className = "review-row";
    const summary = document.createElement("summary"), brand = document.createElement("strong"), label = document.createElement("span"), meta = document.createElement("span");
    brand.className = "review-brand"; brand.textContent = row.Brand || "Brand not stated";
    const sentiment = row.Sentiment_Label || "Neutral"; label.className = `review-badge ${sentiment.toLowerCase()}`; label.textContent = sentiment;
    meta.textContent = `${row.Rating || "—"}/5 · ${row.Review_Date || "Date not stated"}`; summary.append(brand, label, meta);
    const text = document.createElement("p"); text.className = "review-snippet"; text.textContent = [row.Review_Title, row.Review_Text].filter((value) => value && value !== "Not stated").join(" — ") || "Review text not stated.";
    const footer = document.createElement("div"); footer.className = "review-meta";
    const detail = document.createElement("span"); detail.textContent = `${row.Country || "Country not stated"} · ${row.Language || "Language not stated"}`;
    const link = document.createElement("a"); link.textContent = "Source review"; link.target = "_blank"; link.rel = "noopener noreferrer";
    try { const url = new URL(row.Source_URL); if (url.protocol === "https:") link.href = url.href; else link.textContent = "Source unavailable"; } catch (_) { link.textContent = "Source unavailable"; }
    footer.append(detail, link); details.append(summary, text, footer); return details;
  }
  async function resume() {
    const match = location.hash.match(/(?:^#|&)job=([^&]+)/), jobId = match ? decodeURIComponent(match[1]) : null;
    if (!jobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`); state.jobId = jobId; drawRunBrands(job.brands || []);
      $("#run-summary").textContent = `${job.brands.length} selected brands`; updateRunState(job.status === "done" ? "Complete" : "In progress", job.status === "done" ? "complete" : "running");
      if (job.status === "done") { completed = true; $("#download-run").hidden = false; $("#view-findings").hidden = false; await loadResult(jobId); }
      else { $("#reopen-progress").hidden = false; connectEvents(jobId, false); }
    } catch (_) { localStorage.removeItem("luxury-review-job"); history.replaceState(null, "", location.pathname + location.search); }
  }
  function bind() {
    const rotatingWord = $("#rotating-word");
    const phrases = ["value", "remember", "expect"];
    if (rotatingWord && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      let phrase = 0;
      setInterval(() => {
        if (document.hidden || $("#landing").hidden) return;
        rotatingWord.classList.add("is-changing");
        setTimeout(() => { phrase = (phrase + 1) % phrases.length; rotatingWord.textContent = phrases[phrase]; rotatingWord.classList.remove("is-changing"); }, 260);
      }, 3000);
    }
    $("#landing-start").addEventListener("click", () => {
      $("#landing").hidden = true; $("#application").hidden = false;
      if (!location.hash.includes("job=")) {
        $("#results-panel").hidden = true; $("#nav-results").hidden = true;
        if (completed) { state.jobId = null; state.results = null; completed = false; localStorage.removeItem("luxury-review-job"); }
        $("#reopen-progress").hidden = !state.jobId;
      }
      if (state.jobId && !completed && !$("#progress-dialog").open) $("#progress-dialog").showModal();
      requestAnimationFrame(() => $("#brand-search").focus());
    });
    $("#back-home").addEventListener("click", (event) => {
      event.preventDefault(); history.replaceState(null, "", `${location.pathname}${location.search}`); $("#application").hidden = true; $("#landing").hidden = false; window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
    $("#brand-search").addEventListener("input", (event) => { state.search = event.target.value; drawBrands(); });
    document.querySelectorAll(".category-tab").forEach((tab) => tab.addEventListener("click", () => {
      state.category = tab.dataset.category;
      document.querySelectorAll(".category-tab").forEach((button) => { const active = button === tab; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
      drawBrands();
    }));
    $("#per-brand").addEventListener("input", updateOrder); $("#refresh-toggle").addEventListener("change", updateOrder); $("#demo-toggle").addEventListener("change", updateOrder);
    $("#start-analysis").addEventListener("click", startAnalysis);
    $("#progress-close").addEventListener("click", () => $("#progress-dialog").close());
    $("#reopen-progress").addEventListener("click", () => { if (!$("#progress-dialog").open) $("#progress-dialog").showModal(); });
    $("#view-findings").addEventListener("click", () => {
      $("#progress-dialog").close(); $("#results-panel").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    });
    $("#connection-help").addEventListener("click", () => showDialog("#connection-dialog")); $("#alert-settings").addEventListener("click", () => showDialog("#connection-dialog"));
    $("#refresh-status").addEventListener("click", async () => { try { setConnection(await api("/api/status")); toast("Connection status refreshed."); } catch (_) { toast("Could not reach the local server.", true); } });
    $("#help-button").addEventListener("click", () => showDialog("#about-dialog"));
    $("#review-sentiment").addEventListener("change", (event) => { state.sentiment = event.target.value; state.page = 1; loadReviews(true); });
    $("#review-brand").addEventListener("change", () => { state.page = 1; loadReviews(true); }); $("#load-more").addEventListener("click", () => { state.page++; loadReviews(false); });
    ["#download-results", "#download-run"].forEach((selector) => $(selector).addEventListener("click", () => {
      if (!state.jobId) return; const link = document.createElement("a"); link.href = `/api/jobs/${encodeURIComponent(state.jobId)}/download`; link.download = "luxury_sentiment.xlsx"; document.body.appendChild(link); link.click(); link.remove();
    }));
    $("#toast-close").addEventListener("click", () => { clearTimeout(toast.timer); $("#toast").hidden = true; });
    document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => {
      document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("active", item === link));
    }));
  }
  async function init() {
    bind();
    try {
      const [status, brands] = await Promise.all([api("/api/status"), api("/api/brands")]);
      state.brands = brands; state.status = status; setConnection(status); $("#metric-brands").textContent = brands.length;
      $("#metric-reviews").textContent = brands.reduce((sum, brand) => sum + Number(brand.cached_reviews || 0), 0).toLocaleString();
      await resume();
    } catch (error) {
      $("#connection-label").textContent = "Server unavailable"; $("#connection-description").textContent = "Start the app, then refresh this page.";
      $("#service-alert").hidden = false; $("#service-alert-title").textContent = "The application server is unavailable";
      $("#service-alert-message").textContent = "Start this project with .\\run_web.bat, then refresh the page.";
      $("#metric-brands").textContent = "—"; $("#order-note").textContent = error.message; $("#order-note").classList.add("error");
    }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
