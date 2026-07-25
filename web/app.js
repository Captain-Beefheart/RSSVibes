/* ============================================================
   Local RSS Dashboard — client app (vanilla JS, no build step)
   ============================================================ */
"use strict";

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const uid = () => "w" + Date.now().toString(36) + Math.floor(Math.random() * 1e5).toString(36);
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

/* ---------------------------------------------------------------- state */
const ACCENTS = ["teal", "blue", "violet", "rose", "amber", "green", "slate"];

function defaultState() {
  return {
    version: 1,
    settings: { theme: "light", accent: "teal", columns: 3, refreshMins: 15, brand: "RSSVibes" },
    activeTabId: "home",
    tabs: [{
      id: "home", name: "Home",
      widgets: [
        { id: uid(), type: "feed", col: 0, title: "Hacker News", url: "https://hnrss.org/frontpage", max: 12, thumbs: false, read: {} },
        { id: uid(), type: "feed", col: 0, title: "The Verge", url: "https://www.theverge.com/rss/index.xml", max: 10, thumbs: true, read: {} },
        { id: uid(), type: "feed", col: 1, title: "BBC News", url: "https://feeds.bbci.co.uk/news/rss.xml", max: 12, thumbs: true, read: {} },
        { id: uid(), type: "feed", col: 1, title: "Ars Technica", url: "https://feeds.arstechnica.com/arstechnica/index", max: 10, thumbs: true, read: {} },
        { id: uid(), type: "clock", col: 2, title: "Clock", fmt24: false },
        { id: uid(), type: "notes", col: 2, title: "Notes", text: "Welcome to your dashboard 👋\n\n• Drag widgets by their header to rearrange\n• “＋ Feed” adds any RSS/Atom URL (or paste a site URL and it’ll find the feed)\n• “⚙” up top changes theme, accent & columns\n\nEverything saves locally to data/state.json." },
        { id: uid(), type: "bookmarks", col: 2, title: "Bookmarks", links: [
          { title: "Wikipedia", url: "https://wikipedia.org" },
          { title: "GitHub", url: "https://github.com" },
          { title: "Hacker News", url: "https://news.ycombinator.com" },
        ] },
      ],
    }],
  };
}

let state = defaultState();
const feedCache = {};          // url -> { items, title, error, loading, fetched }

/* ---------------------------------------------------------------- helpers */
const activeTab = () => state.tabs.find(t => t.id === state.activeTabId) || state.tabs[0];
const widgetsById = () => Object.fromEntries(activeTab().widgets.map(w => [w.id, w]));

function effectiveColumns() {
  const want = state.settings.columns || 3;
  const fit = Math.max(1, Math.floor(window.innerWidth / 320));
  return Math.min(want, fit);
}

let saveTimer = null;
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await fetch("/api/state", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state) });
    } catch (e) { /* offline is fine; localStorage mirror below */ }
    try { localStorage.setItem("dashState", JSON.stringify(state)); } catch (e) {}
  }, 500);
}

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60); if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
  const d = Math.floor(h / 24); if (d < 30) return d + "d ago";
  const mo = Math.floor(d / 30); if (mo < 12) return mo + "mo ago";
  return Math.floor(mo / 12) + "y ago";
}

function sanitize(html) {
  const doc = new DOMParser().parseFromString(html || "", "text/html");
  doc.querySelectorAll("script,style,iframe,object,embed,form,link,meta,base,noscript,svg").forEach(n => n.remove());
  doc.querySelectorAll("*").forEach(el => {
    [...el.attributes].forEach(a => {
      const n = a.name.toLowerCase();
      const v = (a.value || "").trim().toLowerCase();
      if (n.startsWith("on")) el.removeAttribute(a.name);
      else if ((n === "href" || n === "src") && (v.startsWith("javascript:") || v.startsWith("data:text/html"))) el.removeAttribute(a.name);
    });
  });
  doc.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  return doc.body.innerHTML;
}

