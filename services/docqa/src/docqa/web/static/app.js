// docqa: conversations (Chat tab) and per-question traces (Traces tab).
// All model and document text is inserted with textContent, never as HTML.
// Routes: #/  (new conversation)  #/c/<conversation id>  #/traces  #/t/<trace id>
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  conversations: [], // summaries, newest first
  conversation: null, // the open conversation (full), or null for a new one
  busy: false,
  emptyThread: null, // the hint shown in a new conversation (kept to re-insert)
};

// ------------------------------------------------------------------ helpers

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function link(href, text, className) {
  const a = el("a", text, className);
  a.href = href;
  return a;
}

function pages(c) {
  return c.page_start === c.page_end ? `p${c.page_start}` : `pp${c.page_start}-${c.page_end}`;
}

function fileName(key) {
  return key.split("/").pop();
}

function sourceName(c) {
  return c.title || fileName(c.source_key);
}

function ms(value) {
  return `${Math.round(value).toLocaleString()} ms`;
}

function usd(value) {
  return `$${value.toFixed(6)}`;
}

function when(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("signed out");
  }
  if (response.status === 204) return { response, data: null };
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return { response, data };
}

function errorDetail(data, fallback) {
  return data && typeof data.detail === "string" ? data.detail : fallback;
}

// ------------------------------------------------------------------ routing

