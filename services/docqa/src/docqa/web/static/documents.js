// docqa: Documents tab. Every document joined across raw/, ingest status and the index, polled
// while the tab is open (every 2 s while something is ingesting, every 10 s otherwise).
// All document text and names are inserted with textContent, never as HTML.
// Routes: #/documents  #/documents/<doc id>
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const FAST_MS = 2000;
  const SLOW_MS = 10000;

  // Ingestion steps in order, with what each one does (shown in the timeline and the guide).
  const STAGES = [
    ["read", "Read", "Fetch the file and hash it; the same bytes reuse the cached parse."],
    ["parse", "Parse", "PDF text layer per page; scanned pages and images go to the vision model."],
    ["metadata", "Metadata", "An LLM extracts type, title, institution, person and date."],
    ["chunk", "Chunk", "Split into retrieval units along headings and table rows (~400 tokens)."],
    ["embed", "Embed", "One vector per chunk, from the chunk plus a context header."],
    ["index", "Index", "Replace this document's rows in LanceDB; rebuild the BM25 index."],
  ];
  const STAGE_LABEL = Object.fromEntries(STAGES.map(([key, label]) => [key, label]));

  // State -> icon, label and status tone. The icon and label carry the meaning; color only
  // reinforces it.
  const STATES = {
    processing: ["⟳", "Processing", "info"],
    stalled: ["⚠", "Stalled", "serious"],
    failed: ["✕", "Failed", "critical"],
    outdated: ["↻", "Outdated", "warning"],
    pending: ["○", "Pending", "neutral"],
    orphaned: ["⚠", "Orphaned", "warning"],
    unsupported: ["–", "Skipped", "neutral"],
    indexed: ["✓", "Indexed", "good"],
  };
  const FILTERS = [
    ["all", "All", () => true],
    ["active", "In progress", (d) => d.state === "processing"],
    ["attention", "Needs attention", (d) => ["failed", "stalled", "outdated", "orphaned", "pending"].includes(d.state)],
    ["indexed", "Indexed", (d) => d.state === "indexed"],
  ];

  const view = {
    shown: false,
    timer: null,
    filter: "all",
    selected: null, // doc id from the route
    overview: null,
    detailKey: "", // what the open detail was rendered from; re-rendered only when it changes
    inflight: false,
  };

  // ---------------------------------------------------------------- helpers

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function bytes(n) {
    if (n === null || n === undefined) return "–";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function duration(ms) {
    if (ms === null || ms === undefined) return "–";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
    return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
  }

  function ago(iso) {
    if (!iso) return "–";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
  }

  function exact(iso) {
    return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }) : "";
  }

  function fileName(key) {
    return key.split("/").pop();
  }

  // Wall time of the last attempt, including a step that failed part-way.
  function totalMs(status) {
    if (!status || !status.finished_at) return null;
    return new Date(status.finished_at) - new Date(status.started_at);
  }

  function badge(state) {
    const [icon, label, tone] = STATES[state] || ["?", state, "neutral"];
    const node = el("span", null, `state-badge tone-${tone}`);
    const glyph = el("span", icon, `state-icon${state === "processing" ? " spin" : ""}`);
    glyph.setAttribute("aria-hidden", "true");
    node.append(glyph, el("span", label));
    return node;
  }

  function progressText(status) {
    if (!status || !status.stage) return "";
    const p = status.progress || {};
    const label = STAGE_LABEL[status.stage] || status.stage;
    if (p.total) {
      const unit = status.stage === "parse" ? "pages" : status.stage === "embed" ? "chunks" : "";
      return `${label}: ${p.done} of ${p.total}${unit ? ` ${unit}` : ""}`;
    }
    return `${label}…`;
  }

  function meter(done, total) {
    const track = el("span", null, "meter-track doc-meter");
    const fill = el("span", null, "meter-fill");
    fill.style.width = `${total ? Math.min(100, (done / total) * 100) : 0}%`;
    track.append(fill);
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(total || 0));
    track.setAttribute("aria-valuenow", String(done || 0));
    return track;
  }

  function tile(label, value, sub) {
    const card = el("div", null, "tile");
    const head = el("div", null, "tile-head");
    head.append(el("span", label, "tile-label"));
    card.append(head, el("div", value, "tile-value"));
    if (sub) card.append(el("div", sub, "tile-sub"));
    return card;
  }

  // ---------------------------------------------------------------- data

  async function load() {
    if (view.inflight) return;
    view.inflight = true;
    try {
      const response = await fetch("/api/documents", { headers: { Accept: "application/json" } });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        showError((data && data.detail) || "Could not load the documents.");
        return;
      }
      view.overview = data;
      renderOverview();
      await renderDetail();
    } catch {
      showError("Could not reach the server. Retrying…");
    } finally {
      view.inflight = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(view.timer);
    if (!view.shown || document.hidden) return;
    const fast = view.overview && view.overview.active;
    view.timer = setTimeout(load, fast ? FAST_MS : SLOW_MS);
    const live = $("docs-live");
    live.classList.toggle("active", Boolean(fast));
    live.textContent = fast ? "Live · ingesting" : "Live";
    live.title = `Refreshes every ${(fast ? FAST_MS : SLOW_MS) / 1000} s while this tab is open`;
  }

  function showError(message) {
    $("docs-updated").textContent = "";
    const box = $("docs-overview");
    if (!view.overview) box.replaceChildren(el("p", message, "error-text"));
    else $("docs-updated").textContent = message;
  }

  // ---------------------------------------------------------------- overview

  function renderOverview() {
    const o = view.overview;
    $("docs-updated").textContent = `Updated ${new Date(o.generated_at).toLocaleTimeString()}`;
    $("docs-table").textContent = o.index_table;
    const box = $("docs-overview");
    const nodes = [];

    const c = o.counts;
    const attention = c.failed + c.stalled + c.outdated + c.orphaned + c.pending;
    const tiles = el("div", null, "tiles");
    const searchable = c.indexed + c.outdated + c.orphaned; // these still have chunks in the index
    tiles.append(
      tile("Documents", String(o.totals.documents), bytes(o.totals.bytes)),
      tile("Indexed", String(c.indexed), `${o.totals.chunks} chunks searchable`),
      tile("In progress", String(c.processing), c.processing ? "ingesting now" : "idle"),
      tile(
        "Needs attention",
        String(attention),
        [["failed", c.failed + c.stalled], ["pending", c.pending], ["outdated", c.outdated], ["orphaned", c.orphaned]]
          .filter(([, n]) => n)
          .map(([k, n]) => `${n} ${k}`)
          .join(" · ") || "nothing",
      ),
      tile("Pages", String(o.totals.pages), `${o.totals.vision_pages} read by the vision model`),
    );
    nodes.push(tiles);
    if (searchable !== c.indexed) {
      nodes.push(
        el("p", "Outdated and orphaned documents are still searchable: answers can cite a version that no longer matches the file.", "m-note docs-warning"),
      );
    }

    // Filter
    const bar = el("div", null, "docs-filter");
    const seg = el("div", null, "segmented");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Show");
    for (const [key, label, test] of FILTERS) {
      const n = o.documents.filter(test).length;
      const button = el("button", `${label} (${n})`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(view.filter === key));
      button.addEventListener("click", () => {
        view.filter = key;
        renderOverview();
      });
      seg.append(button);
    }
    bar.append(seg);
    nodes.push(bar);

    const test = FILTERS.find(([key]) => key === view.filter)[2];
    const rows = o.documents.filter(test);
    if (!o.documents.length) {
      nodes.push(emptyState(o));
    } else if (!rows.length) {
      nodes.push(el("p", "No documents in this view.", "empty"));
    } else {
      nodes.push(table(rows));
    }
    box.replaceChildren(...nodes);
  }

  function emptyState(o) {
    const box = el("div", null, "empty");
    box.append(el("p", "No documents yet."), howTo(o, true));
    return box;
  }

  function howTo(o, open) {
    const d = el("details", null, "docs-howto");
    d.open = Boolean(open);
    d.append(el("summary", "Add or update documents"));
    const p1 = el("p");
    p1.append("Put PDF, PNG or JPEG files in ", el("code", o.source), ".");
    const p2 = el("p", o.ingest_hint);
    const p3 = el(
      "p",
      "Replacing a file with a new version re-ingests it (the old chunks stay searchable until then). Deleting a file removes its chunks: automatically when deployed, with make docqa-local-prune locally.",
      "muted",
    );
    d.append(p1, p2, p3);
    return d;
  }

  function table(rows) {
    const wrap = el("div", null, "table-wrap-x");
    const t = el("table", null, "data-table docs-table");
    const head = el("tr");
    for (const [label, cls] of [
      ["Document", ""], ["Status", ""], ["Details", ""], ["Type", ""],
      ["Chunks", "num"], ["Pages", "num"], ["Size", "num"], ["Ingested", ""], ["Took", "num"],
    ]) head.append(el("th", label, cls)); // prettier-ignore
    const thead = el("thead");
    thead.append(head);
    const body = el("tbody");
    for (const d of rows) body.append(row(d));
    t.append(thead, body);
    wrap.append(t);
    return wrap;
  }

  function row(d) {
    const s = d.status;
    const ix = d.indexed;
    const tr = el("tr", null, d.doc_id === view.selected ? "selected" : "");
    const name = el("td", null, "doc-name");
    const a = el("a", (ix && ix.title) || fileName(d.source_key));
    a.href = `#/documents/${d.doc_id}`;
    name.append(a, el("div", d.source_key, "mono muted doc-key"));

    const details = el("td", null, "doc-details");
    if (d.state === "processing") {
      details.append(el("div", progressText(s)));
      if (s.progress && s.progress.total) details.append(meter(s.progress.done, s.progress.total));
    } else if (d.state !== "indexed") {
      details.append(el("div", d.reason, d.state === "failed" || d.state === "stalled" ? "error-inline" : "muted"));
    } else if (ix) {
      details.append(el("div", [ix.institution, ix.date].filter(Boolean).join(" · "), "muted"));
    }

    const pagesCell = el("td", ix ? String(ix.pages) : s && s.pages !== null ? String(s.pages) : "–", "num nowrap");
    if (s && s.vision_pages) pagesCell.append(el("div", `${s.vision_pages} vision`, "muted small"));

    const ingested = el("td", s && s.finished_at ? ago(s.finished_at) : "–", "nowrap");
    if (s && s.finished_at) ingested.title = exact(s.finished_at);

    tr.append(
      name,
      wrapTd(badge(d.state)),
      details,
      el("td", ix ? ix.doc_type : "–", "mono"),
      el("td", ix ? String(ix.chunks) : "–", "num"),
      pagesCell,
      el("td", bytes(d.size), "num nowrap"),
      ingested,
      el("td", duration(totalMs(s)), "num nowrap"),
    );
    return tr;
  }

  function wrapTd(node) {
    const td = el("td");
    td.append(node);
    return td;
  }

  // ---------------------------------------------------------------- detail

  async function renderDetail() {
    const box = $("docs-detail");
    if (!view.selected) {
      box.hidden = true;
      view.detailKey = "";
      return;
    }
    const row = view.overview && view.overview.documents.find((d) => d.doc_id === view.selected);
    // Re-fetch only when something changed, so open chunk text stays open while polling.
    const key = row ? `${row.state}|${row.status ? row.status.updated_at : ""}|${row.indexed ? row.indexed.content_hash : ""}` : "missing";
    if (key === view.detailKey) return;
    view.detailKey = key;
    box.hidden = false;
    if (!row) {
      box.replaceChildren(el("p", "That document is no longer in raw/ or the index.", "empty"));
      return;
    }
    const response = await fetch(`/api/documents/${encodeURIComponent(view.selected)}`);
    const data = await response.json().catch(() => null);
    if (!response.ok || !data) {
      box.replaceChildren(el("p", "Could not load this document.", "error-text"));
      return;
    }
    box.replaceChildren(...detailNodes(data));
  }

  function detailNodes(data) {
    const d = data.document;
    const s = d.status;
    const ix = d.indexed;
    const nodes = [];

    const head = el("div", null, "doc-detail-head");
    head.append(el("h2", (ix && ix.title) || fileName(d.source_key)), badge(d.state));
    const close = el("a", "Close", "doc-close");
    close.href = "#/documents";
    head.append(close);
    nodes.push(head, el("p", d.reason, d.state === "failed" || d.state === "stalled" ? "error-inline" : "m-note"));

    const dl = el("dl", null, "run-meta doc-meta");
    for (const [k, v] of [
      ["file", d.source_key],
      ["doc id", d.doc_id],
      ["type", ix && ix.doc_type],
      ["institution", ix && ix.institution],
      ["person", ix && ix.person],
      ["date", ix && ix.date],
      ["size", d.size !== null ? bytes(d.size) : ""],
      ["file modified", exact(d.modified)],
      ["ingest started", s && exact(s.started_at)],
      ["ingest finished", s && exact(s.finished_at)],
      ["content hash", (s && s.content_hash) || (ix && ix.content_hash) ? `${((s && s.content_hash) || ix.content_hash).slice(0, 12)}…` : ""],
      ["index table", (s && s.index_table) || view.overview.index_table],
    ]) {
      if (!v) continue;
      dl.append(el("dt", k), el("dd", v));
    }
    nodes.push(dl);

    nodes.push(section("Ingestion timeline", s ? "Each step of the last ingest and how long it took." : "No ingest record: this document was indexed before status tracking existed. Ingest it again to see its timeline."));
    if (s) nodes.push(timeline(s));

    const chunks = data.chunks;
    nodes.push(
      section(
        `Chunks (${chunks.length})`,
        "What retrieval searches. BM25 matches the text; the embedding was made from the context header plus the text, so the vector also knows which document it came from.",
      ),
    );
    if (chunks.length) nodes.push(chunkTable(chunks));
    else nodes.push(el("p", ix ? "No chunks." : "Not in the index yet.", "muted"));
    return nodes;
  }

  function section(title, note) {
    const box = el("div", null, "m-section");
    box.append(el("h2", title));
    if (note) box.append(el("p", note, "m-note"));
    return box;
  }

  function timeline(s) {
    const list = el("ol", null, "stage-list");
    const max = Math.max(1, ...Object.values(s.timings_ms));
    const reached = s.stage ? STAGES.findIndex(([key]) => key === s.stage) : STAGES.length;
    STAGES.forEach(([key, label, about], i) => {
      const ms = s.timings_ms[key];
      let mark = "pending";
      if (ms !== undefined) mark = "done";
      else if (key === s.stage) mark = s.state === "failed" ? "failed" : "running";
      // Parse and metadata leave no timing when the cached parse was reused.
      else if ((key === "parse" || key === "metadata") && i < reached) mark = "skipped";

      const li = el("li", null, `stage stage-${mark}`);
      const icon = { done: "✓", failed: "✕", running: "⟳", skipped: "↷", pending: "○" }[mark];
      const glyph = el("span", icon, `stage-icon${mark === "running" ? " spin" : ""}`);
      glyph.setAttribute("aria-hidden", "true");
      const name = el("span", label, "stage-name");
      const bar = el("span", null, "stage-track");
      if (ms !== undefined) {
        const fill = el("span", null, "stage-fill");
        fill.style.width = `max(2px, ${(ms / max) * 100}%)`;
        bar.append(fill);
      } else if (mark === "running" && s.progress && s.progress.total) {
        bar.append(meter(s.progress.done, s.progress.total));
      }
      let value = ms !== undefined ? duration(ms) : "";
      if (mark === "running") value = progressText(s);
      if (mark === "skipped") value = "cached parse reused";
      if (mark === "failed") value = "failed here";
      if (mark === "pending") value = s.state === "failed" ? "not reached" : "";
      li.append(glyph, name, bar, el("span", value, "stage-value mono"));
      li.title = about;
      li.append(el("span", about, "stage-about"));
      list.append(li);
    });
    const box = el("div", null, "timeline-box");
    box.append(list);
    if (s.error) box.append(el("pre", s.error, "stage-error"));
    return box;
  }

  function chunkTable(chunks) {
    const wrap = el("div", null, "table-wrap-x");
    const t = el("table", null, "data-table chunk-table");
    const thead = el("thead");
    const head = el("tr");
    for (const [label, cls] of [["#", "num"], ["Pages", ""], ["~Tokens", "num"], ["Text", ""]]) head.append(el("th", label, cls));
    thead.append(head);
    const body = el("tbody");
    for (const c of chunks) {
      const tr = el("tr");
      const text = el("td");
      const details = el("details");
      details.append(el("summary", c.text.replace(/\s+/g, " ").slice(0, 140)));
      const header = c.embed_text.endsWith(c.text) ? c.embed_text.slice(0, c.embed_text.length - c.text.length).trim() : "";
      if (header) {
        const h = el("p", null, "embed-header");
        h.append(el("span", "Context header (embedded, not shown to BM25): ", "muted"), el("span", header, "mono"));
        details.append(h);
      }
      details.append(el("pre", c.text));
      text.append(details);
      tr.append(
        el("td", String(c.ordinal + 1), "num"),
        el("td", c.page_start === c.page_end ? `p${c.page_start}` : `pp${c.page_start}-${c.page_end}`, "nowrap"),
        el("td", String(Math.round(c.text.length / 4)), "num"),
        text,
      );
      body.append(tr);
    }
    t.append(thead, body);
    wrap.append(t);
    return wrap;
  }

  // ---------------------------------------------------------------- entry points

  function show(id) {
    const changed = view.selected !== (id || null);
    view.selected = id || null;
    if (changed) view.detailKey = "";
    if (!view.shown) {
      view.shown = true;
      if (!view.overview) $("docs-overview").replaceChildren(el("p", "Loading…", "muted"));
    }
    if (view.overview) {
      renderOverview();
      renderDetail();
      $("docs-howto-slot").replaceChildren(howTo(view.overview, false));
    }
    load().then(() => {
      if (view.overview) $("docs-howto-slot").replaceChildren(howTo(view.overview, false));
      if (id) $("docs-detail").scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }

  function hide() {
    view.shown = false;
    clearTimeout(view.timer);
  }

  document.addEventListener("visibilitychange", () => {
    if (!view.shown) return;
    if (document.hidden) clearTimeout(view.timer);
    else load();
  });

  document.addEventListener("DOMContentLoaded", () => {
    $("docs-refresh").addEventListener("click", () => load());
  });

  window.DocqaDocuments = { show, hide };
})();