/* ---------------------------------------------------------------- toasts */
function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 300); }, 2600);
}

/* ---------------------------------------------------------------- theming */
function applyTheme() {
  const s = state.settings;
  document.documentElement.dataset.theme = s.theme;
  document.documentElement.dataset.accent = s.accent;
  $("#brandName").textContent = s.brand || "RSSVibes";
  document.title = s.brand || "RSSVibes";
}

/* ---------------------------------------------------------------- tabs */
function renderTabs() {
  const wrap = $("#tabs");
  wrap.innerHTML = "";
  state.tabs.forEach(t => {
    const el = document.createElement("div");
    el.className = "tab" + (t.id === state.activeTabId ? " active" : "");
    el.innerHTML = `<span class="tab-label">${esc(t.name)}</span>` +
      (state.tabs.length > 1 ? `<span class="tab-close" title="Delete page">✕</span>` : "");
    el.querySelector(".tab-label").onclick = () => { state.activeTabId = t.id; renderTabs(); renderBoard(); persist(); };
    el.querySelector(".tab-label").ondblclick = () => renameTab(t);
    const close = el.querySelector(".tab-close");
    if (close) close.onclick = (e) => { e.stopPropagation(); deleteTab(t); };
    wrap.appendChild(el);
  });
}

function renameTab(t) {
  const name = prompt("Rename page:", t.name);
  if (name && name.trim()) { t.name = name.trim(); renderTabs(); persist(); }
}

function addTab() {
  const t = { id: uid(), name: "Page " + (state.tabs.length + 1), widgets: [] };
  state.tabs.push(t);
  state.activeTabId = t.id;
  renderTabs(); renderBoard(); persist();
}

function deleteTab(t) {
  if (!confirm(`Delete page “${t.name}” and its widgets?`)) return;
  const i = state.tabs.indexOf(t);
  state.tabs.splice(i, 1);
  if (state.activeTabId === t.id) state.activeTabId = state.tabs[Math.max(0, i - 1)].id;
  renderTabs(); renderBoard(); persist();
}

/* ---------------------------------------------------------------- board */
function renderBoard() {
  const board = $("#board");
  const tab = activeTab();
  const cols = effectiveColumns();
  board.style.gridTemplateColumns = `repeat(${cols}, minmax(0,1fr))`;
  board.innerHTML = "";

  if (!tab.widgets.length) {
    board.innerHTML = `<div class="empty-board"><h2>This page is empty</h2>
      <p>Add a feed or widget from the toolbar above.</p></div>`;
    return;
  }

  const colEls = [];
  for (let i = 0; i < cols; i++) {
    const c = document.createElement("div");
    c.className = "column";
    c.dataset.col = i;
    wireColumnDnd(c);
    board.appendChild(c);
    colEls.push(c);
  }
  tab.widgets.forEach(w => {
    const ci = Math.min(w.col || 0, cols - 1);
    colEls[ci].appendChild(makeWidget(w));
  });
  colEls.forEach(c => {
    if (!c.children.length) {
      const ph = document.createElement("div");
      ph.className = "column-empty";
      ph.textContent = "Drop widgets here";
      c.appendChild(ph);
    }
  });
}