function route() {
  const hash = window.location.hash || "#/";
  const [, kind, id] = hash.match(/^#\/(c|t|traces|metrics)?\/?(.*)$/) || [];
  const traces = kind === "t" || kind === "traces";
  const metrics = kind === "metrics";
  $("view-chat").hidden = traces || metrics;
  $("view-traces").hidden = !traces;
  $("view-metrics").hidden = !metrics;
  $("tab-chat").classList.toggle("active", !traces && !metrics);
  $("tab-traces").classList.toggle("active", traces);
  $("tab-metrics").classList.toggle("active", metrics);
  const tip = $("viz-tip");
  if (tip) tip.hidden = true;
  if (metrics) {
    window.DocqaMetrics.show(id || "live");
    return;
  }
  $("tab-chat").href = state.conversation ? `#/c/${state.conversation.conversation_id}` : "#/";

  if (kind === "c" && id) openConversation(id);
  else if (kind === "t" && id) openTrace(decodeURIComponent(id));
  else if (kind === "traces") openLatestTrace();
  else if (!kind) newConversation();
}

// ------------------------------------------------------------------ chat: sidebar

async function loadConversations() {
  const { response, data } = await api("/api/conversations");
  if (!response.ok) return;
  state.conversations = data;
  renderConversationList();
}

function renderConversationList() {
  const list = $("conversation-list");
  list.replaceChildren();
  if (!state.conversations.length) {
    list.append(el("li", "No conversations yet.", "muted"));
    return;
  }
  for (const c of state.conversations) {
    const item = el("li");
    const a = link(`#/c/${c.conversation_id}`, null, "conversation-link");
    a.append(el("span", c.title, "title"), el("span", `${when(c.updated_at)} · ${c.turns} Q`, "meta"));
    if (state.conversation && state.conversation.conversation_id === c.conversation_id) {
      a.classList.add("active");
      a.setAttribute("aria-current", "page");
    }
    item.append(a);
    list.append(item);
  }
}

// ------------------------------------------------------------------ chat: thread

function newConversation() {
  state.conversation = null;
  renderThread();
  renderConversationList();
  $("question").focus();
}

async function openConversation(id) {
  if (state.conversation && state.conversation.conversation_id === id) {
    renderThread();
    renderConversationList();
    return;
  }
  $("status").textContent = "Loading…";
  const { response, data } = await api(`/api/conversations/${encodeURIComponent(id)}`);
  $("status").textContent = "";
  if (!response.ok) {
    $("status").textContent = errorDetail(data, "Could not open that conversation.");
    state.conversation = null;
    renderThread();
    return;
  }
  state.conversation = data;
  renderThread();
  renderConversationList();
}

function renderThread() {
  const c = state.conversation;
  $("conversation-title").textContent = c ? c.title : "New conversation";
  $("conversation-id").textContent = c ? c.conversation_id : "";
  $("delete-conversation").hidden = !c;
  $("tab-chat").href = c ? `#/c/${c.conversation_id}` : "#/";
  const thread = $("thread");
  thread.replaceChildren();
  if (!c || !c.turns.length) {
    thread.append(state.emptyThread);
    return;
  }
  for (const turn of c.turns) appendTurn(thread, turn);
  thread.scrollTop = thread.scrollHeight;
}

function appendTurn(thread, turn) {
  thread.append(el("div", turn.question, "message user"));
  thread.append(turn.result ? answerBubble(turn) : errorBubble(turn.turn_id, turn.error));
}

// "GPA 3.86 [1]." -> text nodes plus <sup> citation markers.
function renderAnswer(container, text, cited) {
  for (const part of text.split(/(\[\d+\])/)) {
    const match = part.match(/^\[(\d+)\]$/);
    if (match && cited.has(Number(match[1]))) container.append(el("sup", match[1], "cite"));
    else container.append(document.createTextNode(part));
  }
}

function answerBubble(turn) {
  const r = turn.result;
  const bubble = el("div", null, "message assistant");
  if (r.not_found) bubble.classList.add("not-found");
  const body = el("div", null, "answer");
  renderAnswer(body, r.answer, new Set(r.citations.map((c) => c.number)));
  bubble.append(body);

  if (r.citations.length) {
    const sources = el("ol", null, "citations");
    for (const c of r.citations) {
      const li = el("li", `${sourceName(c)} · ${pages(c)}`);
      li.value = c.number;
      li.title = c.source_key;
      sources.append(li);
    }
    bubble.append(sources);
  }
  if (r.standalone_question && r.standalone_question !== turn.question) {
    bubble.append(el("p", `Searched as: “${r.standalone_question}”`, "rewrite muted"));
  }
  const meta = el("p", null, "meta muted");
  meta.append(
    document.createTextNode(`${r.strategy} · ${ms(turn.trace.duration_ms)} · ${usd(r.cost.usd)} · `),
    link(`#/t/${turn.turn_id}`, `trace ${turn.turn_id}`, "mono"),
    feedbackButtons(turn),
  );
  bubble.append(meta);
  return bubble;
}

// Thumbs up/down: the online usefulness signal. Clicking the active rating clears it.
function feedbackButtons(turn) {
  const box = el("span", null, "feedback");
  const buttons = {};
  const paint = () => {
    for (const [rating, button] of Object.entries(buttons)) {
      button.setAttribute("aria-pressed", String(turn.feedback === rating));
    }
  };
  for (const [rating, glyph, label] of [["up", "👍", "Helpful"], ["down", "👎", "Not helpful"]]) {
    const button = el("button", glyph, "thumb");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.addEventListener("click", async () => {
      const next = turn.feedback === rating ? null : rating;
      const { response } = await api(`/api/traces/${encodeURIComponent(turn.turn_id)}/feedback`, {
        method: "PUT",
        body: JSON.stringify({ rating: next }),
      });
      if (response.ok) {
        turn.feedback = next;
        paint();
      }
    });
    buttons[rating] = button;
    box.append(button);
  }
  paint();
  return box;
}

function errorBubble(traceId, message) {
  const bubble = el("div", null, "message assistant error");
  bubble.append(el("div", message || "Something went wrong.", "answer"));
  if (traceId) {
    const meta = el("p", null, "meta muted");
    meta.append(document.createTextNode("See what failed: "), link(`#/t/${traceId}`, `trace ${traceId}`, "mono"));
    bubble.append(meta);
  }
  return bubble;
}

async function ask(event) {
  event.preventDefault();
  if (state.busy) return;
  const question = $("question").value.trim();
  if (!question) return;
  state.busy = true;
  $("ask-button").disabled = true;
  $("status").textContent = "";

  const thread = $("thread");
  if (!state.conversation || !state.conversation.turns.length) thread.replaceChildren();
  const pending = [el("div", question, "message user"), el("div", "Thinking…", "message assistant pending")];
  thread.append(...pending);
  thread.scrollTop = thread.scrollHeight;

  try {
    const { response, data } = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        strategy: $("strategy").value,
        conversation_id: state.conversation ? state.conversation.conversation_id : null,
      }),
    });
    pending.forEach((node) => node.remove());
    if (!response.ok) {
      const traceId = response.headers.get("X-Docqa-Trace-Id");
      if (traceId) {
        // The failed turn was saved: reload the conversation so it shows with its trace.
        const conversationId = traceId.split(".")[0];
        state.conversation = null;
        await loadConversations();
        await openConversation(conversationId);
        setHash(`#/c/${conversationId}`);
      }
      thread.append(errorBubble(traceId, errorDetail(data, "The request was rejected.")));
      thread.scrollTop = thread.scrollHeight;
      return;
    }
    $("question").value = "";
    if (!state.conversation) {
      state.conversation = { ...data.conversation, owner: "", turns: [] };
    }
    state.conversation.turns.push(data.turn);
    Object.assign(state.conversation, { title: data.conversation.title, updated_at: data.conversation.updated_at });
    setHash(`#/c/${data.conversation.conversation_id}`);
    renderThread();
    await loadConversations();
  } catch (err) {
    pending.forEach((node) => node.remove());
    if (err.message !== "signed out") thread.append(errorBubble(null, "Could not reach the server."));
  } finally {
    state.busy = false;
    $("ask-button").disabled = false;
    $("question").focus();
  }
}

