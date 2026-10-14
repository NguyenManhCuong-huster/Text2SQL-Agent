"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const PAGE_SIZE = 50;

const state = {
  config: null,
  sources: [],
  sourceId: null,
  schema: null,
  context: [],          // [{question, sql}] for follow-up questions
  busy: false,
  historyTab: "all",
};

const store = {
  get(k, d) { try { const v = localStorage.getItem("t2s." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("t2s." + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

const ICON = {
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M4 20h4L20 8l-4-4L4 16z"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>',
  download: '<svg viewBox="0 0 24 24"><path d="M12 4v12M6 10l6 6 6-6M4 20h16"/></svg>',
  spark: '<svg viewBox="0 0 24 24"><path d="M12 3l2.5 5.5L20 11l-5.5 2.5L12 19l-2.5-5.5L4 11l5.5-2.5z"/></svg>',
  chev: '<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/></svg>',
  starFill: '<svg viewBox="0 0 24 24" style="fill:currentColor"><path d="M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M7 11v9H4v-9zM7 11l4-8c1.5 0 2.5 1 2.5 2.5V9h5.2a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.5 20H7"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M7 13V4H4v9zM7 13l4 8c1.5 0 2.5-1 2.5-2.5V15h5.2a2 2 0 0 0 2-2.3l-1.2-7A2 2 0 0 0 17.5 4H7"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"/></svg>',
};

// ------------------------------------------------------------------ utils

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && path !== "/api/login") showLogin();
  if (!res.ok) {
    let msg = data?.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map(d => d.msg).join("; ");
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data;
}

// POSTs to the SSE endpoint and feeds progress events to onEvent; resolves with the final result.
async function askStream(body, onEvent) {
  const res = await fetch("/api/ask/stream", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not json */ }
    if (res.status === 401) showLogin();
    throw new Error(Array.isArray(msg) ? msg.map(d => d.msg).join("; ") : msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", final = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find(l => l.startsWith("data: "));
      if (!line) continue;
      const ev = JSON.parse(line.slice(6));
      if (ev.type === "error") throw new Error(ev.message);
      if (ev.type === "result") final = ev;
      else onEvent(ev);
    }
  }
  if (!final) throw new Error("Connection closed before the answer was ready.");
  return final;
}

function showLogin() {
  const dlg = $("#login-dialog");
  if (dlg.open) return;
  $("#login-error").textContent = "";
  dlg.showModal();
  $("#login-password").focus();
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 4500 : 2200);
}

function fmtNum(v) {
  if (typeof v !== "number") return v;
  if (Number.isInteger(v)) return Math.abs(v) >= 10000 ? v.toLocaleString() : String(v);
  return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function highlight(sql) {
  if (window.hljs) {
    try { return hljs.highlight(sql, { language: "sql" }).value; } catch { /* fall through */ }
  }
  return esc(sql);
}

function copyText(text) {
  navigator.clipboard?.writeText(text).then(() => toast("Copied"), () => toast("Copy failed", true));
}

function toCSV(columns, rows) {
  const cell = v => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [columns.map(cell).join(","), ...rows.map(r => r.map(cell).join(","))].join("\r\n");
}

function download(filename, text) {
  const blob = new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: filename });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

function timeAgo(iso) {
  const d = new Date(iso.replace(" ", "T") + "Z");
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return d.toLocaleDateString();
}

// ------------------------------------------------------------------ theme

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#hljs-light").disabled = dark;
  $("#hljs-dark").disabled = !dark;
  if (window.Chart) {
    Chart.defaults.color = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim();
    Chart.defaults.borderColor = getComputedStyle(document.documentElement).getPropertyValue("--border").trim();
  }
  return dark;
}

function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}

// ------------------------------------------------------------------ sources & schema

async function loadSources(selectId) {
  state.sources = await api("/api/sources");
  const sel = $("#source-select");
  const kindLabel = { postgres: "PostgreSQL", mysql: "MySQL" };
  sel.innerHTML = state.sources.map(s =>
    `<option value="${esc(s.id)}">${esc(s.name)}${kindLabel[s.kind] ? ` · ${kindLabel[s.kind]}` : ""}</option>`).join("");
  const wanted = selectId || store.get("source", null);
  const pick = state.sources.find(s => s.id === wanted) || state.sources[0];
  if (pick) await selectSource(pick.id);
}

async function selectSource(id) {
  state.sourceId = id;
  store.set("source", id);
  $("#source-select").value = id;
  const src = state.sources.find(s => s.id === id);
  $("#btn-delete-source").style.visibility = src?.builtin ? "hidden" : "visible";
  $("#btn-delete-source").title = src?.kind && src.kind !== "sqlite" ? "Remove connection" : "Delete data source";
  $("#btn-refresh-schema").hidden = !src || src.kind === "sqlite";
  resetConversation();
  await loadSchema(false);
  loadSuggestions();
  if ($("#history-drawer").classList.contains("open")) loadHistory();
}