/* ---------------------------------------------------------------- widget */
function makeWidget(w) {
  const el = $("#widgetTpl").content.firstElementChild.cloneNode(true);
  el.dataset.id = w.id;
  el.draggable = false;
  if (w.collapsed) el.classList.add("collapsed");
  $(".widget-title", el).textContent = w.title || w.type;

  const body = $(".widget-body", el);
  renderWidgetBody(w, el, body);

  // header controls
  const head = $(".widget-head", el);
  $(".act-remove", el).onclick = () => removeWidget(w);
  $(".act-collapse", el).onclick = () => { w.collapsed = !w.collapsed; el.classList.toggle("collapsed"); persist(); };
  $(".act-refresh", el).onclick = () => { if (w.type === "feed") fetchFeed(w, true); else if (w.type === "clock") {} };
  $(".act-config", el).onclick = () => configWidget(w);
  if (w.type !== "feed") $(".act-refresh", el).style.display = "none";

  // drag: only arm from the header, so body interactions (text select, links) stay clean
  head.addEventListener("mousedown", (e) => { if (!e.target.closest(".wbtn")) el.draggable = true; });
  $$(".wbtn", el).forEach(b => b.addEventListener("mousedown", e => e.stopPropagation()));
  el.addEventListener("dragstart", (e) => {
    el.classList.add("dragging");
    try { e.dataTransfer.setData("text/plain", w.id); e.dataTransfer.effectAllowed = "move"; } catch (x) {}
  });
  el.addEventListener("dragend", () => { el.draggable = false; el.classList.remove("dragging"); $$(".column").forEach(c => c.classList.remove("drag-over")); commitOrderFromDOM(); });
  window.addEventListener("mouseup", () => { el.draggable = false; }, { once: true });

  return el;
}

function renderWidgetBody(w, el, body) {
  body.innerHTML = "";
  if (w.type === "feed")       renderFeed(w, el, body);
  else if (w.type === "notes") renderNotes(w, body);
  else if (w.type === "clock") renderClock(w, body);
  else if (w.type === "bookmarks") renderBookmarks(w, body);
}

/* -------- feed widget */
function renderFeed(w, el, body) {
  const data = feedCache[w.url];
  const countEl = $(".widget-count", el);
  if (!data || data.loading) {
    body.innerHTML = `<div class="feed-loading"><span class="spinner"></span>Loading…</div>`;
    countEl.classList.add("zero");
    if (!data) fetchFeed(w);
    return;
  }
  if (data.error) {
    body.innerHTML = `<div class="feed-error">⚠ ${esc(data.error)}</div>`;
    countEl.classList.add("zero");
    return;
  }
  const items = (data.items || []).slice(0, w.max || 12);
  const unread = items.filter(it => !w.read[it.id]).length;
  countEl.textContent = unread;
  countEl.classList.toggle("zero", unread === 0);

  if (!items.length) { body.innerHTML = `<div class="feed-empty">No items.</div>`; return; }

  const frag = document.createDocumentFragment();
  items.forEach(it => {
    const row = document.createElement("div");
    row.className = "feed-item" + (w.read[it.id] ? " read" : "");
    const thumb = (w.thumbs && it.thumb) ? `<img class="fi-thumb" src="${esc(it.thumb)}" loading="lazy" onerror="this.remove()">` : "";
    row.innerHTML = `<span class="fi-dot"></span>
      <div class="fi-main">
        <div class="fi-title">${esc(it.title)}</div>
        <div class="fi-meta"><span>${esc(timeAgo(it.ts))}</span>${it.author ? `<span>· ${esc(it.author)}</span>` : ""}</div>
      </div>${thumb}`;
    row.onclick = () => { markRead(w, it, el); openReader(it, w); };
    frag.appendChild(row);
  });
  body.appendChild(frag);
}

async function fetchFeed(w, force) {
  feedCache[w.url] = { ...(feedCache[w.url] || {}), loading: true };
  refreshWidgetDom(w);
  try {
    const r = await fetch("/api/feed?url=" + encodeURIComponent(w.url) + (force ? "&_=" + Date.now() : ""));
    const j = await r.json();
    if (j.error) feedCache[w.url] = { error: j.error, loading: false };
    else {
      feedCache[w.url] = { items: j.items || [], title: j.title || "", loading: false, fetched: Date.now() };
      if ((!w.title || w.title === "New feed") && j.title) w.title = j.title;
    }
  } catch (e) {
    feedCache[w.url] = { error: "Could not reach server", loading: false };
  }
  refreshWidgetDom(w);
}

