// docqa Metrics tab: live (online) metrics from your traces, offline eval runs, and a guide.
// Charts are plain SVG. Every chart has a table view; values are inserted with textContent.
"use strict";

window.DocqaMetrics = (() => {
  const STRATEGY_SLOT = { dense: 1, bm25: 2, hybrid: 3, hybrid_rerank: 4, agent: 5 }; // color follows the strategy
  const state = { sub: "live", window: "7d", strategy: "", glossary: null, live: null, runs: null, run: null, trendMetric: "correct" };
  const SVG = "http://www.w3.org/2000/svg";

  // ---------------------------------------------------------------- helpers

  const $ = (id) => document.getElementById(id);
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function svg(tag, attrs = {}) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  }
  async function getJSON(path) {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("signed out");
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error((data && data.detail) || `HTTP ${response.status}`);
    return data;
  }

  const fmt = {
    num: (v, d = 0) => (v === null || v === undefined ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })),
    pct: (v, d = 1) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(d)}%`),
    ms: (v) => (v === null || v === undefined ? "–" : v >= 10000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v).toLocaleString()} ms`),
    usd: (v) => (v === null || v === undefined ? "–" : v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(6)}` : `$${v.toFixed(4)}`),
    score: (v) => (v === null || v === undefined ? "–" : Number(v).toFixed(3)),
  };

  // ---------------------------------------------------------------- tooltip

  const tip = () => $("viz-tip");
  function showTip(event, rows) {
    const box = tip();
    box.replaceChildren();
    for (const [label, value, slot] of rows) {
      const row = el("div", null, "tip-row");
      if (slot) row.append(el("span", null, `key series-${slot}`));
      row.append(el("strong", value), el("span", label, "tip-label"));
      box.append(row);
    }
    box.hidden = false;
    const rect = (event.target.getBoundingClientRect && event.type === "focus") ? event.target.getBoundingClientRect() : null;
    const x = rect ? rect.right : event.clientX;
    const y = rect ? rect.top : event.clientY;
    const left = Math.min(x + 12, window.innerWidth - box.offsetWidth - 8);
    box.style.left = `${Math.max(8, left)}px`;
    box.style.top = `${Math.max(8, y - box.offsetHeight - 10)}px`;
  }
  function hideTip() {
    tip().hidden = true;
  }
  function hoverable(node, rows) {
    node.setAttribute("tabindex", "0");
    node.classList.add("hit");
    const show = (e) => showTip(e, rows);
    node.addEventListener("pointermove", show);
    node.addEventListener("focus", show);
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("blur", hideTip);
  }

  // ---------------------------------------------------------------- glossary (the "what is this?")

  function info(key) {
    const meta = state.glossary && state.glossary[key];
    if (!meta) return null;
    const details = el("details", null, "info");
    const summary = el("summary", "ⓘ", "info-toggle");
    summary.setAttribute("aria-label", `What is ${meta.name}?`);
    details.append(summary);
    const body = el("div", null, "info-body");
    body.append(el("p", meta.definition));
    const formula = el("p", null, "formula");
    formula.append(el("span", "Formula ", "muted"), el("code", meta.formula));
    body.append(formula);
    body.append(el("p", `Why it matters: ${meta.why}`));
    const better = { higher: "Higher is better.", lower: "Lower is better.", target: "Aim for a target, not an extreme.", context: "Read in context; no universal direction." }[meta.better];
    body.append(el("p", better, "muted"));
    if (meta.caveat) body.append(el("p", `Caveat: ${meta.caveat}`, "caveat"));
    details.append(body);
    // Fixed to the viewport and clamped, so it never runs off the screen edge.
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      for (const other of document.querySelectorAll("details.info[open]")) if (other !== details) other.open = false;
      const r = summary.getBoundingClientRect();
      const w = Math.min(352, window.innerWidth - 16);
      body.style.width = `${w}px`;
      body.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
      const below = r.bottom + 6;
      body.style.top = `${below + body.offsetHeight > window.innerHeight ? Math.max(8, r.top - body.offsetHeight - 6) : below}px`;
    });
    return details;
  }

  function tile(key, value, sub) {
    const meta = state.glossary && state.glossary[key];
    const card = el("div", null, "tile");
    const head = el("div", null, "tile-head");
    head.append(el("span", meta ? meta.name : key, "tile-label"));
    const i = info(key);
    if (i) head.append(i);
    card.append(head, el("div", value, "tile-value"));
    if (sub) card.append(el("div", sub, "tile-sub"));
    return card;
  }

  function section(title, note, infoKey) {
    const box = el("section", null, "m-section");
    const h = el("h2", title);
    const i = infoKey ? info(infoKey) : null;
    if (i) h.append(i);
    box.append(h);
    if (note) box.append(el("p", note, "m-note"));
    return box;
  }

  // ---------------------------------------------------------------- chart frame + table view

  function figure(title, subtitle, legend, table) {
    const fig = el("figure", null, "chart");
    const cap = el("figcaption");
    cap.append(el("span", title, "chart-title"));
    if (subtitle) cap.append(el("span", subtitle, "chart-sub"));
    fig.append(cap);
    if (legend && legend.length > 1) {
      const lg = el("div", null, "legend");
      for (const [label, slot, shape] of legend) {
        const item = el("span", null, "legend-item");
        item.append(el("span", null, `key ${shape || "rect"} series-${slot}`), document.createTextNode(label));
        lg.append(item);
      }
      fig.append(lg);
    }
    const plot = el("div", null, "plot");
    fig.append(plot);
    if (table) {
      const details = el("details", null, "table-view");
      details.append(el("summary", "Table view"));
      details.append(table);
      fig.append(details);
    }
    return { fig, plot };
  }

  function table(headers, rows, numeric = []) {
    const t = el("table", null, "data-table");
    const head = el("tr");
    headers.forEach((h, i) => head.append(el("th", h, numeric.includes(i) ? "num" : "")));
    t.append(el("thead"));
    t.tHead.append(head);
    const body = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      row.forEach((cell, i) => {
        const td = el("td", null, numeric.includes(i) ? "num" : "");
        if (cell instanceof Node) td.append(cell);
        else td.textContent = cell;
        tr.append(td);
      });
      body.append(tr);
    }
    t.append(body);
    return t;
  }

  const width = (plot) => Math.max(280, plot.clientWidth || plot.parentElement.clientWidth || 600);

  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  // Horizontal bars (magnitude by category): bar = main value; optional tick = second value.
  function hbars(plot, rows, { format, tickLabel, barLabel }) {
    const w = width(plot);
    const narrow = w < 520;
    const longest = Math.max(...rows.map((r) => r.label.length));
    const labelW = Math.min(w * 0.4, longest * 7.4 + 14); // monospace 12px is ~7.2px per character
    const hasTick = rows.some((r) => r.tick !== null && r.tick !== undefined);
    const valueW = narrow ? 10 : hasTick ? 160 : 96; // on phones the tooltip and table carry the values
    const rowH = 30;
    const ticks = narrow ? 2 : 4;
    const h = rows.length * rowH + 26;
    const max = niceMax(Math.max(...rows.map((r) => Math.max(r.value || 0, r.tick || 0))));
    const x = (v) => labelW + ((w - labelW - valueW) * (v || 0)) / max;
    const root = svg("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "img" });
    for (let i = 0; i <= ticks; i += 1) {
      const gx = labelW + ((w - labelW - valueW) * i) / ticks;
      root.append(svg("line", { x1: gx, x2: gx, y1: 0, y2: h - 22, class: i === 0 ? "baseline" : "grid" }));
      const t = svg("text", { x: gx, y: h - 6, class: "axis", "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" });
      t.textContent = format((max * i) / ticks);
      root.append(t);
    }
    rows.forEach((r, i) => {
      const y = i * rowH + 6;
      const label = svg("text", { x: labelW - 8, y: y + 13, class: `axis-label${r.depth ? " nested" : ""}`, "text-anchor": "end" });
      label.textContent = r.label;
      root.append(label);
      const barW = Math.max(2, x(r.value) - labelW);
      const bar = svg("path", { d: roundedBar(labelW, y + 2, barW, 16), class: `bar series-${r.slot || 1}` });
      root.append(bar);
      if (r.tick !== null && r.tick !== undefined) {
        root.append(svg("line", { x1: x(r.tick), x2: x(r.tick), y1: y - 1, y2: y + 21, class: "tick-mark" }));
      }
      if (!narrow) {
        const value = svg("text", { x: w - valueW + 10, y: y + 13, class: "value" });
        value.textContent = r.tick !== null && r.tick !== undefined ? `${format(r.value)} · ${format(r.tick)}` : format(r.value);
        root.append(value);
      }
      const hit = svg("rect", { x: 0, y: y - 3, width: w, height: rowH, class: "hit-area" });
      const tipRows = [[barLabel, format(r.value), r.slot || 1]];
      if (r.tick !== null && r.tick !== undefined) tipRows.push([tickLabel, format(r.tick)]);
      if (r.extra) tipRows.push(...r.extra);
      tipRows.unshift([r.label, ""]);
      hoverable(hit, tipRows);
      root.append(hit);
    });
    plot.replaceChildren(root);
  }

  // Bar with a 4px rounded data end and a square baseline end.
  function roundedBar(x, y, w, h) {
    const r = Math.min(4, w / 2, h / 2);
    return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`;
  }

  // Columns over time (one series).
  function columns(plot, points, { format, label }) {
    const w = width(plot);
    const h = 180;
    const left = 52;
    const bottom = 24;
    const max = niceMax(Math.max(...points.map((p) => p.value || 0)));
    const band = (w - left - 8) / Math.max(points.length, 1);
    const colW = Math.min(24, band * 0.6);
    const y = (v) => 8 + (h - bottom - 8) * (1 - (v || 0) / max);
    const root = svg("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "img" });
    for (let i = 0; i <= 4; i += 1) {
      const v = (max * i) / 4;
      root.append(svg("line", { x1: left, x2: w - 8, y1: y(v), y2: y(v), class: i === 0 ? "baseline" : "grid" }));
      const t = svg("text", { x: left - 6, y: y(v) + 4, class: "axis", "text-anchor": "end" });
      t.textContent = format(v);
      root.append(t);
    }
    points.forEach((p, i) => {
      const cx = left + band * i + band / 2;
      const top = Math.min(y(p.value), y(0) - 1);
      root.append(svg("path", { d: `M${cx - colW / 2},${y(0)} V${top + 4} Q${cx - colW / 2},${top} ${cx - colW / 2 + 4},${top} H${cx + colW / 2 - 4} Q${cx + colW / 2},${top} ${cx + colW / 2},${top + 4} V${y(0)} Z`, class: "bar series-1" }));
      if (points.length <= 14 || i === 0 || i === points.length - 1) {
        const t = svg("text", { x: cx, y: h - 6, class: "axis", "text-anchor": "middle" });
        t.textContent = p.label;
        root.append(t);
      }
      const hit = svg("rect", { x: cx - band / 2, y: 0, width: band, height: h - bottom, class: "hit-area" });
      hoverable(hit, [[p.label, ""], [label, format(p.value), 1]]);
      root.append(hit);
    });
    plot.replaceChildren(root);
  }

  // Lines (one per series) over ordered x positions; crosshair tooltip lists every series.
  function lines(plot, xs, series, { format, maxValue }) {
    const w = width(plot);
    const h = 200;
    const left = 52;
    const right = 96;
    const bottom = 24;
    const max = maxValue || niceMax(Math.max(...series.flatMap((s) => s.values.filter((v) => v !== null))));
    const x = (i) => left + (xs.length === 1 ? (w - left - right) / 2 : ((w - left - right) * i) / (xs.length - 1));
    const y = (v) => 8 + (h - bottom - 8) * (1 - v / max);
    const root = svg("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "img" });
    for (let i = 0; i <= 4; i += 1) {
      const v = (max * i) / 4;
      root.append(svg("line", { x1: left, x2: w - right, y1: y(v), y2: y(v), class: i === 0 ? "baseline" : "grid" }));
      const t = svg("text", { x: left - 6, y: y(v) + 4, class: "axis", "text-anchor": "end" });
      t.textContent = format(v);
      root.append(t);
    }
    xs.forEach((label, i) => {
      if (xs.length <= 8 || i === 0 || i === xs.length - 1) {
        const t = svg("text", { x: x(i), y: h - 6, class: "axis", "text-anchor": "middle" });
        t.textContent = label;
        root.append(t);
      }
    });
    const cross = svg("line", { x1: 0, x2: 0, y1: 8, y2: h - bottom, class: "crosshair", visibility: "hidden" });
    root.append(cross);
    for (const s of series) {
      const pts = s.values.map((v, i) => (v === null ? null : [x(i), y(v)]));
      let d = "";
      pts.forEach((p, i) => {
        if (!p) return;
        d += `${d && pts[i - 1] ? "L" : "M"}${p[0]},${p[1]} `;
      });
      root.append(svg("path", { d, class: `line series-${s.slot}` }));
      pts.forEach((p) => p && root.append(svg("circle", { cx: p[0], cy: p[1], r: 4, class: `dot series-${s.slot}` })));
      const last = [...pts].reverse().find(Boolean);
      if (last && series.length <= 4) {
        const t = svg("text", { x: last[0] + 8, y: last[1] + 4, class: "end-label" });
        t.textContent = s.name;
        root.append(t);
      }
    }
    const overlay = svg("rect", { x: left, y: 0, width: w - left - right, height: h - bottom, class: "hit-area" });
    overlay.setAttribute("tabindex", "0");
    const at = (clientX) => {
      const box = root.getBoundingClientRect();
      const px = clientX - box.left;
      let best = 0;
      xs.forEach((_, i) => {
        if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
      });
      return best;
    };
    const show = (e, i) => {
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("visibility", "visible");
      showTip(e, [[xs[i], ""], ...series.map((s) => [s.name, format(s.values[i]), s.slot])]);
    };
    overlay.addEventListener("pointermove", (e) => show(e, at(e.clientX)));
    overlay.addEventListener("focus", (e) => show(e, xs.length - 1));
    const hide = () => {
      cross.setAttribute("visibility", "hidden");
      hideTip();
    };
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("blur", hide);
    root.append(overlay);
    plot.replaceChildren(root);
  }

  // A number with a thin inline bar, for comparison tables (0..1 metrics).
  function meterCell(value, slot, max = 1) {
    const box = el("span", null, "meter-cell");
    box.append(el("span", fmt.score(value), "meter-value"));
    const track = el("span", null, "meter-track");
    const fill = el("span", null, `meter-fill series-${slot}`);
    fill.style.width = `${value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value / max)) * 100}%`;
    track.append(fill);
    box.append(track);
    return box;
  }

  function strategyCell(strategy) {
    const box = el("span", null, "strategy-cell");
    box.append(el("span", null, `key rect series-${STRATEGY_SLOT[strategy] || 1}`), document.createTextNode(strategy));
    return box;
  }

  // ---------------------------------------------------------------- live view

  async function loadLive() {
    const qs = new URLSearchParams({ window: state.window });
    if (state.strategy) qs.set("strategy", state.strategy);
    $("metrics-body").classList.add("refreshing");
    try {
      state.live = await getJSON(`/api/metrics/live?${qs}`);
      $("metrics-updated").textContent = `Updated ${new Date(state.live.generated_at).toLocaleTimeString()}`;
      renderLive();
    } catch (err) {
      if (err.message !== "signed out") $("metrics-body").replaceChildren(el("p", `Could not load metrics: ${err.message}`, "error-text"));
    } finally {
      $("metrics-body").classList.remove("refreshing");
    }
  }

  function renderLive() {
    const m = state.live;
    const body = $("metrics-body");
    body.replaceChildren();
    const k = m.kpis;
    if (!k.questions) {
      body.append(el("p", "No questions in this window yet. Ask a few in the Chat tab (try different strategies), then refresh.", "empty"));
      return;
    }

    const intro = section("Live traffic", "Online metrics: computed from the traces of your own questions. There is no answer key here, so these measure behaviour (speed, cost, abstentions, confidence signals) and your 👍/👎. For quality against known answers, see Offline evaluation.");
    const kpis = el("div", null, "tiles");
    kpis.append(
      tile("questions", fmt.num(k.questions), `${fmt.num(k.conversations)} conversations`),
      tile("answer_rate", fmt.pct(k.answer_rate)),
      tile("not_found_rate", fmt.pct(k.not_found_rate)),
      tile("error_rate", fmt.pct(k.error_rate)),
      tile("helpful_rate", fmt.pct(k.helpful_rate), `coverage ${fmt.pct(k.feedback_coverage, 0)}`),
      tile("latency_p50", fmt.ms(k.latency_p50)),
      tile("latency_p95", fmt.ms(k.latency_p95), `P99 ${fmt.ms(k.latency_p99)} · tail ×${fmt.num(k.tail_ratio, 1)}`),
      tile("cost_per_question", fmt.usd(k.cost_per_question), `total ${fmt.usd(k.total_cost)}`),
    );
    intro.append(kpis);
    body.append(intro);

    // Latency
    const lat = section("Latency: where the time goes", null, "stage_latency");
    const stageRows = m.stages.map((s) => ({
      label: s.depth ? `↳ ${s.name}` : s.name,
      depth: s.depth,
      value: s.latency_ms.p50,
      tick: s.latency_ms.p95,
      extra: [["share of time", s.share === null ? "–" : fmt.pct(s.share)], ["questions", fmt.num(s.latency_ms.count)]],
    }));
    const stageTable = table(
      ["Step", "Questions", "P50", "P90", "P95", "P99", "Share of time"],
      m.stages.map((s) => [s.depth ? `↳ ${s.name}` : s.name, fmt.num(s.latency_ms.count), fmt.ms(s.latency_ms.p50), fmt.ms(s.latency_ms.p90), fmt.ms(s.latency_ms.p95), fmt.ms(s.latency_ms.p99), s.share === null ? "–" : fmt.pct(s.share)]),
      [1, 2, 3, 4, 5, 6],
    );
    const stageFig = figure("Time per step", "bar = P50, tick = P95 · ↳ = inside an agent step", [["P50", 1, "rect"], ["P95", "ink", "tick"]], stageTable);
    lat.append(stageFig.fig);
    body.append(lat);
    hbars(stageFig.plot, stageRows, { format: fmt.ms, barLabel: "P50", tickLabel: "P95" });

    if (m.daily.length > 1) {
      const trend = el("div", null, "two-up");
      const qTable = table(["Day", "Questions", "Errors"], m.daily.map((d) => [d.date, fmt.num(d.questions), fmt.num(d.errors)]), [1, 2]);
      const qFig = figure("Questions per day", null, null, qTable);
      const pTable = table(["Day", "P95"], m.daily.map((d) => [d.date, fmt.ms(d.p95_ms)]), [1]);
      const pFig = figure("Latency P95 per day", "separate chart: different unit", null, pTable);
      trend.append(qFig.fig, pFig.fig);
      lat.append(trend);
      columns(qFig.plot, m.daily.map((d) => ({ label: d.date.slice(5), value: d.questions })), { format: (v) => fmt.num(v), label: "questions" });
      lines(pFig.plot, m.daily.map((d) => d.date.slice(5)), [{ name: "P95", slot: 1, values: m.daily.map((d) => d.p95_ms) }], { format: fmt.ms });
    }

    // Retrieval signals
    const r = m.retrieval;
    const ret = section("Retrieval signals", "Label-free proxies for retrieval health. They cannot tell you if the right document was found (that needs the golden set); they tell you when retrieval behaviour changes.");
    const rt = el("div", null, "tiles");
    rt.append(
      tile("top1_similarity", fmt.score(r.top1_similarity)),
      tile("similarity_margin", fmt.score(r.similarity_margin)),
      tile("source_diversity", fmt.num(r.source_diversity, 1)),
      tile("empty_retrieval_rate", fmt.pct(r.empty_retrieval_rate)),
      tile("rewrite_rate", fmt.pct(r.rewrite_rate)),
      tile("agent_retry_rate", fmt.pct(r.agent_retry_rate)),
    );
    ret.append(rt);
    body.append(ret);

    // Generation & inference
    const g = m.generation;
    const gen = section("Generation and inference");
    const gt = el("div", null, "tiles");
    gt.append(
      tile("input_tokens", fmt.num(g.input_tokens)),
      tile("output_tokens", fmt.num(g.output_tokens)),
      tile("context_tokens", fmt.num(g.context_tokens)),
      tile("output_tps", g.output_tps === null ? "–" : `${fmt.num(g.output_tps, 1)} tok/s`),
      tile("model_calls", fmt.num(g.model_calls, 1)),
      tile("cited_answer_rate", fmt.pct(g.cited_answer_rate)),
      tile("citations_per_answer", fmt.num(g.citations_per_answer, 1)),
      tile("answer_length", `${fmt.num(g.answer_length)} chars`),
    );
    gen.append(gt);
    const tokSteps = m.tokens_by_step.filter((t) => t.step !== "embed");
    if (tokSteps.length) {
      const tokTable = table(["Step", "Questions", "Input / question", "Output / question"], m.tokens_by_step.map((t) => [t.step, fmt.num(t.questions), fmt.num(t.input_per_question), fmt.num(t.output_per_question)]), [1, 2, 3]);
      const tokFig = figure("Model tokens per question, by step", "bar = input, tick = output (same token axis)", [["input", 1, "rect"], ["output", "ink", "tick"]], tokTable);
      gen.append(tokFig.fig);
      body.append(gen);
      hbars(tokFig.plot, tokSteps.map((t) => ({ label: t.step, value: t.input_per_question, tick: t.output_per_question })), { format: (v) => fmt.num(v), barLabel: "input tokens", tickLabel: "output tokens" });
    } else {
      body.append(gen);
    }
    const models = Object.entries(m.models);
    if (models.length) gen.append(table(["Answer model", "Questions"], models.map(([name, n]) => [name, fmt.num(n)]), [1]));

    // By strategy
    const by = section("By strategy", "Same traffic, split by retrieval strategy. Small groups are noisy: compare strategies properly in Offline evaluation.");
    by.append(
      table(
        ["Strategy", "Questions", "Answer rate", "Not found", "Errors", "P50", "P95", "Model calls", "$ / question", "Helpful"],
        m.by_strategy.map((s) => [strategyCell(s.strategy), fmt.num(s.questions), fmt.pct(s.answer_rate), fmt.pct(s.not_found_rate), fmt.pct(s.error_rate), fmt.ms(s.p50_ms), fmt.ms(s.p95_ms), fmt.num(s.model_calls, 1), fmt.usd(s.cost_per_question), fmt.pct(s.helpful_rate)]),
        [1, 2, 3, 4, 5, 6, 7, 8, 9],
      ),
    );
    body.append(by);
  }

  // ---------------------------------------------------------------- offline view

  const RETRIEVAL_KEYS = ["hit@1", "hit@5", "recall@5", "precision@5", "mrr", "ndcg@5"];
  const ANSWER_KEYS = ["correct", "refusal_accuracy", "false_refusal_rate", "citation_hit", "citation_precision"];
  const JUDGE_KEYS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall", "answer_correctness"];

  async function loadOffline() {
    try {
      state.runs = await getJSON("/api/metrics/evals");
      if (!state.runs.length) {
        $("metrics-body").replaceChildren(section("Offline evaluation", "No eval runs yet. Run `make docqa-local-eval` (about 6 minutes locally), then refresh."));
        return;
      }
      const comparing = state.runs.find((r) => r.strategies.length > 1);
      const runId = state.run ? state.run.run_id : (comparing || state.runs[0]).run_id;
      state.run = await getJSON(`/api/metrics/evals/${encodeURIComponent(runId)}`);
      renderOffline();
      loadTrend();
    } catch (err) {
      if (err.message !== "signed out") $("metrics-body").replaceChildren(el("p", `Could not load eval runs: ${err.message}`, "error-text"));
    }
  }

  function headerWithInfo(key) {
    const th = el("span", null, "th-info");
    const meta = state.glossary[key];
    th.append(document.createTextNode(meta ? meta.name.replace(/ \(.*\)$/, "") : key));
    const i = info(key);
    if (i) th.append(i);
    return th;
  }

  function metricTable(summaries, keys, group, invert = []) {
    const t = el("table", null, "data-table compare");
    const head = el("tr");
    head.append(el("th", "Strategy"));
    for (const key of keys) {
      const th = el("th", null, "num");
      th.append(headerWithInfo(key));
      head.append(th);
    }
    t.append(el("thead"));
    t.tHead.append(head);
    const body = el("tbody");
    for (const s of summaries) {
      const tr = el("tr");
      const first = el("td");
      first.append(strategyCell(s.strategy));
      tr.append(first);
      for (const key of keys) {
        const td = el("td", null, "num");
        const value = s[group] ? s[group][key] : null;
        td.append(meterCell(value, STRATEGY_SLOT[s.strategy] || 1));
        if (invert.includes(key)) td.title = "lower is better";
        tr.append(td);
      }
      body.append(tr);
    }
    t.append(body);
    return t;
  }

  function renderOffline() {
    const body = $("metrics-body");
    body.replaceChildren();
    const run = state.run;

    const top = section("Offline evaluation", "Quality measured against the golden set, where the right answers and their evidence are known. These are the numbers to compare strategies, models and settings on; produced by `make docqa-local-eval`.");
    const picker = el("div", null, "run-picker");
    const label = el("label", "Eval run");
    label.htmlFor = "run-select";
    const select = el("select");
    select.id = "run-select";
    for (const r of state.runs) {
      const opt = el("option", `${r.meta.date || r.run_id} · ${r.strategies.join(", ")} · ${r.meta["generation model"] || ""}`);
      opt.value = r.run_id;
      if (r.run_id === run.run_id) opt.selected = true;
      select.append(opt);
    }
    select.addEventListener("change", async () => {
      state.run = await getJSON(`/api/metrics/evals/${encodeURIComponent(select.value)}`);
      renderOffline();
      loadTrend();
    });
    picker.append(label, select);
    top.append(picker);
    const meta = el("dl", null, "run-meta");
    for (const [key, value] of Object.entries(run.meta)) {
      meta.append(el("dt", key), el("dd", value));
    }
    top.append(meta);
    body.append(top);

    const s = run.summaries;
    const ret = section("Retrieval quality", "Did the right evidence reach the model? hit@5 bounds answer quality: evidence that is not retrieved cannot be used.");
    ret.append(metricTable(s, RETRIEVAL_KEYS, "retrieval"));
    body.append(ret);

    if (s.some((x) => x.answers && Object.keys(x.answers).length)) {
      const ans = section("Answer quality", "Deterministic checks against expected facts, abstentions and citations.");
      ans.append(metricTable(s, ANSWER_KEYS, "answers", ["false_refusal_rate"]));
      body.append(ans);
    }
    if (s.some((x) => x.judge && Object.keys(x.judge).length)) {
      const judge = section("LLM judge (RAGAS)", "Scored by a language model: tolerant of wording, but biased and noisy. Trust large differences, spot-check small ones.");
      judge.append(metricTable(s, JUDGE_KEYS.filter((k) => s.some((x) => x.judge && x.judge[k] !== undefined)), "judge"));
      body.append(judge);
    }

    const perf = section("Latency and cost in the eval run");
    perf.append(
      table(
        ["Strategy", "Questions", "Errors", "Retrieval P50", "Retrieval P95", "Total P50", "Total P95", "$ / question"],
        s.map((x) => [strategyCell(x.strategy), fmt.num(x.questions), fmt.num(x.errors), fmt.ms(x.latency_ms.retrieval_p50), fmt.ms(x.latency_ms.retrieval_p95), fmt.ms(x.latency_ms.total_p50), fmt.ms(x.latency_ms.total_p95), fmt.usd(x.cost_usd.per_question)]),
        [1, 2, 3, 4, 5, 6, 7],
      ),
    );
    body.append(perf);

    const cats = [...new Set(s.flatMap((x) => Object.keys(x.by_category || {})))].sort();
    if (cats.length) {
      const bycat = section("By question category", "Correct answers per category: where each strategy wins or fails (paraphrases, tables, multi-document, unanswerable).");
      bycat.append(
        table(
          ["Strategy", ...cats],
          s.map((x) => [strategyCell(x.strategy), ...cats.map((c) => {
            const v = x.by_category[c] || {};
            const val = v.correct !== undefined && v.correct !== null ? v.correct : v["hit@5"];
            return meterCell(val === undefined ? null : val, STRATEGY_SLOT[x.strategy] || 1);
          })]),
          cats.map((_, i) => i + 1),
        ),
      );
      body.append(bycat);
    }

    const trend = section("Across runs", "How a metric moved between eval runs (oldest left). Differences under ~2-3 points on 49 questions are noise.");
    const chooser = el("div", null, "run-picker");
    const tl = el("label", "Metric");
    tl.htmlFor = "trend-metric";
    const ts = el("select");
    ts.id = "trend-metric";
    for (const key of [...ANSWER_KEYS.slice(0, 1), ...RETRIEVAL_KEYS]) {
      const o = el("option", (state.glossary[key] || { name: key }).name);
      o.value = key;
      if (key === state.trendMetric) o.selected = true;
      ts.append(o);
    }
    ts.addEventListener("change", () => {
      state.trendMetric = ts.value;
      loadTrend();
    });
    chooser.append(tl, ts);
    trend.append(chooser);
    const holder = el("div");
    holder.id = "trend-holder";
    trend.append(holder);
    body.append(trend);
  }

  async function loadTrend() {
    const holder = $("trend-holder");
    if (!holder) return;
    const key = state.trendMetric;
    const group = RETRIEVAL_KEYS.includes(key) ? "retrieval" : "answers";
    const runs = [...state.runs].slice(0, 12).reverse(); // oldest first
    const details = await Promise.all(runs.map((r) => getJSON(`/api/metrics/evals/${encodeURIComponent(r.run_id)}`).catch(() => null)));
    const valid = details.filter((d) => d && d.summaries.some((s) => s[group] && s[group][key] !== undefined && s[group][key] !== null));
    if (!valid.length) {
      holder.replaceChildren(el("p", "No run has this metric yet.", "muted"));
      return;
    }
    const strategies = Object.keys(STRATEGY_SLOT).filter((st) => valid.some((d) => d.summaries.some((s) => s.strategy === st)));
    const xs = valid.map((d) => (d.meta.date || d.run_id).slice(5, 16));
    const series = strategies.map((st) => ({
      name: st,
      slot: STRATEGY_SLOT[st],
      values: valid.map((d) => {
        const s = d.summaries.find((x) => x.strategy === st);
        const v = s && s[group] ? s[group][key] : null;
        return v === undefined ? null : v;
      }),
    }));
    const tbl = table(["Run", ...strategies], valid.map((d, i) => [xs[i], ...series.map((s) => fmt.score(s.values[i]))]), strategies.map((_, i) => i + 1));
    const meta = state.glossary[key] || { name: key };
    const fig = figure(meta.name, "per eval run", strategies.map((st) => [st, STRATEGY_SLOT[st], "line"]), tbl);
    holder.replaceChildren(fig.fig);
    lines(fig.plot, xs, series, { format: (v) => (v === null || v === undefined ? "–" : Number(v).toFixed(2)), maxValue: 1 });
  }

  // ---------------------------------------------------------------- guide

  function renderGuide() {
    const body = $("metrics-body");
    body.replaceChildren();
    const intro = section("Metric guide", "Every number on this dashboard, what it means and when to act. Online metrics come from real traffic (no answer key); offline metrics are scored against the golden set. The full write-up, with the concepts behind them, is documents/07-ai-metrics.md.");
    body.append(intro);
    const groups = {};
    for (const [key, meta] of Object.entries(state.glossary)) {
      (groups[meta.group] = groups[meta.group] || []).push([key, meta]);
    }
    for (const [group, entries] of Object.entries(groups)) {
      const sec = section(group);
      for (const [, meta] of entries) {
        const card = el("div", null, "guide-card");
        const h = el("h3", meta.name);
        h.append(el("span", meta.kind, `badge ${meta.kind}`));
        card.append(h, el("p", meta.definition));
        const formula = el("p", null, "formula");
        formula.append(el("span", "Formula ", "muted"), el("code", meta.formula));
        card.append(formula, el("p", `Why it matters: ${meta.why}`));
        if (meta.caveat) card.append(el("p", `Caveat: ${meta.caveat}`, "caveat"));
        sec.append(card);
      }
      body.append(sec);
    }
  }

  // ---------------------------------------------------------------- entry point

  async function show(sub) {
    hideTip();
    state.sub = ["live", "offline", "guide"].includes(sub) ? sub : "live";
    for (const a of document.querySelectorAll(".subtabs a")) {
      a.classList.toggle("active", a.dataset.sub === state.sub);
      if (a.dataset.sub === state.sub) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    $("live-filters").hidden = state.sub !== "live";
    if (!state.glossary) {
      try {
        state.glossary = await getJSON("/api/metrics/glossary");
      } catch (err) {
        if (err.message === "signed out") return;
        state.glossary = {};
      }
    }
    if (state.sub === "live") await loadLive();
    else if (state.sub === "offline") await loadOffline();
    else renderGuide();
  }

  function paintWindow() {
    for (const b of document.querySelectorAll("[data-window]")) b.setAttribute("aria-pressed", String(b.dataset.window === state.window));
  }

  document.addEventListener("DOMContentLoaded", () => {
    paintWindow();
    for (const b of document.querySelectorAll("[data-window]")) {
      b.addEventListener("click", () => {
        state.window = b.dataset.window;
        paintWindow();
        loadLive();
      });
    }
    $("metrics-strategy").addEventListener("change", (e) => {
      state.strategy = e.target.value;
      loadLive();
    });
    $("metrics-refresh").addEventListener("click", () => show(state.sub));
    let timer = null;
    window.addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if ($("view-metrics").hidden) return;
        if (state.sub === "live" && state.live) renderLive();
        else if (state.sub === "offline" && state.run) {
          renderOffline();
          loadTrend();
        }
      }, 200);
    });
  });

  return { show };
})();
