// docqa ask page. All model and document text is inserted with textContent, never innerHTML.
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function pages(c) {
  return c.page_start === c.page_end ? `p${c.page_start}` : `pp${c.page_start}-${c.page_end}`;
}

function sourceName(c) {
  return c.title || c.source_key.split("/").pop();
}

function fillList(dl, entries) {
  dl.replaceChildren();
  for (const [key, value] of entries) {
    dl.append(el("dt", key), el("dd", String(value)));
  }
}

// Turn "GPA was 3.86 [1]." into text nodes plus <sup> markers.
function renderAnswer(container, text, cited) {
  container.replaceChildren();
  const parts = text.split(/(\[\d+\])/);
  for (const part of parts) {
    const match = part.match(/^\[(\d+)\]$/);
    if (match && cited.has(Number(match[1]))) {
      container.append(el("sup", match[1], "cite"));
    } else {
      container.append(document.createTextNode(part));
    }
  }
}

function render(result) {
  const cited = new Set(result.citations.map((c) => c.number));
  const answer = $("answer");
  renderAnswer(answer, result.answer, cited);
  answer.classList.toggle("not-found", result.not_found);

  const citations = $("citations");
  citations.replaceChildren();
  for (const c of result.citations) {
    const item = el("li", `${sourceName(c)} · ${pages(c)}`);
    item.value = c.number;
    item.title = c.source_key;
    citations.append(item);
  }

  fillList($("timings"), Object.entries(result.timings_ms));
  fillList($("tokens"), Object.entries(result.tokens));
  const cost = [["USD", result.cost.usd.toFixed(6)]];
  if (result.cost.unpriced_models.length) {
    cost.push(["unpriced", result.cost.unpriced_models.join(", ")]);
  }
  fillList($("cost"), cost);
  fillList($("models"), Object.entries(result.models));

  const body = $("chunks").tBodies[0];
  body.replaceChildren();
  result.chunks.forEach((c, i) => {
    const row = document.createElement("tr");
    if (cited.has(i + 1)) row.className = "cited";
    const fmt = (obj) =>
      Object.entries(obj)
        .map(([k, v]) => `${k} ${typeof v === "number" && !Number.isInteger(v) ? v.toFixed(4) : v}`)
        .join("\n");
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

  $("result").hidden = false;
}

async function ask(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const button = $("ask-button");
  button.disabled = true;
  $("status").textContent = "Thinking…";
  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, strategy: $("strategy").value }),
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "request was rejected";
      $("status").textContent = `Error: ${detail}`;
      return;
    }
    $("status").textContent = "";
    render(data);
  } catch (err) {
    $("status").textContent = "Error: could not reach the server.";
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("ask-form").addEventListener("submit", ask);
  $("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("ask-form").requestSubmit();
  });
});