function markRead(w, it, el) {
  if (w.read[it.id]) return;
  w.read[it.id] = true;
  // prune read set so it doesn't grow forever
  const ids = new Set((feedCache[w.url]?.items || []).map(x => x.id));
  Object.keys(w.read).forEach(k => { if (!ids.has(k)) delete w.read[k]; });
  refreshWidgetDom(w);
  persist();
}

/* -------- notes widget */
function renderNotes(w, body) {
  const ta = document.createElement("textarea");
  ta.className = "note-area";
  ta.placeholder = "Type a note…";
  ta.value = w.text || "";
  ta.addEventListener("input", () => { w.text = ta.value; persist(); });
  body.appendChild(ta);
}

/* -------- clock widget */
function renderClock(w, body) {
  body.innerHTML = `<div class="clock"><div class="clock-time"></div><div class="clock-date"></div></div>`;
  const tick = () => {
    if (!body.isConnected) return; // stop when re-rendered
    const now = new Date();
    const t = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: !w.fmt24 });
    const d = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    const tEl = $(".clock-time", body), dEl = $(".clock-date", body);
    if (tEl) tEl.textContent = t;
    if (dEl) dEl.textContent = d;
  };
  tick();
  const iv = setInterval(() => { if (!body.isConnected) return clearInterval(iv); tick(); }, 1000);
}

/* -------- bookmarks widget */
function renderBookmarks(w, body) {
  const list = document.createElement("div");
  list.className = "bm-list";
  (w.links || []).forEach((lk, i) => {
    const a = document.createElement("a");
    a.className = "bm-item";
    a.href = lk.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    let host = ""; try { host = new URL(lk.url).hostname; } catch (e) {}
    a.innerHTML = `<img class="bm-fav" src="https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32" onerror="this.style.visibility='hidden'"><span>${esc(lk.title || lk.url)}</span>`;
    list.appendChild(a);
  });
  const add = document.createElement("button");
  add.className = "btn ghost bm-add";
  add.textContent = "＋ Add link";
  add.onclick = () => {
    const url = prompt("Bookmark URL:"); if (!url) return;
    const title = prompt("Label:", url) || url;
    w.links = w.links || []; w.links.push({ title, url });
    refreshWidgetDom(w); persist();
  };
  body.appendChild(list); body.appendChild(add);
}

/* refresh a single widget's DOM in place */
function refreshWidgetDom(w) {
  const el = $(`.widget[data-id="${w.id}"]`);
  if (!el) return;
  renderWidgetBody(w, el, $(".widget-body", el));
}

function removeWidget(w) {
  const tab = activeTab();
  tab.widgets = tab.widgets.filter(x => x.id !== w.id);
  renderBoard(); persist();
}

/* ---------------------------------------------------------------- drag & drop */
function wireColumnDnd(col) {
  col.addEventListener("dragover", (e) => {
    e.preventDefault();
    col.classList.add("drag-over");
    const dragging = $(".widget.dragging");
    if (!dragging) return;
    const after = dragAfter(col, e.clientY);
    const ph = $(".column-empty", col); if (ph) ph.remove();
    if (after == null) col.appendChild(dragging);
    else col.insertBefore(dragging, after);
  });
  col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("drag-over"); });
  col.addEventListener("drop", (e) => { e.preventDefault(); col.classList.remove("drag-over"); });
}

function dragAfter(col, y) {
  const els = $$(".widget:not(.dragging)", col);
  let closest = null, min = -Infinity;
  els.forEach(el => {
    const box = el.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > min) { min = offset; closest = el; }
  });
  return closest;
}

function commitOrderFromDOM() {
  const map = widgetsById();
  const list = [];
  $$(".column").forEach((c, ci) => {
    $$(".widget", c).forEach(el => {
      const w = map[el.dataset.id];
      if (w) { w.col = ci; list.push(w); }
    });
  });
  if (list.length === activeTab().widgets.length) activeTab().widgets = list;
  // restore empty-column placeholders
  $$(".column").forEach(c => { if (!c.querySelector(".widget") && !c.querySelector(".column-empty")) {
    const ph = document.createElement("div"); ph.className = "column-empty"; ph.textContent = "Drop widgets here"; c.appendChild(ph);
  }});
  persist();
}