async function deleteConversation() {
  const c = state.conversation;
  if (!c || !window.confirm(`Delete “${c.title}” and its traces?`)) return;
  const { response, data } = await api(`/api/conversations/${c.conversation_id}`, { method: "DELETE" });
  if (!response.ok) {
    $("status").textContent = errorDetail(data, "Could not delete the conversation.");
    return;
  }
  state.conversation = null;
  await loadConversations();
  window.location.hash = "#/";
}

// Change the address without re-running the router for a view that is already shown.
function setHash(hash) {
  if (window.location.hash !== hash) history.pushState(null, "", hash);
  $("tab-chat").href = hash.startsWith("#/c/") ? hash : $("tab-chat").href;
}

// ------------------------------------------------------------------ traces

async function openLatestTrace() {
  const c = state.conversation;
  if (c && c.turns.length) {
    window.location.replace(`#/t/${c.turns[c.turns.length - 1].turn_id}`);
    return;
  }
  renderTraceList(null);
  $("trace-detail").hidden = true;
  $("trace-status").textContent = c ? "No questions in this conversation yet." : "Open a conversation, or paste a trace ID.";
}

async function openTrace(id) {
  $("trace-id").value = id;
  // A conversation ID opens its latest trace.
  if (/^c_[0-9a-f]{16}$/.test(id)) {
    await openConversation(id);
    window.location.replace("#/traces");
    return;
  }
  $("trace-status").textContent = "Loading…";
  const { response, data } = await api(`/api/traces/${encodeURIComponent(id)}`);
  if (!response.ok) {
    $("trace-status").textContent = errorDetail(data, "Trace not found.");
    $("trace-detail").hidden = true;
    return;
  }
  $("trace-status").textContent = "";
  const conversationId = id.split(".")[0];
  if (!state.conversation || state.conversation.conversation_id !== conversationId) {
    const conv = await api(`/api/conversations/${conversationId}`);
    if (conv.response.ok) state.conversation = conv.data;
  }
  $("tab-chat").href = `#/c/${conversationId}`;
  renderTraceList(id);
  renderTrace(data);
}

function renderTraceList(activeId) {
  const c = state.conversation;
  $("trace-list-title").textContent = c ? c.title : "Traces";
  const list = $("trace-list");
  list.replaceChildren();
  if (!c) return;
  c.turns.forEach((turn) => {
    const a = link(`#/t/${turn.turn_id}`, null, "trace-link");
    a.append(
      el("span", turn.question, "title"),
      el("span", `${turn.turn_id} · ${ms(turn.trace.duration_ms)}${turn.error ? " · error" : ""}`, "meta mono"),
    );
    if (turn.turn_id === activeId) {
      a.classList.add("active");
      a.setAttribute("aria-current", "page");
    }
    if (turn.error) a.classList.add("failed");
    const li = el("li");
    li.append(a);
    list.append(li);
  });
}