async function loadSchema(refresh) {
  const id = state.sourceId;
  const src = state.sources.find(s => s.id === id);
  $("#schema-tree").innerHTML = '<div class="skeleton" style="height:120px"></div>';
  $("#source-meta").textContent = src?.detail || "";
  try {
    state.schema = await api(`/api/sources/${encodeURIComponent(id)}/schema${refresh ? "?refresh=true" : ""}`);
    if (state.sourceId !== id) return;
    const rows = state.schema.tables.reduce((a, t) => a + (t.row_count || 0), 0);
    const parts = [`${state.schema.tables.length} tables`, `${rows.toLocaleString()} rows`];
    if (src?.kind === "sqlite") parts.push(`${src.size_kb} KB`);
    else parts.unshift(state.schema.version);
    $("#source-meta").textContent = parts.join(" · ");
    if (src?.detail) $("#source-meta").title = src.detail;
    renderSchema();
  } catch (e) {
    state.schema = null;
    $("#schema-tree").innerHTML = `<p class="error small">${esc(e.message)}</p>`;
  }
}

function renderSchema() {
  const filter = $("#schema-filter").value.trim().toLowerCase();
  const tree = $("#schema-tree");
  const html = state.schema.tables.map(t => {
    const tableHit = !filter || t.name.toLowerCase().includes(filter);
    const cols = t.columns.filter(c => tableHit || c.name.toLowerCase().includes(filter));
    if (!tableHit && !cols.length) return "";
    const open = filter ? " open" : "";
    return `<div class="tbl${open}" data-table="${esc(t.name)}">
      <button class="tbl-head" type="button">${ICON.chev}
        <span class="tbl-name">${esc(t.name)}</span>
        ${t.kind === "view" ? '<span class="badge view">VIEW</span>' : ""}
        ${t.hidden_columns ? `<span class="badge hidden-cols" title="${t.hidden_columns} column(s) hidden by the access policy">−${t.hidden_columns}</span>` : ""}
        <span class="tbl-insert" data-insert="${esc(t.name)}" title="Insert into question">insert</span>
        <span class="tbl-count">${t.row_count != null ? t.row_count.toLocaleString() : ""}</span>
      </button>
      <div class="cols">${cols.map(c => `
        <div class="col">
          <span>${esc(c.name)}</span>
          ${c.pk ? '<span class="badge pk">PK</span>' : ""}
          ${c.fk ? `<span class="badge fk" title="→ ${esc(c.fk)}">FK</span>` : ""}
          <span class="col-type">${esc(c.type.toLowerCase())}</span>
        </div>`).join("")}
      </div>
    </div>`;
  }).join("");
  tree.innerHTML = html || '<p class="muted small">No matches.</p>';
}

async function loadSuggestions(refresh = false) {
  const box = $("#suggestions");
  box.innerHTML = '<div class="skeleton"></div>'.repeat(4);
  try {
    const q = new URLSearchParams({ model: currentModel() });
    if (refresh) q.set("refresh", "true");
    const items = await api(`/api/sources/${encodeURIComponent(state.sourceId)}/suggestions?${q}`);
    box.innerHTML = items.map(s => `<button class="chip" type="button">${esc(s)}</button>`).join("") +
      `<button class="chip" type="button" data-refresh="1" title="Generate new suggestions">${ICON.retry}</button>`;
  } catch (e) {
    box.innerHTML = `<span class="muted small">Couldn't load suggestions: ${esc(e.message)}</span>`;
  }
}

// ------------------------------------------------------------------ conversation

function currentModel() { return $("#model-select").value || state.config?.default_model; }

function resetConversation() {
  state.context = [];
  $$(".turn").forEach(el => el.remove());
  $("#empty-state").style.display = "";
  updateContextHint();
}

function updateContextHint() {
  const n = state.context.length;
  $("#context-hint").textContent = n
    ? `Follow-up mode · remembering ${Math.min(n, 4)} previous question${n > 1 ? "s" : ""}`
    : "Enter to send · Shift+Enter for new line";
}

function scrollToEl(el) {
  requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }));
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy || !state.sourceId) return;
  if (!state.config.key_configured) return toast("OPENROUTER_KEY is not configured on the server.", true);
  state.busy = true;
  $("#btn-send").disabled = true;
  $("#empty-state").style.display = "none";

  const turn = document.createElement("div");
  turn.className = "turn";
  turn.innerHTML = `<div class="q-bubble">${esc(question)}</div>
    <div class="card"><div class="thinking"><span class="dots"><span></span><span></span><span></span></span>
    <span class="thinking-text">Reading schema and writing SQL…</span></div></div>`;
  $("#feed").appendChild(turn);
  scrollToEl(turn);
  const card = $(".card", turn);
  const onEvent = (ev) => {
    if (ev.type === "step") {
      const t = $(".thinking-text", turn);
      if (t) t.textContent = ev.message;
    } else if (ev.type === "sql") {
      let pre = $(".live-sql", card);
      if (!pre) {
        pre = document.createElement("pre");
        pre.className = "sql-code live-sql";
        card.appendChild(pre);
      }
      pre.innerHTML = `<code class="hljs language-sql">${highlight(ev.sql)}</code>`;
    }
  };

  try {
    const res = await askStream({
      source_id: state.sourceId, question, model: currentModel(), context: state.context.slice(-4),
    }, onEvent);
    renderResult(turn, question, res);
    if (res.status === "ok" && res.sql) {
      state.context.push({ question, sql: res.sql });
      updateContextHint();
    }
  } catch (e) {
    $(".card", turn).innerHTML = `<div class="card-body"><div class="status-box error"><b>Request failed</b><pre>${esc(e.message)}</pre></div>
      <div><button class="tool-btn primary" data-retry>${ICON.retry} Retry</button></div></div>`;
    $("[data-retry]", turn).onclick = () => { turn.remove(); ask(question); };
  } finally {
    state.busy = false;
    $("#btn-send").disabled = false;
  }
}

