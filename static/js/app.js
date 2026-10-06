/* RAT dashboard - application shell: state, API, repos, filters, routing, modals. */
"use strict";

window.RAT = window.RAT || {};
window.RAT.views = window.RAT.views || {};

(function () {
  // ------------------------------------------------------------------ utils
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  const fmtInt = (n) => (n == null ? "-" : Number(n).toLocaleString("en-US"));
  const fmtF = (x, d) => (x == null || isNaN(x) ? "-" : Number(x).toFixed(d == null ? 2 : d));
  const fmtPct = (x) => (x == null || isNaN(x) ? "-" : (100 * x).toFixed(1) + "%");
  const fmtDay = (ts) => (ts == null ? "-" : new Date(ts * 1000).toISOString().slice(0, 10));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ------------------------------------------------------------------ state
  const state = {
    repos: [], repoId: null, repo: null, refs: null, authors: [],
    filters: { ref: null, since: null, until: null, author_group: null, commit_ids: null },
    tab: "overview", path: "", isDir: true, bucket: "month",
    filesSort: { key: "churn", dir: -1 },
    commits: { page: 1, per: 50, q: "", scope: null, sel: new Set() },
  };
  window.RAT.state = state;

  // ------------------------------------------------------------------ api
  async function api(path, opts) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const detail = body && (body.detail || body.message);
      const err = new Error(typeof detail === "string" ? detail : `request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return body;
  }
  window.RAT.api = api;

  function qs(extra) {
    const p = new URLSearchParams();
    const f = state.filters;
    if (f.ref) p.set("ref", f.ref);
    if (f.since != null) p.set("since", f.since);
    if (f.until != null) p.set("until", f.until);
    if (f.author_group != null) p.set("author_group", f.author_group);
    if (f.commit_ids) p.set("commits", f.commit_ids.join(","));
    for (const k of Object.keys(extra || {})) {
      const v = extra[k];
      if (v !== undefined && v !== null && v !== "") p.set(k, v);
    }
    const s = p.toString();
    return s ? "?" + s : "";
  }

  // ------------------------------------------------------------------ toasts
  function toast(msg, kind, detail, ms) {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.innerHTML = esc(msg) + (detail ? "<small>" + esc(detail) + "</small>" : "");
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, ms || (kind === "error" ? 8000 : 3800));
    setTimeout(() => el.remove(), (ms || (kind === "error" ? 8000 : 3800)) + 350);
  }

  // ------------------------------------------------------------------ modals
  function openModal(id) { $("#" + id).hidden = false; }
  function closeModal(id) { $("#" + id).hidden = true; }
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal(b.dataset.close)));
  $$(".modal-backdrop").forEach((bd) => bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.hidden = true; }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $$(".modal-backdrop").forEach((bd) => { bd.hidden = true; });
      closeDrawer();
    }
  });

  function confirmBox(text, danger) {
    return new Promise((resolve) => {
      const bd = document.createElement("div");
      bd.className = "modal-backdrop";
      bd.innerHTML = `<div class="modal" style="width:min(430px,100%)">
        <div class="modal-body"><p style="margin-top:0">${esc(text)}</p>
        <div class="modal-actions">
          <button class="btn" data-no>Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-yes>${danger ? "Delete" : "Confirm"}</button>
        </div></div></div>`;
      document.body.appendChild(bd);
      bd.addEventListener("mousedown", (e) => { if (e.target === bd) { bd.remove(); resolve(false); } });
      $("[data-no]", bd).onclick = () => { bd.remove(); resolve(false); };
      $("[data-yes]", bd).onclick = () => { bd.remove(); resolve(true); };
    });
  }

  // ------------------------------------------------------------------ hash state
  function writeHash() {
    const p = new URLSearchParams();
    if (state.repoId != null) p.set("r", String(state.repoId));
    p.set("tab", state.tab);
    const f = state.filters;
    if (f.ref) p.set("ref", f.ref);
    if (f.since != null) p.set("since", String(f.since));
    if (f.until != null) p.set("until", String(f.until));
    if (f.author_group != null) p.set("a", String(f.author_group));
    if (f.commit_ids) p.set("cs", f.commit_ids.join(","));
    if (state.tab === "files" && state.path) { p.set("path", state.path); p.set("dir", state.isDir ? "1" : "0"); }
    if (state.tab === "commits") {
      if (state.commits.scope) { p.set("sp", state.commits.scope.path); p.set("spd", state.commits.scope.is_dir ? "1" : "0"); }
      if (state.commits.q) p.set("q", state.commits.q);
      if (state.commits.page > 1) p.set("page", String(state.commits.page));
    }
    if (state.tab === "overview" && state.bucket !== "month") p.set("bucket", state.bucket);
    const h = "#" + p.toString();
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  function readHash() {
    const p = new URLSearchParams(location.hash.replace(/^#/, ""));
    const num = (k) => (p.has(k) && p.get(k) !== "" ? Number(p.get(k)) : null);
    return {
      r: num("r"), tab: p.get("tab") || "overview",
      ref: p.get("ref") || null, since: num("since"), until: num("until"),
      a: num("a"), cs: p.get("cs") ? p.get("cs").split(",").map(Number).filter((x) => !isNaN(x)) : null,
      path: p.get("path") || "", dir: p.get("dir") !== "0",
      sp: p.get("sp") || null, spd: p.get("spd") !== "0",
      q: p.get("q") || "", page: num("page") || 1, bucket: p.get("bucket") || "month",
    };
  }

  function applyHash() {
    const h = readHash();
    state.tab = h.tab;
    state.filters = { ref: h.ref, since: h.since, until: h.until, author_group: h.a, commit_ids: h.cs };
    state.path = h.path; state.isDir = h.dir;
    state.commits.scope = h.sp ? { path: h.sp, is_dir: h.spd } : null;
    state.commits.q = h.q; state.commits.page = h.page;
    state.commits.sel = new Set();
    state.bucket = h.bucket;
    syncFilterControls();
    if (h.r != null && h.r !== state.repoId) { selectRepo(h.r, true); return true; }
    return false;
  }
  window.addEventListener("hashchange", () => { if (!applyHash()) render(); });

  // ------------------------------------------------------------------ chart helper
  const charts = {};
  function chart(sel) {
    const el = $(sel);
    if (!el) return null;
    let c = charts[sel];
    if (!c || c.isDisposed()) { c = echarts.init(el); charts[sel] = c; }
    return c;
  }
  function disposeChart(sel) { if (charts[sel] && !charts[sel].isDisposed()) charts[sel].dispose(); }
  window.addEventListener("resize", debounce(() => {
    Object.values(charts).forEach((c) => { if (!c.isDisposed()) c.resize(); });
  }, 120));

  // ------------------------------------------------------------------ health
  async function loadHealth() {
    try {
      const h = await api("/api/health");
      $("#health").className = "health ok";
      $("#health").textContent = h.git;
    } catch (e) {
      $("#health").className = "health bad";
      $("#health").textContent = "server unreachable";
    }
  }

  // ------------------------------------------------------------------ repos
  let pollTimer = null, prevBusy = false;

  async function loadRepos(first) {
    try {
      const data = await api("/api/repos");
      const prev = state.repos;
      state.repos = data.repos;
      renderRepoList();
      const active = state.repos.find((r) => r.id === state.repoId);
      if (active) {
        const wasBusy = prev.find((r) => r.id === active.id);
        state.repo = active;
        renderTopbar();
        if (wasBusy && ["cloning", "parsing", "pending"].includes(wasBusy.status) && active.status === "ready") {
          toast(`"${active.name}" is ready`, "success", active.message);
          await afterRepoReady();
        } else if (wasBusy && ["cloning", "parsing", "pending"].includes(wasBusy.status) && active.status === "error") {
          toast(`"${active.name}" failed`, "error", active.message);
          render();
        } else if (["cloning", "parsing", "pending"].includes(active.status)) {
          render();
        }
      }
      const busy = state.repos.some((r) => ["cloning", "parsing", "pending"].includes(r.status));
      prevBusy = busy;
      if (busy) schedulePoll();
      if (first) {
        if (state.repoId == null && state.repos.length) {
          const preferred = state.repos.find((r) => r.status === "ready") || state.repos[0];
          selectRepo(preferred.id, true);
        } else {
          render();
        }
      } else if (state.repoId == null && state.repos.length && !applyHash()) {
        selectRepo(state.repos[0].id, true);
      }
    } catch (e) {
      toast("could not load repositories", "error", e.message);
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => loadRepos(false), 1200);
  }

  async function afterRepoReady() {
    await loadRefs();
    await loadAuthors();
    render();
  }

  function renderRepoList() {
    const box = $("#repo-list");
    if (!state.repos.length) {
      box.innerHTML = `<p class="muted-small">Nothing here yet. Add a repository to begin.</p>`;
      return;
    }
    box.innerHTML = state.repos.map((r) => {
      const busy = ["cloning", "parsing", "pending"].includes(r.status);
      const pill = busy ? `<span class="status-pill busy">${esc(r.phase || r.status)}</span>`
        : r.status === "ready" ? `<span class="status-pill ready">ready</span>`
        : `<span class="status-pill error">error</span>`;
      const src = r.source_type === "zip" ? "zip" : "git";
      const bar = busy ? `<div class="progress"><div class="progress-fill" style="width:${Math.round((r.progress || 0) * 100)}%"></div></div>` : "";
      const meta = r.status === "ready" ? `${fmtInt(r.commit_count)} commits` : (busy ? esc(r.message || "") : esc(r.message || ""));
      return `<div class="repo-item ${r.id === state.repoId ? "active" : ""}" data-repo="${r.id}">
        <div class="r-top">
          <span class="r-name" title="${esc(r.name)}">${esc(r.name)}</span>
          ${pill}
          <span class="r-actions">
            <button class="ico-btn" data-reanalyze="${r.id}" title="Re-analyze from scratch">&#8635;</button>
            <button class="ico-btn" data-delete="${r.id}" title="Delete repository">&times;</button>
          </span>
        </div>
        <div class="r-source" title="${esc(r.source)}">${src}: ${esc(r.source)}</div>
        <div class="r-meta">${meta}</div>
        ${bar}
      </div>`;
    }).join("");
    $$(".repo-item", box).forEach((el) => el.addEventListener("click", (e) => {
      if (e.target.closest("[data-delete]") || e.target.closest("[data-reanalyze]")) return;
      selectRepo(Number(el.dataset.repo));
    }));
    $$("[data-delete]", box).forEach((b) => b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const repo = state.repos.find((r) => r.id === Number(b.dataset.delete));
      if (!(await confirmBox(`Delete "${repo.name}" and all of its analysed data?`, true))) return;
      try {
        await api(`/api/repos/${repo.id}`, { method: "DELETE" });
        toast(`"${repo.name}" deleted`);
        if (state.repoId === repo.id) { state.repoId = null; state.repo = null; }
        state.repoId = null;
        await loadRepos(false);
      } catch (err) { toast("delete failed", "error", err.message); }
    }));
    $$("[data-reanalyze]", box).forEach((b) => b.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/repos/${b.dataset.reanalyze}/reanalyze`, { method: "POST" });
        toast("re-analyzing...");
        loadRepos(false);
      } catch (err) { toast("re-analyze failed", "error", err.message); }
    }));
  }

  async function selectRepo(id, initial) {
    state.repoId = id;
    state.repo = state.repos.find((r) => r.id === id) || null;
    if (!initial) {
      state.filters = { ref: null, since: null, until: null, author_group: null, commit_ids: null };
      state.tab = "overview"; state.path = ""; state.isDir = true;
      state.commits = { page: 1, per: state.commits.per, q: "", scope: null, sel: new Set() };
    }
    disposeChart("#ov-timeline"); disposeChart("#ov-bars"); disposeChart("#ov-owners");
    disposeChart("#files-chart"); disposeChart("#files-owners");
    syncFilterControls();
    try {
      if (!state.repo) { state.repo = await api(`/api/repos`).then((d) => d.repos.find((r) => r.id === id)); }
      if (state.repo && state.repo.status === "ready") { await loadRefs(); await loadAuthors(); }
      else { state.refs = null; state.authors = []; }
    } catch (e) { /* the render() below will show the state */ }
    writeHash();
    render();
  }

  function renderTopbar() {
    const el = $("#topbar-repo");
    if (!state.repo) { el.innerHTML = `<span class="muted">no repository selected</span>`; return; }
    const r = state.repo;
    const busy = ["cloning", "parsing", "pending"].includes(r.status);
    el.innerHTML = `<b>${esc(r.name)}</b>
      <span class="muted-small">${r.status === "ready" ? fmtInt(r.commit_count) + " commits - " + esc(r.message || "") : esc(r.message || r.status)}</span>
      ${busy ? `<div class="progress" style="max-width:260px;margin-top:4px"><div class="progress-fill" style="width:${Math.round((r.progress || 0) * 100)}%"></div></div>` : ""}`;
  }

  // ------------------------------------------------------------------ refs / authors
  async function loadRefs() {
    try { state.refs = await api(`/api/repos/${state.repoId}/refs`); }
    catch (e) { state.refs = null; }
    renderRefSelect();
  }

  function renderRefSelect() {
    const sel = $("#f-ref");
    const r = state.refs;
    if (!r) { sel.innerHTML = `<option>HEAD</option>`; sel.disabled = true; return; }
    sel.disabled = false;
    const opts = [`<option value="HEAD">HEAD (default branch)</option>`];
    const vals = ["HEAD"];
    (r.branches || []).forEach((b) => { vals.push(b.name); opts.push(`<option value="${esc(b.name)}">branch: ${esc(b.name)}</option>`); });
    (r.tags || []).forEach((t) => { vals.push(t.name); opts.push(`<option value="${esc(t.name)}">tag: ${esc(t.name)}</option>`); });
    const activeRef = state.filters.ref;
    if (activeRef && !vals.includes(activeRef)) {
      opts.push(`<option value="${esc(activeRef)}">${esc(String(activeRef).slice(0, 12))}</option>`);
    }
    opts.push(`<option value="__custom__">custom ref / sha\u2026</option>`);
    sel.innerHTML = opts.join("");
    sel.value = activeRef || (r.active && !/^[0-9a-f]{40}$/.test(r.active) ? r.active : "HEAD");
  }

  async function loadAuthors() {
    try {
      const d = await api(`/api/repos/${state.repoId}/authors`);
      state.authors = d.groups || [];
    } catch (e) { state.authors = []; }
    renderAuthorSelect();
  }

  function renderAuthorSelect() {
    const sel = $("#f-author");
    const g = state.filters.author_group;
    sel.innerHTML = `<option value="">All authors</option>` + state.authors.map((a) =>
      `<option value="${a.id}">${esc(a.display_name)} (${fmtInt(a.commits)} commits)</option>`).join("");
    sel.value = g == null ? "" : String(g);
  }

  // ------------------------------------------------------------------ filters
  function syncFilterControls() {
    renderRefSelect();
    renderAuthorSelect();
    const f = state.filters;
    const sel = $("#f-range");
    const hasRange = f.since != null || f.until != null;
    if (hasRange) {
      sel.value = "custom";
      $("#f-since").value = f.since != null ? fmtDay(f.since) : "";
      $("#f-until").value = f.until != null ? fmtDay(f.until - 86400) : "";
    } else if (sel.value !== "custom") {
      sel.value = "all";
    }
    $("#f-custom").hidden = sel.value !== "custom";
    renderFilterSummary();
  }

  function renderFilterSummary() {
    const f = state.filters;
    const chips = [];
    if (f.ref) chips.push(`<span class="chip">ref: ${esc(f.ref)} <span class="x" data-clear="ref">&times;</span></span>`);
    if (f.since != null || f.until != null) {
      const txt = `${f.since != null ? fmtDay(f.since) : "start"} &rarr; ${f.until != null ? fmtDay(f.until - 86400) : "now"}`;
      chips.push(`<span class="chip">${txt} <span class="x" data-clear="range">&times;</span></span>`);
    }
    if (f.author_group != null) {
      const a = state.authors.find((x) => x.id === f.author_group);
      chips.push(`<span class="chip">author: ${esc(a ? a.display_name : f.author_group)} <span class="x" data-clear="author">&times;</span></span>`);
    }
    if (f.commit_ids) {
      chips.push(`<span class="chip chip-cset">manual set: ${f.commit_ids.length} commits <span class="x" data-clear="cset">&times;</span></span>`);
    }
    $("#filter-summary").innerHTML = chips.join("");
    $$("#filter-summary .x").forEach((x) => x.addEventListener("click", () => {
      const k = x.dataset.clear;
      if (k === "ref") setFilters({ ref: null });
      if (k === "range") setFilters({ since: null, until: null });
      if (k === "author") setFilters({ author_group: null });
      if (k === "cset") setFilters({ commit_ids: null });
    }));
    $("#f-cset").textContent = f.commit_ids ? `${f.commit_ids.length} commits selected` : "all commits in filter";
  }

  function setFilters(patch) {
    Object.assign(state.filters, patch);
    syncFilterControls();
    writeHash();
    render();
    if (state.authorSelTimer) clearTimeout(state.authorSelTimer);
  }

  function presetRange(days) {
    if (!days) return { since: null, until: null };
    const now = Math.floor(Date.now() / 1000);
    return { since: now - days * 86400, until: null };
  }

  // ------------------------------------------------------------------ events: filters
  async function applyRef(ref) {
    // Validate + persist server-side first so a bad ref never corrupts the UI state.
    try {
      await api(`/api/repos/${state.repoId}/ref`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref }),
      });
    } catch (e) {
      toast("could not switch reference", "error", e.message);
      renderRefSelect();
      return;
    }
    $("#f-refcustom").hidden = true;
    setFilters({ ref });
    loadRepos(false);
  }
  $("#f-ref").addEventListener("change", async () => {
    const ref = $("#f-ref").value;
    if (ref === "__custom__") {
      $("#f-refcustom").hidden = false;
      const inp = $("#f-ref-input");
      inp.value = state.filters.ref || "";
      inp.focus();
      return;
    }
    await applyRef(ref);
  });
  $("#btn-ref-apply").addEventListener("click", () => {
    const ref = ($("#f-ref-input").value || "").trim();
    if (!ref) { $("#f-refcustom").hidden = true; renderRefSelect(); return; }
    applyRef(ref);
  });
  $("#f-ref-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#btn-ref-apply").click();
  });
  $("#f-range").addEventListener("change", () => {
    const v = $("#f-range").value;
    if (v === "all") setFilters({ since: null, until: null });
    else if (v === "custom") { $("#f-custom").hidden = false; }
    else setFilters(presetRange(Number(v)));
  });
  $("#f-since").addEventListener("change", () => {
    const v = $("#f-since").value;
    setFilters({ since: v ? Math.floor(Date.parse(v + "T00:00:00Z") / 1000) : null });
  });
  $("#f-until").addEventListener("change", () => {
    const v = $("#f-until").value;
    setFilters({ until: v ? Math.floor(Date.parse(v + "T00:00:00Z") / 1000) + 86400 : null });
  });
  $("#f-author").addEventListener("change", () => {
    const v = $("#f-author").value;
    setFilters({ author_group: v === "" ? null : Number(v) });
  });
  $("#btn-reset-filters").addEventListener("click", () => setFilters({ ref: null, since: null, until: null, author_group: null, commit_ids: null }));

  // ------------------------------------------------------------------ tabs
  function switchTab(tab) {
    state.tab = tab;
    if (tab === "commits") state.commits.page = 1;
    writeHash();
    render();
  }
  $$("#tabs .tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#ov-bucket").addEventListener("change", () => {
    state.bucket = $("#ov-bucket").value;
    writeHash();
    render();
  });

  // ------------------------------------------------------------------ navigation
  function navPath(path, isDir) {
    state.tab = "files"; state.path = path || ""; state.isDir = !!isDir;
    writeHash();
    render();
  }
  function showCommitsFor(path, isDir) {
    state.commits.scope = { path, is_dir: !!isDir };
    state.commits.page = 1;
    switchTab("commits");
  }

  // ------------------------------------------------------------------ render dispatcher
  // Busy / empty / error states render into a dedicated panel so the real view
  // sections (element ids, chart canvases, listeners) are never destroyed;
  // wiping them made every view fail with "ctx.$(...) is null" until a reload.
  function showViewStatus(html) {
    const el = $("#view-status");
    if (!el) return;
    el.innerHTML = html;
    el.hidden = false;
    $$(".view").forEach((v) => { v.hidden = true; });
  }
  function hideViewStatus() {
    const el = $("#view-status");
    if (!el || el.hidden) return;
    el.hidden = true;
    el.innerHTML = "";
  }

  async function render() {
    $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
    $$(".view").forEach((v) => { v.hidden = v.id !== "view-" + state.tab; });
    renderTopbar();
    renderRepoList();
    renderFilterSummary();

    if (!state.repo) {
      showViewStatus(`<div class="card"><div class="center-note">Add a repository (zip or clone URL) to start analysing.<br><br><button class="btn btn-primary" onclick="document.getElementById('btn-add-repo').click()">+ Add repository</button></div></div>`);
      return;
    }
    if (state.repo.status !== "ready") {
      const busy = ["cloning", "parsing", "pending"].includes(state.repo.status);
      if (busy) {
        showViewStatus(`<div class="card"><div class="center-note">${escapeSpinner(state.repo.message || state.repo.status)}
          <div class="progress" style="max-width:420px;margin:14px auto 0"><div class="progress-fill" style="width:${Math.round((state.repo.progress || 0) * 100)}%"></div></div></div></div>`);
      } else {
        showViewStatus(`<div class="card"><div class="center-note">This repository could not be analysed.<br><br>
          <span class="muted-small">${esc(state.repo.message || "unknown error")}</span><br><br>
          <button class="btn btn-primary" id="err-reanalyze">Re-analyze</button></div></div>`);
        const rb = $("#err-reanalyze");
        if (rb) rb.addEventListener("click", async () => {
          try {
            await api(`/api/repos/${state.repoId}/reanalyze`, { method: "POST" });
            toast("re-analyzing...");
            loadRepos(false);
          } catch (e) { toast("re-analyze failed", "error", e.message); }
        });
      }
      return;
    }
    hideViewStatus();
    if (!["overview", "files", "authors", "commits"].includes(state.tab)) state.tab = "overview";
    try {
      await RAT.views[state.tab](ctx);
    } catch (e) {
      toast("could not load this view", "error", e.message);
    }
    Object.values(charts).forEach((c) => { if (!c.isDisposed()) c.resize(); });
  }
  function escapeSpinner(msg) { return `<span class="spinner"></span>${esc(msg)}`; }

  // ------------------------------------------------------------------ author merge
  let mergeSelected = new Set();
  async function openMergeModal() {
    mergeSelected = new Set();
    $("#merge-search").value = "";
    $("#merge-name").value = ""; $("#merge-email").value = "";
    renderMergeList();
    openModal("modal-merge");
  }
  function renderMergeList() {
    const term = $("#merge-search").value.toLowerCase();
    const rows = [];
    state.authors.forEach((g) => {
      g.members.forEach((m) => {
        const txt = `${m.name} ${m.email}`.toLowerCase();
        if (term && !txt.includes(term)) return;
        rows.push(`<label class="merge-row">
          <input type="checkbox" data-raw="${m.raw_id}" ${mergeSelected.has(m.raw_id) ? "checked" : ""}>
          <span class="who"><b>${esc(m.name)}</b><span class="muted-small">${esc(m.email)} - ${fmtInt(m.commits)} commits, churn ${fmtInt(m.churn)} - group: ${esc(g.display_name)}</span></span>
        </label>`);
      });
    });
    $("#merge-list").innerHTML = rows.join("") || `<div class="empty-note">no identities match</div>`;
    $$("#merge-list input[type=checkbox]").forEach((cb) => cb.addEventListener("change", () => {
      const id = Number(cb.dataset.raw);
      if (cb.checked) mergeSelected.add(id); else mergeSelected.delete(id);
      $("#merge-count").textContent = `${mergeSelected.size} selected`;
      $("#merge-go").disabled = mergeSelected.size < 1;
    }));
    $("#merge-count").textContent = `${mergeSelected.size} selected`;
    $("#merge-go").disabled = mergeSelected.size < 1;
  }
  $("#merge-search").addEventListener("input", debounce(renderMergeList, 150));
  $("#btn-merge-authors").addEventListener("click", openMergeModal);
  $("#merge-go").addEventListener("click", async () => {
    try {
      const d = await api(`/api/repos/${state.repoId}/authors/merge`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          raw_ids: Array.from(mergeSelected),
          display_name: $("#merge-name").value.trim() || null,
          email: $("#merge-email").value.trim() || null,
        }),
      });
      closeModal("modal-merge");
      await loadAuthors();
      toast("identities merged", "success");
      render();
      void d;
    } catch (e) { toast("merge failed", "error", e.message); }
  });

  $("#btn-reapply-mailmap").addEventListener("click", async () => {
    try {
      const d = await api(`/api/repos/${state.repoId}/authors/reapply_mailmap`, { method: "POST" });
      await loadAuthors();
      toast(d.merged ? `${d.merged} identities merged from .mailmap` : "no new mailmap merges found", "success");
      render();
    } catch (e) { toast("mailmap re-apply failed", "error", e.message); }
  });

  window.RAT.openMergeModal = openMergeModal;

  async function unmerge(rawId, groupId) {
    if (!(await confirmBox("Split this identity out into its own author group?"))) return;
    try {
      const d = await api(`/api/repos/${state.repoId}/authors/unmerge`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rawId != null ? { raw_id: rawId } : { group_id: groupId }),
      });
      state.authors = d.groups || [];
      renderAuthorSelect();
      toast("identity detached", "success");
      render();
    } catch (e) { toast("unmerge failed", "error", e.message); }
  }
  window.RAT.unmerge = unmerge;

  // ------------------------------------------------------------------ commit picker
  const cset = { page: 1, per: 100, q: "", draft: new Set(), pageIds: [] };

  function openCsetModal() {
    cset.draft = new Set(state.filters.commit_ids || []);
    cset.page = 1; cset.q = "";
    $("#cs-search").value = "";
    openModal("modal-cset");
    renderCset();
  }
  $("#btn-edit-cset").addEventListener("click", openCsetModal);

  async function renderCset() {
    const f = state.filters;
    const q = new URLSearchParams();
    // list commits with search + paging; the period filter is deliberately ignored
    // here so any commit can be picked.
    q.set("page", cset.page); q.set("per", cset.per);
    if (cset.q) q.set("q", cset.q);
    if (f.ref) q.set("ref", f.ref);
    let data;
    try { data = await api(`/api/repos/${state.repoId}/commits?${q.toString()}`); }
    catch (e) { $("#cs-list").innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; return; }
    cset.pageIds = data.commits.map((c) => c.id);
    $("#cs-list").innerHTML = data.commits.map((c) => `<label class="cset-row-item">
        <input type="checkbox" data-id="${c.id}" ${cset.draft.has(c.id) ? "checked" : ""}>
        <span class="mono">${esc(c.sha.slice(0, 10))}</span>
        <span class="muted-small">${fmtDay(c.ts)}</span>
        <span class="muted-small">${esc(c.author)}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(c.subject)}">${esc(c.subject)}</span>
        <span class="add-pos">+${fmtInt(c.added)}</span><span class="add-neg">-${fmtInt(c.removed)}</span>
      </label>`).join("") || `<div class="empty-note">no commits match</div>`;
    $$("#cs-list input[type=checkbox]").forEach((cb) => cb.addEventListener("change", () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) cset.draft.add(id); else cset.draft.delete(id);
      $("#cs-count").textContent = `${cset.draft.size} selected`;
    }));
    const pages = Math.max(1, Math.ceil(data.total / cset.per));
    $("#cs-pager").innerHTML = `<span class="muted-small">${fmtInt(data.total)} commits - page ${cset.page} of ${pages}</span>
      <button class="btn btn-sm" id="cs-prev" ${cset.page <= 1 ? "disabled" : ""}>&larr;</button>
      <button class="btn btn-sm" id="cs-next" ${cset.page >= pages ? "disabled" : ""}>&rarr;</button>`;
    $("#cs-prev").onclick = () => { cset.page--; renderCset(); };
    $("#cs-next").onclick = () => { cset.page++; renderCset(); };
    $("#cs-page-all").checked = cset.pageIds.length > 0 && cset.pageIds.every((id) => cset.draft.has(id));
    $("#cs-count").textContent = `${cset.draft.size} selected`;
  }
  $("#cs-search").addEventListener("input", debounce(() => { cset.q = $("#cs-search").value.trim(); cset.page = 1; renderCset(); }, 200));
  $("#cs-page-all").addEventListener("change", () => {
    const on = $("#cs-page-all").checked;
    cset.pageIds.forEach((id) => { if (on) cset.draft.add(id); else cset.draft.delete(id); });
    renderCset();
  });
  $("#cs-clear").addEventListener("click", () => { cset.draft = new Set(); renderCset(); });
  $("#cs-apply").addEventListener("click", () => {
    closeModal("modal-cset");
    setFilters({ commit_ids: cset.draft.size ? Array.from(cset.draft).sort((a, b) => a - b) : null });
    toast(cset.draft.size ? `commit set: ${cset.draft.size} commits` : "commit set cleared");
  });

  // ------------------------------------------------------------------ commit drawer
  async function openCommitDrawer(id) {
    const d = $("#drawer"), bd = $("#drawer-backdrop");
    d.hidden = false; bd.hidden = false;
    $("#drawer-body").innerHTML = `<div class="center-note"><span class="spinner" style="border-color:rgba(0,0,0,.15);border-top-color:var(--accent)"></span></div>`;
    try {
      const c = await api(`/api/commits/${id}`);
      const files = c.files.map((f) => `<tr><td class="mono">${esc(f.path)}</td>
        <td class="num add-pos">+${fmtInt(f.added)}</td><td class="num add-neg">-${fmtInt(f.removed)}</td></tr>`).join("");
      $("#drawer-body").innerHTML = `
        <dl class="kv">
          <dt>sha</dt><dd><span class="mono">${esc(c.sha)}</span> <button class="btn btn-sm" id="copy-sha">copy</button></dd>
          <dt>subject</dt><dd>${esc(c.subject)}</dd>
          <dt>author</dt><dd>${esc(c.author)} <span class="muted-small">${esc(c.email || "")}</span></dd>
          <dt>committed</dt><dd>${esc(new Date(c.ts * 1000).toISOString().replace("T", " ").slice(0, 16))} UTC</dd>
          <dt>lines</dt><dd><span class="add-pos">+${fmtInt(c.added)}</span> / <span class="add-neg">-${fmtInt(c.removed)}</span></dd>
        </dl>
        <div class="table-wrap"><table class="data"><thead><tr><th>file</th><th class="num">l&#8314;</th><th class="num">l&#8315;</th></tr></thead>
        <tbody>${files || `<tr><td colspan="3" class="muted-small">no measured file changes (binary or empty commit)</td></tr>`}</tbody></table></div>
        <div class="modal-actions"><button class="btn btn-sm" id="drawer-cset">Use this commit as commit set</button></div>`;
      $("#copy-sha").onclick = async () => {
        try { await navigator.clipboard.writeText(c.sha); toast("sha copied"); } catch (e) { toast("copy failed", "error", e.message); }
      };
      $("#drawer-cset").onclick = () => { closeDrawer(); setFilters({ commit_ids: [c.id] }); };
    } catch (e) {
      $("#drawer-body").innerHTML = `<div class="empty-note">${esc(e.message)}</div>`;
    }
  }
  function closeDrawer() { $("#drawer").hidden = true; $("#drawer-backdrop").hidden = true; }
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);

  // ------------------------------------------------------------------ add repo modal
  function setupAddModal() {
    $("#btn-add-repo").addEventListener("click", () => {
      $("#add-status").hidden = true;
      openModal("modal-add");
    });
    $$(".mtab").forEach((t) => t.addEventListener("click", () => {
      $$(".mtab").forEach((x) => x.classList.toggle("active", x === t));
      $("#add-pane-url").hidden = t.dataset.addtab !== "url";
      $("#add-pane-zip").hidden = t.dataset.addtab !== "zip";
    }));

    $("#add-url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#add-url-go").click(); });
    $("#add-url-go").addEventListener("click", async () => {
      const url = $("#add-url").value.trim();
      if (!url) { toast("enter a clone URL", "error"); return; }
      const btn = $("#add-url-go");
      btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>cloning...`;
      try {
        const repo = await api("/api/repos/clone", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
        });
        await watchIngest(repo.id);
      } catch (e) {
        toast("could not start the clone", "error", e.message);
      } finally {
        btn.disabled = false; btn.textContent = "Clone & analyze";
      }
    });

    const drop = $("#add-drop");
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
    drop.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) { $("#add-file").files = e.dataTransfer.files; onFilePicked(); }
    });
    $("#add-file").addEventListener("change", onFilePicked);
    function onFilePicked() {
      const f = $("#add-file").files[0];
      $("#add-file-name").textContent = f ? `${f.name} (${(f.size / 1048576).toFixed(1)} MB)` : "";
      $("#add-zip-go").disabled = !f;
    }

    $("#add-zip-go").addEventListener("click", () => {
      const f = $("#add-file").files[0];
      if (!f) return;
      const btn = $("#add-zip-go");
      btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>uploading...`;
      $("#add-upload-bar").hidden = false;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/repos/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) $("#add-upload-fill").style.width = Math.round((e.loaded / e.total) * 100) + "%";
      };
      xhr.onload = async () => {
        btn.disabled = false; btn.textContent = "Upload & analyze";
        $("#add-upload-bar").hidden = true;
        $("#add-upload-fill").style.width = "0";
        if (xhr.status >= 200 && xhr.status < 300) {
          const repo = JSON.parse(xhr.responseText);
          await watchIngest(repo.id);
        } else {
          let msg = "upload failed";
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (e) { /* ignore */ }
          if (xhr.status === 400 && /empty/i.test(msg)) msg = "the file was empty";
          toast("could not upload the zip", "error", msg);
        }
      };
      xhr.onerror = () => {
        btn.disabled = false; btn.textContent = "Upload & analyze";
        toast("upload failed", "error", "network error");
      };
      const fd = new FormData();
      fd.append("file", f, f.name);
      xhr.send(fd);
    });
  }

  async function watchIngest(repoId) {
    $("#add-status").hidden = false;
    $("#add-phase").textContent = "queued...";
    let done = false;
    while (!done) {
      await new Promise((r) => setTimeout(r, 900));
      let repo;
      try { repo = (await api("/api/repos")).repos.find((r) => r.id === repoId); }
      catch (e) { continue; }
      if (!repo) continue;
      $("#add-phase").textContent = `${repo.phase}: ${repo.message || repo.status}`;
      $("#add-progress").style.width = Math.round((repo.progress || 0) * 100) + "%";
      if (repo.status === "ready") {
        done = true;
        toast(`"${repo.name}" analysed`, "success", repo.message);
        closeModal("modal-add");
        state.repoId = null;
        await loadRepos(false);
        await selectRepo(repoId, false);
        state.repoId = repoId;
      } else if (repo.status === "error") {
        done = true;
        toast(`ingest failed`, "error", repo.message);
      }
    }
  }

  // ------------------------------------------------------------------ search & per
  $("#cm-search").addEventListener("input", debounce(() => {
    state.commits.q = $("#cm-search").value.trim();
    state.commits.page = 1;
    writeHash();
    render();
  }, 300));
  $("#cm-per").addEventListener("change", () => {
    state.commits.per = Number($("#cm-per").value);
    state.commits.page = 1;
    render();
  });
  $("#cm-scope").addEventListener("click", (e) => {
    if (e.target.classList.contains("x")) { state.commits.scope = null; writeHash(); render(); }
  });
  $("#cm-use-cset").addEventListener("click", () => {
    const ids = Array.from(state.commits.sel).sort((a, b) => a - b);
    setFilters({ commit_ids: ids });
    toast(`commit set: ${ids.length} commits`);
  });
  $("#files-top-sort").addEventListener("change", () => render());
  $("#ov-top-sort").addEventListener("change", () => render());

  // ------------------------------------------------------------------ ctx
  const ctx = {
    state, api, qs, esc, $, $$, fmtInt, fmtF, fmtPct, fmtDay, debounce,
    chart, disposeChart,
    navPath, showCommitsFor, switchTab, render, toast, confirmBox,
    openCommitDrawer, openCsetModal, openMergeModal, unmerge,
    setFilters, writeHash,
  };
  window.RAT.ctx = ctx;

  // ------------------------------------------------------------------ init
  async function init() {
    setupAddModal();
    await loadHealth();
    if (applyHash()) { /* selectRepo inside applyHash triggers render */ }
    await loadRepos(true);
  }
  init();
})();