/* ---------------------------------------------------------------- reader */
function openReader(it, w) {
  $("#readerBody").innerHTML =
    `<h1>${esc(it.title)}</h1>
     <div class="r-meta">${esc(w.title)} · ${esc(it.author || "")} ${it.date ? "· " + esc(new Date(it.ts * 1000).toLocaleString()) : ""}</div>
     <div class="r-content">${it.summary ? sanitize(it.summary) : "<p>No preview available. Open the original to read the full article.</p>"}</div>`;
  const open = $("#readerOpen");
  open.href = it.link || "#";
  open.style.display = it.link ? "" : "none";
  $("#reader").hidden = false;
  $("#scrim").hidden = false;
}
function closeReader() { $("#reader").hidden = true; $("#scrim").hidden = true; }

/* ---------------------------------------------------------------- modal */
function modal(title, bodyHTML, footHTML) {
  const wrap = $("#modalWrap");
  $("#modal").innerHTML = `<h2>${esc(title)}</h2><div class="modal-body">${bodyHTML}</div><div class="modal-foot">${footHTML}</div>`;
  wrap.hidden = false;
  wrap.onclick = (e) => { if (e.target === wrap) closeModal(); };
  return $("#modal");
}
function closeModal() { $("#modalWrap").hidden = true; }

