/* Small SVG and DOM charts with no charting dependency. */
(function () {
  const palette = { Positive: "#168b79", Neutral: "#7d88d3", Negative: "#db8063" };

  function element(name, className, text) {
    const node = document.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function donut(target, data, legend) {
    target.replaceChildren();
    legend.replaceChildren();
    const totals = { Positive: 0, Neutral: 0, Negative: 0 };
    data.forEach((row) => { totals[row.Sentiment_Label] = Number(row.Count) || 0; });
    const total = Object.values(totals).reduce((sum, value) => sum + value, 0);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 120 120");
    svg.setAttribute("class", "donut-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Sentiment distribution: ${total} reviews scored`);
    const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = "Overall sentiment distribution";
    const description = document.createElementNS(svg.namespaceURI, "desc");
    description.textContent = ["Positive", "Neutral", "Negative"].map((label) => `${label}: ${totals[label]}`).join("; ");
    svg.append(title, description);
    const radius = 43;
    const circumference = 2 * Math.PI * radius;
    let offset = 0;
    ["Positive", "Neutral", "Negative"].forEach((label) => {
      const circle = document.createElementNS(svg.namespaceURI, "circle");
      circle.setAttribute("cx", "60"); circle.setAttribute("cy", "60"); circle.setAttribute("r", String(radius));
      circle.setAttribute("fill", "none"); circle.setAttribute("stroke", palette[label]); circle.setAttribute("stroke-width", "15");
      const segment = total ? circumference * totals[label] / total : 0;
      circle.setAttribute("stroke-dasharray", `${segment} ${circumference - segment}`);
      circle.setAttribute("stroke-dashoffset", String(-offset));
      circle.setAttribute("transform", "rotate(-90 60 60)");
      svg.appendChild(circle);
      offset += segment;
      const row = element("span");
      const swatch = element("i", "legend-swatch"); swatch.style.backgroundColor = palette[label];
      row.append(swatch, document.createTextNode(`${label} · ${totals[label]} (${total ? Math.round(totals[label] / total * 100) : 0}%)`));
      legend.appendChild(row);
    });
    const totalText = document.createElementNS(svg.namespaceURI, "text");
    totalText.setAttribute("x", "60"); totalText.setAttribute("y", "57"); totalText.setAttribute("text-anchor", "middle");
    totalText.setAttribute("class", "donut-total"); totalText.textContent = String(total);
    const caption = document.createElementNS(svg.namespaceURI, "text");
    caption.setAttribute("x", "60"); caption.setAttribute("y", "70"); caption.setAttribute("text-anchor", "middle");
    caption.setAttribute("class", "donut-caption"); caption.textContent = "reviews scored";
    svg.append(totalText, caption);
    target.appendChild(svg);
  }

  function stackedBars(target, rows, categoryKey) {
    target.replaceChildren();
    const groups = new Map();
    rows.forEach((row) => {
      const name = String(row[categoryKey] || "Not stated");
      if (!groups.has(name)) groups.set(name, { Positive: 0, Neutral: 0, Negative: 0 });
      groups.get(name)[row.Sentiment_Label] = Number(row.Percent) || 0;
    });
    if (!groups.size) { target.appendChild(element("p", "fine-print", "No scored reviews to chart yet.")); return; }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const rowHeight = 38; const height = groups.size * rowHeight + 8;
    svg.setAttribute("viewBox", `0 0 560 ${height}`); svg.setAttribute("class", "stacked-svg");
    svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `Sentiment by ${categoryKey.toLowerCase()}`);
    const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent = `Sentiment by ${categoryKey.toLowerCase()}`;
    const desc = document.createElementNS(svg.namespaceURI, "desc");
    desc.textContent = Array.from(groups, ([name, values]) => `${name}: ${["Positive", "Neutral", "Negative"].map((key) => `${key} ${values[key].toFixed(1)} percent`).join(", ")}`).join(". ");
    svg.append(title, desc);
    let index = 0;
    groups.forEach((values, name) => {
      const y = 7 + index * rowHeight; const label = document.createElementNS(svg.namespaceURI, "text");
      label.setAttribute("x", "0"); label.setAttribute("y", String(y + 13)); label.setAttribute("class", "bar-svg-label");
      label.textContent = name.length > 21 ? `${name.slice(0, 20)}…` : name; label.setAttribute("aria-label", name); svg.appendChild(label);
      let x = 165;
      const barWidth = 318;
      ["Positive", "Neutral", "Negative"].forEach((sentiment) => {
        const percent = Math.max(0, Math.min(100, values[sentiment]));
        if (percent > 0) {
          const rect = document.createElementNS(svg.namespaceURI, "rect");
          rect.setAttribute("x", String(x)); rect.setAttribute("y", String(y));
          rect.setAttribute("width", String(barWidth * percent / 100)); rect.setAttribute("height", "19");
          rect.setAttribute("fill", palette[sentiment]); rect.setAttribute("rx", "3");
          const rectTitle = document.createElementNS(svg.namespaceURI, "title");
          rectTitle.textContent = `${name}: ${sentiment} ${percent.toFixed(1)}%`; rect.appendChild(rectTitle);
          svg.appendChild(rect); x += barWidth * percent / 100;
        }
      });
      const total = Object.values(values).reduce((sum, item) => sum + item, 0);
      const totalLabel = document.createElementNS(svg.namespaceURI, "text");
      totalLabel.setAttribute("x", "555"); totalLabel.setAttribute("y", String(y + 14)); totalLabel.setAttribute("text-anchor", "end");
      totalLabel.setAttribute("class", "bar-svg-total"); totalLabel.textContent = `${Math.round(total)}%`; svg.appendChild(totalLabel);
      index += 1;
    });
    target.appendChild(svg);
  }

  function ratingMatrix(target, rows) {
    target.replaceChildren();
    const sentiments = ["Positive", "Neutral", "Negative"];
    const ratings = [...new Set(rows.map((row) => String(row.Rating)))].sort((a, b) => {
      const left = Number(a), right = Number(b);
      if (!Number.isFinite(left)) return 1;
      if (!Number.isFinite(right)) return -1;
      return left - right;
    });
    const lookup = new Map(rows.map((row) => [`${row.Rating}|${row.Sentiment_Label}`, Number(row.Count) || 0]));
    const table = element("table", "rating-matrix");
    table.setAttribute("aria-label", "Review counts by star rating and sentiment label");
    const head = element("tr"); head.appendChild(element("th", "", "Rating"));
    sentiments.forEach((label) => head.appendChild(element("th", "", label)));
    const thead = element("thead"); thead.appendChild(head); table.appendChild(thead);
    const body = element("tbody");
    ratings.forEach((rating) => {
      const row = element("tr"); row.appendChild(element("th", "", rating));
      sentiments.forEach((label) => {
        const count = lookup.get(`${rating}|${label}`) || 0;
        const cell = element("td", count ? "" : "zero", String(count));
        cell.setAttribute("aria-label", `${rating} stars, ${label}: ${count}`);
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
    table.appendChild(body); target.appendChild(table);
  }

  function themes(target, rows) {
    target.replaceChildren();
    rows.filter((row) => ["Positive terms", "Negative terms"].includes(row.Section)).slice(0, 12).forEach((row) => {
      const chip = element("span", `theme-chip ${row.Section === "Negative terms" ? "negative" : ""}`);
      chip.append(document.createTextNode(String(row.Name)), element("b", "", String(row.Count)));
      target.appendChild(chip);
    });
    if (!target.childElementCount) target.appendChild(element("p", "fine-print", "Themes will appear when there are enough review notes."));
  }

  function tech(target, rows) {
    target.replaceChildren();
    if (!rows.length) {
      target.appendChild(element("p", "fine-print", "No reviews mentioned an explicit technology term this time."));
      return;
    }
    rows.forEach((row) => {
      const line = element("div", "tech-row");
      line.append(element("strong", "", String(row.Sentiment_Label)), element("span", "", String(row.Count)));
      target.appendChild(line);
    });
  }

  window.ReviewCharts = { donut, stackedBars, ratingMatrix, themes, tech };
})();