function fillSummary(dl, entries) {
  dl.replaceChildren();
  for (const [key, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    dl.append(el("dt", key), el("dd", String(value)));
  }
}

function renderTrace(turn) {
  const t = turn.trace;
  const r = turn.result;
  $("trace-detail").hidden = false;
  $("trace-question").textContent = turn.question;
  fillSummary($("trace-summary"), [
    ["trace", t.trace_id],
    ["conversation", t.attributes.conversation_id],
    ["started", when(t.started_at)],
    ["duration", ms(t.duration_ms)],
    ["strategy", turn.strategy],
    ["searched as", r && r.standalone_question !== turn.question ? r.standalone_question : ""],
    ["outcome", turn.error ? `error: ${turn.error}` : r.not_found ? "not found" : `answered, ${r.citations.length} citation(s)`],
    ["tokens", r ? Object.entries(r.tokens).map(([k, v]) => `${k} ${v}`).join(", ") : ""],
    ["cost", r ? usd(r.cost.usd) : ""],
    ["models", r ? Object.values(r.models).join(", ") : ""],
    ["X-Ray trace", t.attributes.xray_trace_id],
    ["Lambda request", t.attributes.lambda_request_id],
  ]);
  renderWaterfall(t);
  renderChunks(r);
  $("trace-json").textContent = JSON.stringify(turn, null, 2);
}

function renderWaterfall(trace) {
  const box = $("waterfall");
  box.replaceChildren();
  const total = Math.max(trace.duration_ms, 1);
  for (const span of trace.spans) {
    const details = el("details", null, `span${span.status === "error" ? " failed" : ""}`);
    const summary = el("summary");
    const track = el("span", null, "track");
    const bar = el("span", null, "bar");
    bar.style.left = `${(span.start_ms / total) * 100}%`;
    bar.style.width = `max(2px, ${(span.duration_ms / total) * 100}%)`;
    track.append(bar);
    const name = el("span", span.name, "name mono");
    name.style.paddingLeft = `${(span.depth || 0) * 0.9}rem`; // nested steps, e.g. an agent's searches
    summary.append(name, track, el("span", ms(span.duration_ms), "dur mono"));
    if (span.depth) details.classList.add("nested");
    details.append(summary);
    const attrs = { ...span.attributes };
    if (span.error) attrs.error = span.error;
    details.append(el("pre", JSON.stringify(attrs, null, 2)));
    box.append(details);
  }
  if (!trace.spans.length) box.append(el("p", "No steps recorded.", "muted"));
}

function renderChunks(result) {
  const body = $("chunks").tBodies[0];
  body.replaceChildren();
  if (!result) return;
  const cited = new Set(result.citations.map((c) => c.number));
  const fmt = (obj) =>
    Object.entries(obj)
      .map(([k, v]) => `${k} ${typeof v === "number" && !Number.isInteger(v) ? v.toFixed(4) : v}`)
      .join("\n");
  result.chunks.forEach((c, i) => {
    const row = el("tr", null, cited.has(i + 1) ? "cited" : "");
    const text = el("td");
    const details = el("details");
    details.append(el("summary", c.text.slice(0, 120)), el("pre", c.text));
    text.append(details);
    row.append(
      el("td", String(i + 1)),
      el("td", `${sourceName(c)} · ${pages(c)}`),
      el("td", fmt(c.ranks), "mono"),
      el("td", fmt(c.scores), "mono"),
      text,
    );
    body.append(row);
  });
}

// ------------------------------------------------------------------ start

document.addEventListener("DOMContentLoaded", async () => {
  state.emptyThread = $("empty-thread");
  $("composer").addEventListener("submit", ask);
  $("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("composer").requestSubmit();
  });
  $("delete-conversation").addEventListener("click", deleteConversation);
  $("trace-search").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("trace-id").value.trim();
    if (id) window.location.hash = `#/t/${encodeURIComponent(id)}`;
  });
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
  await loadConversations();
  route();
});