/* -------- add feed */
function addFeedDialog() {
  const m = modal("Add RSS feed",
    `<div class="field">
       <label>Feed or website URL</label>
       <input type="text" id="feedUrl" placeholder="https://example.com  or  https://example.com/rss.xml" autocomplete="off">
       <div class="hint">Paste a feed URL directly, or a site's homepage — we'll try to find its feed.</div>
     </div>
     <div class="field"><label>Title (optional)</label><input type="text" id="feedTitle" placeholder="Auto-detected from the feed"></div>
     <div id="feedStatus" class="hint"></div>`,
    `<button class="btn ghost" id="mCancel">Cancel</button><button class="btn primary" id="mAdd">Add feed</button>`);
  const input = $("#feedUrl", m); input.focus();
  $("#mCancel", m).onclick = closeModal;
  const go = async () => {
    let url = input.value.trim();
    if (!url) return;
    const status = $("#feedStatus", m);
    status.innerHTML = `<span class="spinner"></span> Checking…`;
    if (!/^https?:\/\//i.test(url)) url = "https://" + url;
    // try as-is first (fast path), else discover
    let feedUrl = url, title = $("#feedTitle", m).value.trim();
    try {
      let r = await fetch("/api/feed?url=" + encodeURIComponent(url));
      let j = await r.json();
      if (j.error || !j.items) {
        const d = await (await fetch("/api/discover?url=" + encodeURIComponent(url))).json();
        if (d.found) { feedUrl = d.found; if (!title) title = d.title || ""; }
        else { status.innerHTML = `<span style="color:#d0596e">⚠ ${esc(d.error || "No feed found")}</span>`; return; }
      } else if (!title) title = j.title || "";
    } catch (e) { status.innerHTML = `<span style="color:#d0596e">⚠ ${esc(e.message)}</span>`; return; }

    const tab = activeTab();
    const cols = effectiveColumns();
    const counts = Array(cols).fill(0);
    tab.widgets.forEach(w => counts[Math.min(w.col, cols - 1)]++);
    const col = counts.indexOf(Math.min(...counts));
    tab.widgets.push({ id: uid(), type: "feed", col, title: title || "New feed", url: feedUrl, max: 12, thumbs: true, read: {} });
    closeModal(); renderBoard(); persist();
    toast("Feed added");
  };
  $("#mAdd", m).onclick = go;
  input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

/* -------- add widget */
function addWidgetDialog() {
  const types = [
    { t: "feed", ico: "📰", label: "RSS Feed" },
    { t: "notes", ico: "📝", label: "Notes" },
    { t: "clock", ico: "🕑", label: "Clock" },
    { t: "bookmarks", ico: "🔖", label: "Bookmarks" },
  ];
  const m = modal("Add a widget",
    `<div class="picker">${types.map(x => `<div class="pick" data-t="${x.t}"><span class="pico">${x.ico}</span>${x.label}</div>`).join("")}</div>`,
    `<button class="btn ghost" id="mCancel">Cancel</button>`);
  $("#mCancel", m).onclick = closeModal;
  $$(".pick", m).forEach(p => p.onclick = () => {
    const t = p.dataset.t;
    closeModal();
    if (t === "feed") return addFeedDialog();
    const tab = activeTab();
    const base = { id: uid(), type: t, col: 0 };
    if (t === "notes") Object.assign(base, { title: "Notes", text: "" });
    if (t === "clock") Object.assign(base, { title: "Clock", fmt24: false });
    if (t === "bookmarks") Object.assign(base, { title: "Bookmarks", links: [] });
    tab.widgets.push(base);
    renderBoard(); persist();
  });
}

/* -------- per-widget config */
function configWidget(w) {
  let bodyHTML = `<div class="field"><label>Title</label><input type="text" id="cfgTitle" value="${esc(w.title || "")}"></div>`;
  if (w.type === "feed") {
    bodyHTML += `<div class="field"><label>Feed URL</label><input type="text" id="cfgUrl" value="${esc(w.url || "")}"></div>
      <div class="field"><label>Max items: <span id="cfgMaxVal">${w.max || 12}</span></label>
        <input type="range" id="cfgMax" min="3" max="30" value="${w.max || 12}"></div>
      <label style="display:flex;gap:8px;align-items:center;font-weight:600;color:var(--ink-soft)">
        <input type="checkbox" id="cfgThumbs" ${w.thumbs ? "checked" : ""}> Show thumbnails</label>`;
  }
  if (w.type === "clock") {
    bodyHTML += `<label style="display:flex;gap:8px;align-items:center;font-weight:600;color:var(--ink-soft)">
      <input type="checkbox" id="cfg24" ${w.fmt24 ? "checked" : ""}> 24-hour time</label>`;
  }
  const m = modal("Widget settings", bodyHTML,
    `<button class="btn ghost" id="mDel">Remove</button><div style="flex:1"></div>
     <button class="btn ghost" id="mCancel">Cancel</button><button class="btn primary" id="mSave">Save</button>`);
  $("#mCancel", m).onclick = closeModal;
  $("#mDel", m).onclick = () => { closeModal(); removeWidget(w); };
  const maxR = $("#cfgMax", m); if (maxR) maxR.oninput = () => $("#cfgMaxVal", m).textContent = maxR.value;
  $("#mSave", m).onclick = () => {
    w.title = $("#cfgTitle", m).value.trim() || w.title;
    if (w.type === "feed") {
      const newUrl = $("#cfgUrl", m).value.trim();
      const changed = newUrl && newUrl !== w.url;
      if (newUrl) w.url = newUrl;
      w.max = +$("#cfgMax", m).value;
      w.thumbs = $("#cfgThumbs", m).checked;
      if (changed) { delete feedCache[w.url]; fetchFeed(w, true); }
    }
    if (w.type === "clock") w.fmt24 = $("#cfg24", m).checked;
    closeModal(); renderBoard(); persist();
  };
}

/* -------- settings */
function settingsDialog() {
  const s = state.settings;
  const m = modal("Settings",
    `<div class="field"><label>Dashboard name</label><input type="text" id="setBrand" value="${esc(s.brand)}"></div>
     <div class="field"><label>Theme</label>
       <div class="seg" id="setTheme">
         <button data-v="light" class="${s.theme === "light" ? "active" : ""}">☀ Light</button>
         <button data-v="dark" class="${s.theme === "dark" ? "active" : ""}">🌙 Dark</button>
       </div></div>
     <div class="field"><label>Accent</label><div class="swatches" id="setAccent"></div></div>
     <div class="field"><label>Columns: <span id="setColsVal">${s.columns}</span></label>
       <input type="range" id="setCols" min="1" max="5" value="${s.columns}"></div>
     <div class="field"><label>Auto-refresh feeds every: <span id="setRefVal">${s.refreshMins}</span> min</label>
       <input type="range" id="setRef" min="0" max="60" step="5" value="${s.refreshMins}">
       <div class="hint">0 = manual refresh only</div></div>
     <div class="field"><label>Backup</label>
       <div style="display:flex;gap:8px"><button class="btn" id="setExport">Export JSON</button>
       <button class="btn" id="setImport">Import JSON</button></div></div>
     <div class="field"><label>Import subscriptions</label>
       <div style="display:flex;gap:8px"><button class="btn" id="setImportOpml">Import Netvibes / OPML…</button></div>
       <div class="hint">A Netvibes export (.zip or .opml) or any OPML file from another reader — your feeds are added as new pages.</div></div>`,
    `<button class="btn ghost" id="mCancel">Close</button>`);

  // accent swatches
  const accWrap = $("#setAccent", m);
  ACCENTS.forEach(a => {
    const sw = document.createElement("div");
    sw.className = "swatch" + (s.accent === a ? " active" : "");
    sw.dataset.a = a;
    sw.style.background = ({ teal: "#0f9b8e", blue: "#2f6fed", violet: "#7c5cff", rose: "#e5487a", amber: "#d98a13", green: "#2fa84f", slate: "#52657a" })[a];
    sw.onclick = () => { s.accent = a; $$(".swatch", accWrap).forEach(x => x.classList.remove("active")); sw.classList.add("active"); applyTheme(); persist(); };
    accWrap.appendChild(sw);
  });
  $$("#setTheme button", m).forEach(b => b.onclick = () => {
    s.theme = b.dataset.v; $$("#setTheme button", m).forEach(x => x.classList.remove("active")); b.classList.add("active"); applyTheme(); persist();
  });
  $("#setBrand", m).oninput = (e) => { s.brand = e.target.value; applyTheme(); persist(); };
  const cols = $("#setCols", m);
  cols.oninput = () => { $("#setColsVal", m).textContent = cols.value; s.columns = +cols.value; renderBoard(); persist(); };
  const ref = $("#setRef", m);
  ref.oninput = () => { $("#setRefVal", m).textContent = ref.value; s.refreshMins = +ref.value; scheduleRefresh(); persist(); };
  $("#mCancel", m).onclick = closeModal;
  $("#setExport", m).onclick = exportState;
  $("#setImport", m).onclick = importState;
  $("#setImportOpml", m).onclick = importOpml;
}

function exportState() {
  const blob = new Blob([JSON.stringify(state, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "dashboard-backup.json"; a.click();
  URL.revokeObjectURL(a.href);
}
function importState() {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = "application/json";
  inp.onchange = () => {
    const f = inp.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = () => {
      try { state = JSON.parse(r.result); closeModal(); boot(); toast("Dashboard imported"); persist(); }
      catch (e) { toast("Invalid JSON file", true); }
    };
    r.readAsText(f);
  };
  inp.click();
}

/* -------- import Netvibes / OPML subscriptions */
function importOpml() {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = ".opml,.xml,.zip,application/xml,text/xml,application/zip";
  inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    toast("Importing subscriptions…");
    try {
      const buf = await f.arrayBuffer();
      const r = await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: buf,
      });
      const data = await r.json();
      if (data.error) return toast(data.error, true);
      applyOpmlImport(data);
    } catch (e) {
      toast("Import failed: " + e.message, true);
    }
  };
  inp.click();
}

function applyOpmlImport(data) {
  const pages = (data.pages || []).filter(p => p.feeds && p.feeds.length);
  if (!pages.length) return toast("No feeds found in that file", true);

  // Widen the global column count to fit the imported layout (capped at 5).
  let maxCols = state.settings.columns || 3;
  pages.forEach(p => { if (p.columns) maxCols = Math.max(maxCols, p.columns); });
  state.settings.columns = Math.min(5, Math.max(1, maxCols));

  let firstNewTab = null;
  pages.forEach(p => {
    const cols = p.columns || state.settings.columns;
    const widgets = p.feeds.map(f => {
      let host = "";
      try { host = new URL(f.htmlUrl || f.url).hostname.replace(/^www\./, ""); } catch (e) {}
      return {
        id: uid(), type: "feed",
        col: Math.max(0, Math.min((f.col || 1) - 1, cols - 1)),
        title: f.title || host || "Feed",
        url: f.url, max: 12, thumbs: true, read: {},
      };
    });
    const tab = { id: uid(), name: p.name || "Imported", widgets };
    state.tabs.push(tab);
    if (!firstNewTab) firstNewTab = tab.id;
  });

  if (firstNewTab) state.activeTabId = firstNewTab;
  closeModal();
  applyTheme(); renderTabs(); renderBoard(); persist();

  const n = data.feedCount, pg = pages.length;
  let msg = `Imported ${n} feed${n === 1 ? "" : "s"} across ${pg} page${pg === 1 ? "" : "s"}`;
  if (data.skipped) msg += ` · skipped ${data.skipped} non-RSS widget${data.skipped === 1 ? "" : "s"}`;
  toast(msg);
}

/* ---------------------------------------------------------------- refresh loop */
let refreshTimer = null;
function scheduleRefresh() {
  clearInterval(refreshTimer);
  const mins = state.settings.refreshMins;
  if (!mins) return;
  refreshTimer = setInterval(refreshAllFeeds, mins * 60 * 1000);
}
function refreshAllFeeds() {
  state.tabs.forEach(t => t.widgets.forEach(w => { if (w.type === "feed") { delete feedCache[w.url]; } }));
  activeTab().widgets.forEach(w => { if (w.type === "feed") fetchFeed(w, true); });
  toast("Refreshing feeds…");
}

/* ---------------------------------------------------------------- boot */
function boot() { applyTheme(); renderTabs(); renderBoard(); scheduleRefresh(); }

async function init() {
  // Wire toolbar
  $("#addTabBtn").onclick = addTab;
  $("#addWidgetBtn").onclick = addWidgetDialog;
  $("#addFeedBtn").onclick = addFeedDialog;
  $("#refreshAllBtn").onclick = refreshAllFeeds;
  $("#settingsBtn").onclick = settingsDialog;
  $("#readerClose").onclick = closeReader;
  $("#scrim").onclick = closeReader;
  document.addEventListener("keydown", e => { if (e.key === "Escape") { closeReader(); closeModal(); } });

  let resizeT = null;
  window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(renderBoard, 150); });

  // Load persisted state (server first, then localStorage, then default seed)
  try {
    const r = await fetch("/api/state");
    const j = await r.json();
    if (j && !j.empty && j.tabs) state = j;
    else {
      const ls = localStorage.getItem("dashState");
      if (ls) state = JSON.parse(ls);
      else persist(); // save the seed so first run is remembered
    }
  } catch (e) {
    const ls = localStorage.getItem("dashState");
    if (ls) try { state = JSON.parse(ls); } catch (x) {}
  }
  // normalize (guard against older/partial states)
  state.settings = Object.assign(defaultState().settings, state.settings || {});
  state.tabs = state.tabs && state.tabs.length ? state.tabs : defaultState().tabs;
  if (!state.tabs.find(t => t.id === state.activeTabId)) state.activeTabId = state.tabs[0].id;
  state.tabs.forEach(t => t.widgets.forEach(w => { if (w.type === "feed" && !w.read) w.read = {}; }));

  boot();
}

document.addEventListener("DOMContentLoaded", init);