function renderResult(turn, question, res) {
  const card = $(".card", turn);
  const body = document.createElement("div");
  body.className = "card-body";
  card.innerHTML = "";
  card.appendChild(body);

  if (res.status === "unanswerable") {
    body.innerHTML = `<div class="status-box warn"><b>Can't answer this from the current database.</b><pre>${esc(res.explanation)}</pre></div>`;
    return;
  }
  if (res.explanation) {
    const p = document.createElement("p");
    p.className = "explanation";
    p.textContent = res.explanation;
    body.appendChild(p);
  }
  if (res.tables_used?.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.style.margin = "0";
    p.textContent = `Schema narrowed to: ${res.tables_used.join(", ")}`;
    body.appendChild(p);
  }
  const answerSlot = document.createElement("div");
  body.appendChild(answerSlot);

  const resultSlot = document.createElement("div");
  resultSlot.style.cssText = "display:flex;flex-direction:column;gap:12px";

  let currentSql = res.sql || "", edited = false;
  body.appendChild(sqlBlock(res.sql || "", async (sql) => {
    resultSlot.innerHTML = '<div class="thinking"><span class="dots"><span></span><span></span><span></span></span> Running…</div>';
    answerSlot.innerHTML = "";
    try {
      const r = await api("/api/execute", { json: { source_id: state.sourceId, sql } });
      resultSlot.innerHTML = "";
      renderData(resultSlot, r, { question, totalMs: null, model: null });
      currentSql = sql;
      edited = true;
      const ctx = state.context.find(c => c.question === question);
      if (ctx) ctx.sql = sql;
      $$(".feedback", body).forEach(el => el.remove());
      if (res.id) body.appendChild(feedbackBar(res.id, () => currentSql, () => edited));
    } catch (e) {
      resultSlot.innerHTML = `<div class="status-box error"><b>Query error</b><pre>${esc(e.message)}</pre></div>`;
    }
  }));

  if (res.attempts?.length) {
    const d = document.createElement("details");
    d.className = "attempts";
    const ok = res.status === "ok";
    d.innerHTML = `<summary>${ok ? "Self-corrected after" : "Gave up after"} ${res.attempts.length} failed attempt${res.attempts.length > 1 ? "s" : ""}</summary>` +
      res.attempts.map((a, i) => `<div class="attempt"><div>Attempt ${i + 1}: <span style="color:var(--danger)">${esc(a.error)}</span></div><pre>${esc(a.sql)}</pre></div>`).join("");
    body.appendChild(d);
  }
  body.appendChild(resultSlot);

  if (res.status === "error") {
    resultSlot.innerHTML = `<div class="status-box error"><b>The query still failed after ${res.attempts.length} attempts.</b><pre>${esc(res.error)}</pre></div>
      <p class="muted small" style="margin:0">Edit the SQL above and press Run, or rephrase your question.</p>`;
    return;
  }
  renderData(resultSlot, res.result, { question, totalMs: res.duration_ms, model: res.model, tokens: res.tokens, cost: res.cost });
  if (res.id) body.appendChild(feedbackBar(res.id, () => currentSql, () => edited));

  if ($("#opt-summary").checked) {
    answerSlot.innerHTML = '<div class="answer">' + ICON.spark + '<p class="skeleton" style="height:18px;width:60%"></p></div>';
    api("/api/answer", {
      json: { question, sql: res.sql, columns: res.result.columns, rows: res.result.rows.slice(0, 40),
              row_count: res.result.row_count, truncated: res.result.truncated, model: currentModel() },
    }).then(a => {
      answerSlot.innerHTML = `<div class="answer">${ICON.spark}<p></p></div>`;
      $("p", answerSlot).textContent = a.answer;
    }).catch(() => { answerSlot.innerHTML = ""; });
  }
}

// "Correct" answers are reused as examples for similar questions; an edited query is saved as the correct one.
function feedbackBar(historyId, getSql, wasEdited) {
  const host = document.createElement("div");
  host.className = "feedback";
  host.innerHTML = `<span class="muted small">Is this answer correct?</span>
    <button class="tool-btn" data-fb="1" title="Correct: reuse as an example for similar questions">${ICON.up} Correct</button>
    <button class="tool-btn" data-fb="-1" title="Wrong">${ICON.down} Wrong</button>`;
  $$("[data-fb]", host).forEach(b => b.onclick = async () => {
    const fb = +b.dataset.fb;
    const active = b.classList.contains("active");
    try {
      await api(`/api/history/${historyId}`, {
        method: "PATCH",
        json: { feedback: active ? 0 : fb, ...(fb === 1 && !active && wasEdited() ? { sql: getSql() } : {}) },
      });
      $$("[data-fb]", host).forEach(x => x.classList.toggle("active", !active && x === b));
      if (!active) toast(fb === 1 ? (wasEdited() ? "Saved your corrected SQL as an example" : "Thanks! Saved as an example") : "Thanks for the feedback");
    } catch (e) { toast(e.message, true); }
  });
  return host;
}

function sqlBlock(sql, onRun) {
  const wrap = document.createElement("div");
  wrap.className = "sql-block";
  wrap.innerHTML = `<div class="sql-toolbar"><span class="label">SQL</span>
      <button class="tool-btn" data-act="edit">${ICON.edit} Edit</button>
      <button class="tool-btn" data-act="copy">${ICON.copy} Copy</button>
      <button class="tool-btn primary" data-act="run" hidden>${ICON.play} Run</button>
    </div>
    <pre class="sql-code"><code class="hljs language-sql"></code></pre>
    <textarea class="sql-edit" spellcheck="false" hidden></textarea>`;
  const code = $("code", wrap), pre = $("pre", wrap), ta = $("textarea", wrap);
  const runBtn = $('[data-act="run"]', wrap), editBtn = $('[data-act="edit"]', wrap);
  let current = sql;
  code.innerHTML = highlight(current);

  const setEditing = (on) => {
    pre.hidden = on; ta.hidden = !on; runBtn.hidden = !on;
    editBtn.innerHTML = on ? `${ICON.edit} Cancel` : `${ICON.edit} Edit`;
    if (on) { ta.value = current; ta.focus(); }
  };
  editBtn.onclick = () => setEditing(pre.hidden === false);
  $('[data-act="copy"]', wrap).onclick = () => copyText(pre.hidden ? ta.value : current);
  const run = () => {
    current = ta.value;
    code.innerHTML = highlight(current);
    setEditing(false);
    onRun(current);
  };
  runBtn.onclick = run;
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); }
  });
  return wrap;
}

// ------------------------------------------------------------------ result data: table + chart

function renderData(container, r, meta) {
  const view = { sortCol: null, sortDir: 1, page: 0, tab: "table", chart: null };
  const numericCols = r.columns.map((_, i) =>
    r.rows.length > 0 && r.rows.every(row => row[i] === null || typeof row[i] === "number") &&
    r.rows.some(row => typeof row[i] === "number"));
  const chartable = r.rows.length > 1 && numericCols.some(Boolean) && r.columns.length >= 2;

  const head = document.createElement("div");
  head.className = "result-head";
  const parts = [`<b>${r.row_count.toLocaleString()}</b> row${r.row_count === 1 ? "" : "s"}${r.truncated ? " (truncated)" : ""}`,
                 `SQL ${r.duration_ms} ms`];
  if (meta.totalMs) parts.push(`total ${(meta.totalMs / 1000).toFixed(1)} s`);
  if (meta.model) parts.push(esc(meta.model.split("/").pop()));
  if (meta.tokens) parts.push(`${meta.tokens.toLocaleString()} tok`);
  if (meta.cost) parts.push(`$${meta.cost < 0.01 ? meta.cost.toFixed(4) : meta.cost.toFixed(3)}`);
  head.innerHTML = `<div class="seg">
      <button data-tab="table" class="active">Table</button>
      <button data-tab="chart" ${chartable ? "" : "disabled title='Needs a numeric column and 2+ rows'"}>Chart</button>
    </div>
    <div class="result-meta">${parts.map(p => `<span>${p}</span>`).join("")}
      <button class="tool-btn" data-act="csv">${ICON.download} CSV</button></div>`;
  container.appendChild(head);
  const pane = document.createElement("div");
  container.appendChild(pane);

  $('[data-act="csv"]', head).onclick = () => download(`query_${Date.now()}.csv`, toCSV(r.columns, r.rows));
  $$(".seg button", head).forEach(b => b.onclick = () => {
    if (b.disabled) return;
    view.tab = b.dataset.tab;
    $$(".seg button", head).forEach(x => x.classList.toggle("active", x === b));
    draw();
  });

  function sortedRows() {
    if (view.sortCol === null) return r.rows;
    const i = view.sortCol, d = view.sortDir;
    return [...r.rows].sort((a, b) => {
      const x = a[i], y = b[i];
      if (x === y) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * d;
    });
  }

  function drawTable() {
    if (!r.rows.length) {
      pane.innerHTML = '<div class="chart-empty">No rows returned.</div>';
      return;
    }
    const rows = sortedRows();
    const pages = Math.ceil(rows.length / PAGE_SIZE);
    view.page = Math.min(view.page, pages - 1);
    const slice = rows.slice(view.page * PAGE_SIZE, (view.page + 1) * PAGE_SIZE);
    pane.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>${r.columns.map((c, i) =>
      `<th data-col="${i}" class="${numericCols[i] ? "num" : ""}">${esc(c)}${view.sortCol === i ? `<span class="sort">${view.sortDir > 0 ? "▲" : "▼"}</span>` : ""}</th>`).join("")}</tr></thead>
      <tbody>${slice.map(row => `<tr>${row.map((v, i) => {
        if (v === null) return '<td class="null">NULL</td>';
        const s = String(v);
        return `<td class="${numericCols[i] ? "num" : ""}${s.length > 60 ? " long" : ""}" ${s.length > 60 ? `title="${esc(s)}"` : ""}>${esc(fmtNum(v))}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div>
      ${pages > 1 ? `<div class="pager" style="margin-top:8px"><button data-p="-1" ${view.page === 0 ? "disabled" : ""}>‹ Prev</button>
        <span>Page ${view.page + 1} / ${pages}</span><button data-p="1" ${view.page >= pages - 1 ? "disabled" : ""}>Next ›</button></div>` : ""}`;
    $$("th", pane).forEach(th => th.onclick = () => {
      const i = +th.dataset.col;
      if (view.sortCol === i) view.sortDir = -view.sortDir; else { view.sortCol = i; view.sortDir = numericCols[i] ? -1 : 1; }
      view.page = 0;
      drawTable();
    });
    $$(".pager button", pane).forEach(b => b.onclick = () => { view.page += +b.dataset.p; drawTable(); });
  }

  // Chart defaults. Dimensions = text columns plus numeric time-like columns (year, month, quarter...).
  const looksTemporal = (i) => r.rows.length > 0 && r.rows.slice(0, 5).every(row =>
    /^\d{4}(-\d{2})?(-\d{2})?/.test(String(row[i] ?? "")) || /^(19|20)\d{2}$/.test(String(row[i])) ||
    /^Q[1-4]$/i.test(String(row[i] ?? "")));
  const timeNamed = (i) => /(^|_)(year|month|quarter|week|day|date|period|yr|qtr|nam|thang|quy)(_|$)/i.test(r.columns[i]) &&
    (!numericCols[i] || r.rows.every(row => row[i] === null || Number.isInteger(row[i])));
  const dims = r.columns.map((c, i) => i).filter(i => !numericCols[i] || timeNamed(i));
  const distinct = (i) => new Set(r.rows.map(row => row[i])).size;
  const firstLabelIdx = dims.find(looksTemporal) ?? dims.find(timeNamed) ?? dims[0] ?? 0;
  // Prefer real measures over helper numbers (ids, year/quarter ordinals, ranks) for the Y axis.
  const isHelper = (i) => /(^|_)(id|year|month|quarter|week|day|num|number|no|rank|idx|index|nam|thang|quy)$/i.test(r.columns[i]);
  const yDefault = numericCols.map((n, i) => n && !dims.includes(i) ? i : -1).filter(i => i >= 0)
    .sort((a, b) => isHelper(a) - isHelper(b));
  // Long-format results (time x category x metric) are pivoted: one series per category.
  // A series column must actually split X (skip e.g. quarter_number next to quarter: same grouping).
  const splitsX = (i) => new Set(r.rows.map(row => `${row[firstLabelIdx]}\u0000${row[i]}`)).size > distinct(firstLabelIdx);
  const seriesIdx = dims.find(i => i !== firstLabelIdx && distinct(i) <= 12 && distinct(i) < r.rows.length && splitsX(i)) ?? -1;
  const labelLooksTemporal = looksTemporal(firstLabelIdx) || timeNamed(firstLabelIdx);
  const chartCfg = {
    x: firstLabelIdx,
    ys: yDefault.slice(0, 1),
    series: seriesIdx,
    type: labelLooksTemporal ? "line" : (r.rows.length <= 8 && yDefault.length === 1 && !numericCols[firstLabelIdx] ? "doughnut" : "bar"),
  };
  if (chartCfg.series >= 0 && labelLooksTemporal && distinct(firstLabelIdx) <= 12) chartCfg.type = "bar";
  if (chartCfg.type === "doughnut" && (r.rows.length > 6 || chartCfg.series >= 0)) chartCfg.type = "bar";

  function drawChart() {
    if (view.chart) { view.chart.destroy(); view.chart = null; }
    if (!window.Chart) { pane.innerHTML = '<div class="chart-empty">Chart library failed to load.</div>'; return; }
    pane.innerHTML = `<div class="chart-controls">
        <label>Type <select data-c="type">${["bar", "line", "area", "hbar", "doughnut", "scatter"].map(t =>
          `<option value="${t}" ${chartCfg.type === t ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label>X <select data-c="x">${r.columns.map((c, i) => `<option value="${i}" ${chartCfg.x === i ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
        <label>Y <select data-c="y">${numericCols.map((n, i) => n ? `<option value="${i}" ${chartCfg.ys[0] === i ? "selected" : ""}>${esc(r.columns[i])}</option>` : "").join("")}
          ${yDefault.length > 1 ? `<option value="all" ${chartCfg.ys.length > 1 ? "selected" : ""}>All numeric</option>` : ""}</select></label>
        ${dims.length > 1 ? `<label>Series <select data-c="series"><option value="-1">(none)</option>${dims.filter(i => i !== chartCfg.x).map(i =>
          `<option value="${i}" ${chartCfg.series === i ? "selected" : ""}>${esc(r.columns[i])}</option>`).join("")}</select></label>` : ""}
      </div><div class="chart-box"><canvas></canvas></div>`;
    $$("select", pane).forEach(s => s.onchange = () => {
      if (s.dataset.c === "type") chartCfg.type = s.value;
      if (s.dataset.c === "x") { chartCfg.x = +s.value; if (chartCfg.series === chartCfg.x) chartCfg.series = -1; }
      if (s.dataset.c === "y") chartCfg.ys = s.value === "all" ? yDefault.slice(0, 6) : [+s.value];
      if (s.dataset.c === "series") chartCfg.series = +s.value;
      drawChart();
    });
    const rows = r.rows.slice(0, 500);
    const palette = isDark()
      ? ["#818cf8", "#34d399", "#fbbf24", "#f472b6", "#60a5fa", "#a78bfa", "#fb923c", "#2dd4bf"]
      : ["#4f46e5", "#059669", "#d97706", "#db2777", "#2563eb", "#7c3aed", "#ea580c", "#0d9488"];
    const labels = rows.map(row => row[chartCfg.x] === null ? "NULL" : String(row[chartCfg.x]));
    const t = chartCfg.type;
    let config;
    if (t === "doughnut") {
      const yi = chartCfg.ys[0];
      config = {
        type: "doughnut",
        data: { labels, datasets: [{ data: rows.map(row => row[yi]), backgroundColor: labels.map((_, i) => palette[i % palette.length]), borderWidth: 1 }] },
        options: { maintainAspectRatio: false, plugins: { legend: { position: "right" } } },
      };
    } else if (t === "scatter") {
      const yi = chartCfg.ys[0];
      config = {
        type: "scatter",
        data: { datasets: [{ label: r.columns[yi], data: rows.map(row => ({ x: row[chartCfg.x], y: row[yi] })), backgroundColor: palette[0] }] },
        options: { maintainAspectRatio: false, scales: { x: { title: { display: true, text: r.columns[chartCfg.x] } }, y: { title: { display: true, text: r.columns[yi] } } } },
      };
    } else {
      // Either one dataset per Y column, or (pivot) one dataset per value of the series column.
      let xLabels = labels, groups;
      if (chartCfg.series >= 0) {
        const yi = chartCfg.ys[0], si = chartCfg.series;
        xLabels = [...new Set(labels)];
        const keys = [...new Set(rows.map(row => String(row[si] ?? "NULL")))];
        groups = keys.map(key => {
          const byX = new Map();
          rows.forEach((row, n) => { if (String(row[si] ?? "NULL") === key) byX.set(labels[n], (byX.get(labels[n]) ?? 0) + (row[yi] ?? 0)); });
          return { label: key, data: xLabels.map(x => byX.has(x) ? byX.get(x) : null) };
        });
      } else {
        groups = chartCfg.ys.map(yi => ({ label: r.columns[yi], data: rows.map(row => row[yi]) }));
      }
      config = {
        type: t === "hbar" ? "bar" : t === "area" ? "line" : t,
        data: {
          labels: xLabels,
          datasets: groups.map((g, k) => ({
            label: g.label,
            data: g.data,
            backgroundColor: t === "area" ? palette[k % palette.length] + "33" : palette[k % palette.length],
            borderColor: palette[k % palette.length],
            borderWidth: t === "bar" || t === "hbar" ? 0 : 2,
            borderRadius: t === "bar" || t === "hbar" ? 4 : 0,
            fill: t === "area",
            tension: 0.25,
            pointRadius: rows.length > 60 ? 0 : 3,
          })),
        },
        options: {
          maintainAspectRatio: false,
          indexAxis: t === "hbar" ? "y" : "x",
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { display: groups.length > 1 } },
          scales: { x: { grid: { display: false } }, y: { beginAtZero: true } },
        },
      };
      if (t === "hbar") config.options.scales = { y: { grid: { display: false } }, x: { beginAtZero: true } };
      // Series on very different scales (e.g. revenue vs. order count) get a secondary axis.
      const maxes = config.data.datasets.map(d => Math.max(...d.data.map(v => Math.abs(v ?? 0)), 1e-9));
      const top = Math.max(...maxes);
      if (t !== "hbar" && chartCfg.series < 0 && maxes.length > 1 && maxes.some(m => top / m > 20)) {
        config.data.datasets.forEach((d, k) => { if (top / maxes[k] > 20) d.yAxisID = "y1"; });
        config.options.scales.y1 = { position: "right", beginAtZero: true, grid: { drawOnChartArea: false } };
      }
    }
    view.chart = new Chart($("canvas", pane), config);
  }

  function draw() { view.tab === "chart" ? drawChart() : (view.chart?.destroy(), view.chart = null, drawTable()); }
  draw();
}

// ------------------------------------------------------------------ history

async function loadHistory() {
  const list = $("#history-list");
  list.innerHTML = '<div class="skeleton" style="height:60px;margin:8px"></div>'.repeat(3);
  try {
    const q = new URLSearchParams({ source_id: state.sourceId });
    if (state.historyTab === "starred") q.set("starred", "true");
    const items = await api(`/api/history?${q}`);
    if (!items.length) {
      list.innerHTML = `<p class="muted small" style="padding:12px">${state.historyTab === "starred" ? "No starred queries yet." : "No questions asked on this data source yet."}</p>`;
      return;
    }
    list.innerHTML = items.map(h => `<div class="h-item" data-id="${h.id}">
        <span class="h-dot ${esc(h.status)}"></span>
        <div class="h-main"><div class="h-q">${esc(h.question)}</div>
          <div class="h-meta"><span>${timeAgo(h.created_at)}</span>${h.row_count != null ? `<span>${h.row_count} rows</span>` : ""}
          <span>${esc((h.model || "").split("/").pop())}</span></div></div>
        <div class="h-actions">
          <button data-star title="Star" class="${h.starred ? "starred" : ""}">${h.starred ? ICON.starFill : ICON.star}</button>
          <button data-del title="Delete">${ICON.trash}</button>
        </div></div>`).join("");
    $$(".h-item", list).forEach(el => {
      const h = items.find(x => x.id === +el.dataset.id);
      el.onclick = (e) => {
        if (e.target.closest("[data-star]")) {
          api(`/api/history/${h.id}`, { method: "PATCH", json: { starred: !h.starred } }).then(loadHistory);
        } else if (e.target.closest("[data-del]")) {
          api(`/api/history/${h.id}`, { method: "DELETE" }).then(loadHistory);
        } else {
          closeDrawers();
          ask(h.question);
        }
      };
    });
  } catch (e) {
    list.innerHTML = `<p class="error small" style="padding:12px">${esc(e.message)}</p>`;
  }
}

function closeDrawers() {
  $("#history-drawer").classList.remove("open");
  $("#sidebar").classList.remove("open");
  $("#scrim").classList.remove("show");
}

// ------------------------------------------------------------------ upload

function setupUpload() {
  const dlg = $("#upload-dialog"), input = $("#upload-files"), zone = $("#dropzone");
  const refreshLabel = () => {
    const files = [...input.files];
    $("#dropzone-text").innerHTML = files.length ? esc(files.map(f => f.name).join(", ")) : "Drop files here or <u>browse</u>";
    if (files.length && !$("#upload-name").value) $("#upload-name").value = files[0].name.replace(/\.[^.]+$/, "");
  };
  $("#btn-upload").onclick = () => {
    $("#upload-form").reset();
    $("#upload-error").textContent = "";
    refreshLabel();
    dlg.showModal();
  };
  input.onchange = refreshLabel;
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, () => zone.classList.add("drag")));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove("drag")));
  // --- tabs: upload a file / connect a database
  let tab = "file";
  const dialects = state.config.dialects || {};
  $("#db-dialect").innerHTML = Object.entries(dialects).map(([k, d]) => `<option value="${k}">${esc(d.label)}</option>`).join("");
  const syncDialect = () => {
    const d = $("#db-dialect").value;
    $("#db-port").value = dialects[d]?.port ?? "";
    $("#db-schema-field").hidden = d !== "postgres";
  };
  $("#db-dialect").onchange = syncDialect;
  const setTab = (t) => {
    tab = t;
    $$("#add-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
    $("#pane-file").hidden = t !== "file";
    $("#pane-db").hidden = t !== "db";
    $("#btn-db-test").hidden = t !== "db";
    $("#btn-upload-submit").textContent = t === "db" ? "Connect" : "Upload";
    $("#upload-error").textContent = "";
  };
  $$("#add-tabs button").forEach(b => b.onclick = () => setTab(b.dataset.tab));
  const dbBody = () => ({
    name: $("#db-name").value.trim(), dialect: $("#db-dialect").value, host: $("#db-host").value.trim(),
    port: +$("#db-port").value, database: $("#db-database").value.trim(), user: $("#db-user").value.trim(),
    password: $("#db-password").value, db_schema: $("#db-dialect").value === "postgres" ? $("#db-schema").value.trim() : "",
  });
  const dbMissing = (b) => ["host", "database", "user"].filter(k => !b[k]).concat(b.port ? [] : ["port"]);

  $("#btn-db-test").onclick = async () => {
    const body = dbBody(), missing = dbMissing(body);
    $("#upload-error").textContent = missing.length ? `Fill in: ${missing.join(", ")}` : "";
    $("#db-test-result").textContent = "";
    if (missing.length) return;
    const btn = $("#btn-db-test");
    btn.disabled = true; btn.textContent = "Testing…";
    try {
      const r = await api("/api/connections/test", { json: body });
      $("#db-test-result").textContent = `✓ Connected to ${r.version} · ${r.tables} tables/views`;
    } catch (e) {
      $("#upload-error").textContent = e.message;
    } finally {
      btn.disabled = false; btn.textContent = "Test connection";
    }
  };

  $("#btn-upload").onclick = () => {
    $("#upload-form").reset();
    $("#upload-error").textContent = "";
    $("#db-test-result").textContent = "";
    refreshLabel();
    syncDialect();
    setTab(tab);
    dlg.showModal();
  };

  $("#btn-upload-submit").onclick = async () => {
    const btn = $("#btn-upload-submit");
    const label = btn.textContent;
    let request;
    if (tab === "db") {
      const body = dbBody(), missing = dbMissing(body);
      if (missing.length) { $("#upload-error").textContent = `Fill in: ${missing.join(", ")}`; return; }
      request = () => api("/api/connections", { json: body });
      btn.textContent = "Connecting…";
    } else {
      const files = [...input.files];
      if (!files.length) { $("#upload-error").textContent = "Choose at least one file."; return; }
      const fd = new FormData();
      fd.append("name", $("#upload-name").value.trim());
      files.forEach(f => fd.append("files", f));
      request = () => api("/api/sources", { method: "POST", body: fd });
      btn.textContent = "Uploading…";
    }
    btn.disabled = true;
    try {
      const res = await request();
      dlg.close();
      toast(tab === "db" ? "Database connected" : "Data source added");
      await loadSources(res.id);
    } catch (e) {
      $("#upload-error").textContent = e.message;
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  };
}

// ------------------------------------------------------------------ source settings

function setupSettings() {
  const dlg = $("#settings-dialog");
  $("#btn-source-settings").onclick = async () => {
    const src = state.sources.find(s => s.id === state.sourceId);
    if (!src) return;
    $("#settings-error").textContent = "";
    $("#settings-source-name").textContent = src.name;
    try {
      const s = await api(`/api/sources/${encodeURIComponent(src.id)}/settings`);
      $("#settings-notes").value = s.notes || "";
      $("#settings-hidden").value = (s.hidden || []).join("\n");
      $("#settings-global").innerHTML = s.global_hidden.length
        ? s.global_hidden.map(p => `<code>${esc(p)}</code>`).join(" ") : "(none)";
      dlg.showModal();
    } catch (e) { toast(e.message, true); }
  };
  $("#btn-settings-save").onclick = async () => {
    const btn = $("#btn-settings-save");
    btn.disabled = true;
    try {
      await api(`/api/sources/${encodeURIComponent(state.sourceId)}/settings`, {
        method: "PUT",
        json: { notes: $("#settings-notes").value, hidden: $("#settings-hidden").value.split("\n").map(s => s.trim()).filter(Boolean) },
      });
      dlg.close();
      toast("Settings saved");
      await loadSchema(false);
    } catch (e) {
      $("#settings-error").textContent = e.message;
    } finally { btn.disabled = false; }
  };
}

// ------------------------------------------------------------------ init

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
}

async function init() {
  applyTheme(store.get("theme", null));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store.get("theme", null)));
  $("#btn-theme").onclick = () => {
    const next = isDark() ? "light" : "dark";
    store.set("theme", next);
    applyTheme(next);
  };

  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { json: { password: $("#login-password").value } });
      location.reload();
    } catch (err) {
      $("#login-error").textContent = err.message;
    }
  };
  $("#login-dialog").addEventListener("cancel", e => e.preventDefault());  // can't dismiss without logging in
  const authState = await api("/api/auth");
  if (!authState.authenticated) { showLogin(); return; }
  if (authState.required) {
    const btn = $("#btn-logout");
    btn.hidden = false;
    btn.onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
  }

  state.config = await api("/api/config");
  const modelSel = $("#model-select");
  const savedModel = store.get("model", state.config.default_model);
  const models = state.config.models.includes(savedModel) ? state.config.models : [savedModel, ...state.config.models];
  modelSel.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("") +
    '<option value="__custom">Custom model…</option>';
  modelSel.value = savedModel;
  modelSel.onchange = () => {
    if (modelSel.value === "__custom") {
      const m = prompt("OpenRouter model id (e.g. openai/gpt-5.5):", "");
      if (m && m.trim()) {
        const opt = new Option(m.trim(), m.trim());
        modelSel.insertBefore(opt, modelSel.lastElementChild);
        modelSel.value = m.trim();
      } else {
        modelSel.value = store.get("model", state.config.default_model);
        return;
      }
    }
    store.set("model", modelSel.value);
  };
  if (!state.config.key_configured) toast("Server has no OPENROUTER_KEY configured.", true);

  $("#opt-summary").checked = store.get("summary", true);
  $("#opt-summary").onchange = e => store.set("summary", e.target.checked);

  const ta = $("#question");
  ta.addEventListener("input", () => autosize(ta));
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#composer").onsubmit = e => {
    e.preventDefault();
    const q = ta.value;
    if (!q.trim() || state.busy) return;
    ta.value = "";
    autosize(ta);
    ask(q);
  };
  $("#btn-new-chat").onclick = () => { resetConversation(); ta.focus(); };

  $("#suggestions").onclick = e => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    if (chip.dataset.refresh) loadSuggestions(true);
    else ask(chip.textContent);
  };

  $("#source-select").onchange = e => selectSource(e.target.value);
  $("#btn-delete-source").onclick = async () => {
    const src = state.sources.find(s => s.id === state.sourceId);
    const msg = src && src.kind !== "sqlite"
      ? `Remove the connection "${src.name}"? The database itself is not touched.`
      : `Delete data source "${src?.name}"? This cannot be undone.`;
    if (!src || src.builtin || !confirm(msg)) return;
    try {
      await api(`/api/sources/${encodeURIComponent(src.id)}`, { method: "DELETE" });
      toast("Data source deleted");
      store.set("source", null);
      await loadSources();
    } catch (err) { toast(err.message, true); }
  };
  $("#schema-filter").oninput = () => state.schema && renderSchema();
  $("#btn-refresh-schema").onclick = async () => { await loadSchema(true); toast("Schema refreshed"); };
  $("#schema-tree").onclick = e => {
    const ins = e.target.closest("[data-insert]");
    if (ins) {
      e.stopPropagation();
      ta.value = (ta.value ? ta.value.trimEnd() + " " : "") + ins.dataset.insert + " ";
      autosize(ta); ta.focus();
      return;
    }
    const head = e.target.closest(".tbl-head");
    if (head) head.parentElement.classList.toggle("open");
  };
  $("#btn-expand-all").onclick = () => {
    const tbls = $$(".tbl");
    const open = !tbls.every(t => t.classList.contains("open"));
    tbls.forEach(t => t.classList.toggle("open", open));
    $("#btn-expand-all").textContent = open ? "Collapse all" : "Expand all";
  };

  $("#btn-history").onclick = () => {
    const d = $("#history-drawer");
    const open = !d.classList.contains("open");
    closeDrawers();
    if (open) { d.classList.add("open"); $("#scrim").classList.add("show"); loadHistory(); }
  };
  $("#btn-close-history").onclick = closeDrawers;
  $("#btn-sidebar").onclick = () => {
    const open = !$("#sidebar").classList.contains("open");
    closeDrawers();
    if (open) { $("#sidebar").classList.add("open"); $("#scrim").classList.add("show"); }
  };
  $("#scrim").onclick = closeDrawers;
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawers(); });
  $$("#history-tabs button").forEach(b => b.onclick = () => {
    state.historyTab = b.dataset.tab;
    $$("#history-tabs button").forEach(x => x.classList.toggle("active", x === b));
    loadHistory();
  });
  $("#btn-clear-history").onclick = async () => {
    if (!confirm("Clear all unstarred history for this data source?")) return;
    await api(`/api/history?source_id=${encodeURIComponent(state.sourceId)}`, { method: "DELETE" });
    loadHistory();
  };

  setupUpload();
  setupSettings();
  await loadSources();
  ta.focus();
}

init().catch(e => toast("Failed to start: " + e.message, true));
