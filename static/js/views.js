/* RAT dashboard - view renderers (overview, files, authors, commits). */
"use strict";

window.RAT = window.RAT || {};
window.RAT.views = window.RAT.views || {};

(function () {
  const V = window.RAT.views;

  const DEF = {
    added: "l\u207A - lines added to non-binary files",
    removed: "l\u207B - lines removed from non-binary files",
    growth: "\u03B4 = l\u207A \u2212 l\u207B - net growth",
    churn: "\u03BB = l\u207A + l\u207B - churn",
    modifications: "n - commits in H that modified the object",
    commits: "|H| - size of the current commit set",
    eta: "\u03B7 = n / |H| - modification frequency",
    rho: "\u03C1 = \u03BB / |H| - churn rate",
    ownership: "\u03C9 = \u03BB\u2090 / \u03BB - author share of the object's churn",
    contributors: "distinct authors in the current commit set",
    files: "files known to the tree (including files deleted at later commits)",
    dirs: "directories known to the tree",
  };

  const ORIGIN_BADGE = {
    raw: `<span class="badge raw" title="identity as found in the log">raw</span>`,
    mailmap: `<span class="badge mailmap" title="merged automatically from .mailmap">mailmap</span>`,
    manual: `<span class="badge manual" title="merged manually in this dashboard">manual</span>`,
  };

  function help(key) {
    return `<span class="help" title="${DEF[key] || ""}">?</span>`;
  }

  function card(label, value, sub, helpKey, cls) {
    return `<div class="metric ${cls || ""}">
      <div class="m-label">${label} ${helpKey ? help(helpKey) : ""}</div>
      <div class="m-value">${value}</div>
      ${sub ? `<div class="m-sub">${sub}</div>` : ""}
    </div>`;
  }

  function metricCards(m, extra) {
    const cards = [
      card("commits |H|", window.RAT.ctx.fmtInt(m.commits), null, "commits"),
      card("added", window.RAT.ctx.fmtInt(m.added), null, "added"),
      card("removed", window.RAT.ctx.fmtInt(m.removed), null, "removed"),
      card("net growth", (m.growth > 0 ? "+" : "") + window.RAT.ctx.fmtInt(m.growth), "\u03B4", "growth", m.growth >= 0 ? "good" : "bad"),
      card("churn", window.RAT.ctx.fmtInt(m.churn), "\u03BB", "churn"),
      card("modifications", window.RAT.ctx.fmtInt(m.modifications), "n", "modifications"),
      card("mod frequency", window.RAT.ctx.fmtF(m.mod_frequency, 3), "\u03B7", "eta"),
      card("churn rate", window.RAT.ctx.fmtF(m.churn_rate, 2), "\u03C1", "rho"),
    ];
    if (extra) cards.push(...extra);
    return cards;
  }

  function barOption(cats, values, color, horizontal) {
    const base = {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      grid: horizontal ? { left: 130, right: 34, top: 8, bottom: 22 } : { left: 46, right: 12, top: 14, bottom: 40 },
      xAxis: horizontal
        ? { type: "value", axisLabel: { formatter: (v) => fmtShort(v) } }
        : { type: "category", data: cats, axisLabel: { rotate: cats.length > 8 ? 35 : 0, formatter: (v) => fmtShort2(v) } },
      yAxis: horizontal
        ? { type: "category", data: cats, inverse: true, axisLabel: { formatter: (v) => fmtShort2(v) } }
        : { type: "value", axisLabel: { formatter: (v) => fmtShort(v) } },
      series: [{
        type: "bar", data: values, itemStyle: { color, borderRadius: horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0] },
        barMaxWidth: 22,
        label: horizontal ? { show: true, position: "right", fontSize: 10, color: "#5a6478", formatter: (p) => fmtShort(p.value) } : { show: false },
      }],
    };
    return base;
  }

  function fmtShort(v) {
    v = Number(v);
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return String(v);
  }
  function fmtShort2(v) {
    const s = String(v);
    return s.length > 24 ? s.slice(0, 22) + "\u2026" : s;
  }

  // ---------------------------------------------------------------- overview
  V.overview = async function (ctx) {
    const { state, api, qs, esc, fmtInt, fmtF } = ctx;
    const d = await api(`/api/repos/${state.repoId}/overview${qs({ bucket: state.bucket })}`);
    const m = d.metrics;

    const repoMeta = [
      card("files", fmtInt(d.files), null, "files"),
      card("directories", fmtInt(d.dirs), null, "dirs"),
      card("contributors", fmtInt(d.contributors), null, "contributors"),
    ];
    if (m.ownership != null) {
      repoMeta.push(card("author ownership", (100 * m.ownership).toFixed(1) + "%",
        `of \u03BB=${fmtInt(m.context_churn)}`, "ownership", "good"));
    }
    ctx.$("#ov-cards").innerHTML = metricCards(m).concat(repoMeta).join("");

    // timeline
    const labels = d.timeline.map((b) => b.bucket);
    const tl = ctx.chart("#ov-timeline");
    tl.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: ["added", "removed", "modifications"], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 52, right: 46, top: 30, bottom: 26 },
      xAxis: { type: "category", data: labels },
      yAxis: [
        { type: "value", axisLabel: { formatter: fmtShort } },
        { type: "value", axisLabel: { formatter: fmtShort }, splitLine: { show: false } },
      ],
      series: [
        { name: "added", type: "bar", stack: "l", itemStyle: { color: "#34a853" }, data: d.timeline.map((b) => b.added) },
        { name: "removed", type: "bar", stack: "l", itemStyle: { color: "#e35d6a" }, data: d.timeline.map((b) => -b.removed) },
        { name: "modifications", type: "line", yAxisIndex: 1, symbolSize: 5, itemStyle: { color: "#4657d4" }, data: d.timeline.map((b) => b.modifications) },
      ],
    }, true);

    // churn by top-level object
    const kids = d.children.slice().sort((a, b) => b.churn - a.churn).slice(0, 14);
    const bars = ctx.chart("#ov-bars");
    bars.setOption(barOption(kids.map((k) => k.name + (k.is_dir ? "/" : "")), kids.map((k) => k.churn), "#7286ff", true), true);
    bars.off("click");
    bars.on("click", (p) => {
      const k = kids[p.dataIndex];
      if (k) ctx.navPath(k.path, k.is_dir);
    });

    // ownership doughnut
    const authors = (d.authors.authors || []).slice().sort((a, b) => b.churn - a.churn);
    const top = authors.slice(0, 8);
    const restChurn = authors.slice(8).reduce((s, a) => s + a.churn, 0);
    const pieData = top.map((a) => ({ name: a.name, value: a.churn }));
    if (restChurn > 0) pieData.push({ name: "other", value: restChurn });
    const owners = ctx.chart("#ov-owners");
    owners.setOption({
      tooltip: { trigger: "item", formatter: (p) => `${esc(p.name)}<br/>churn ${fmtInt(p.value)} (${p.percent}%)` },
      legend: { type: "scroll", orient: "vertical", right: 0, top: "middle", textStyle: { fontSize: 11 }, formatter: fmtShort2 },
      series: [{
        type: "pie", radius: ["42%", "72%"], center: ["36%", "50%"],
        itemStyle: { borderColor: "#fff", borderWidth: 2 },
        label: { show: false },
        data: pieData,
      }],
    }, true);

    // top files
    const topFiles = (await api(`/api/repos/${state.repoId}/top_files${qs({ sort: ctx.$("#ov-top-sort").value, path: "", is_dir: 1, limit: 25 })}`)).files;
    ctx.$("#ov-top").innerHTML = fileTableHTML(topFiles, true);
    ctx.$$("#ov-top tr[data-path]").forEach((tr) => tr.addEventListener("click", () =>
      navPath(tr.dataset.path, false)));

    // contributors
    ctx.$("#ov-contributors").innerHTML = `<table class="data">
      <thead><tr><th>author</th><th>origin</th><th class="num">commits</th><th class="num">added</th>
      <th class="num">removed</th><th class="num">churn</th><th>ownership \u03C9</th></tr></thead>
      <tbody>${authors.map((a) => `<tr class="clickable" data-author="${a.group_id}" title="filter by this author">
        <td>${esc(a.name)} <span class="muted-small">${esc(a.email)}</span></td>
        <td>${ORIGIN_BADGE[a.origin] || a.origin}</td>
        <td class="num">${fmtInt(a.commits)}</td>
        <td class="num">${fmtInt(a.added)}</td><td class="num">${fmtInt(a.removed)}</td>
        <td class="num">${fmtInt(a.churn)}</td>
        <td><span class="owner-bar" style="width:${Math.max(2, Math.round(90 * a.ownership))}px"></span>${(100 * a.ownership).toFixed(1)}%</td>
      </tr>`).join("") || `<tr><td colspan="7" class="empty-note">no commits in this commit set</td></tr>`}</tbody></table>`;
    ctx.$$("#ov-contributors tr[data-author]").forEach((tr) => tr.addEventListener("click", () =>
      ctx.setFilters({ author_group: Number(tr.dataset.author) })));
  };

  function fileTableHTML(files, clickable) {
    const { fmtInt, fmtF } = window.RAT.ctx;
    if (!files.length) return `<div class="empty-note">nothing to show for this selection</div>`;
    return `<table class="data"><thead><tr><th>file</th><th class="num">l\u207A</th><th class="num">l\u207B</th>
      <th class="num">\u03B4</th><th class="num">\u03BB</th><th class="num">n</th><th class="num">\u03B7</th><th class="num">\u03C1</th></tr></thead>
      <tbody>${files.map((f) => `<tr class="${clickable ? "clickable" : ""}" data-path="${window.RAT.ctx.esc(f.path)}">
        <td class="mono">${window.RAT.ctx.esc(f.path)}</td>
        <td class="num">${fmtInt(f.added)}</td><td class="num">${fmtInt(f.removed)}</td>
        <td class="num ${f.growth >= 0 ? "add-pos" : "add-neg"}">${f.growth > 0 ? "+" : ""}${fmtInt(f.growth)}</td>
        <td class="num">${fmtInt(f.churn)}</td><td class="num">${fmtInt(f.modifications)}</td>
        <td class="num">${fmtF(f.mod_frequency, 3)}</td><td class="num">${fmtF(f.churn_rate, 2)}</td></tr>`).join("")}</tbody></table>`;
  }

  // ---------------------------------------------------------------- files
  V.files = async function (ctx) {
    const { state, api, qs, esc, fmtInt, fmtF, navPath } = ctx;
    const path = state.path || "";
    const isDir = state.isDir;

    const m = await api(`/api/repos/${state.repoId}/metrics${qs({ path, is_dir: isDir ? 1 : 0 })}`);

    // breadcrumb
    const parts = path ? path.split("/") : [];
    let accum = "";
    const crumbs = [`<span class="cr ${isDir && !path ? "current" : ""}" data-nav="" data-dir="1" title="repository root">root</span>`];
    parts.forEach((p, i) => {
      accum = accum ? accum + "/" + p : p;
      const isLast = i === parts.length - 1;
      crumbs.push(`<span class="sep">/</span><span class="cr ${isLast ? "current" : ""}" data-nav="${esc(accum)}" data-dir="${isLast ? (isDir ? 1 : 0) : 1}">${esc(p)}${isLast && isDir ? "/" : ""}</span>`);
    });
    ctx.$("#files-breadcrumb").innerHTML = crumbs.join("");
    ctx.$$("#files-breadcrumb .cr").forEach((c) => c.addEventListener("click", () => {
      if (c.classList.contains("current")) return;
      navPath(c.dataset.nav, c.dataset.dir === "1");
    }));

    // summary cards
    const extra = [];
    if (m.ownership != null) extra.push(card("ownership", (100 * m.ownership).toFixed(1) + "%", `of \u03BB=${fmtInt(m.context_churn)}`, "ownership", "good"));
    ctx.$("#files-summary").innerHTML = metricCards(m, extra).join("");

    ctx.$("#files-actions").innerHTML = `<button class="btn btn-sm" id="files-show-commits">Commits touching this ${isDir ? "directory" : "file"}</button>`;
    ctx.$("#files-show-commits").addEventListener("click", () => ctx.showCommitsFor(path, isDir));

    if (isDir) {
      ctx.$("#files-chart-card").hidden = false;
      ctx.$("#files-owners-card").hidden = true;
      ctx.$("#files-list-title").textContent = path ? `Contents of ${path}/` : "Repository root";
      ctx.$("#files-chart-title").textContent = path ? `Churn by child of ${path}/` : "Churn by top-level object";

      const kids = (await api(`/api/repos/${state.repoId}/children${qs({ path })}`)).children;
      // chart
      const top = kids.slice().sort((a, b) => b.churn - a.churn).slice(0, 12);
      const ch = ctx.chart("#files-chart");
      ch.setOption(barOption(top.map((k) => k.name + (k.is_dir ? "/" : "")), top.map((k) => k.churn), "#7286ff", true), true);
      ch.off("click");
      ch.on("click", (p) => { const k = top[p.dataIndex]; if (k) navPath(k.path, k.is_dir); });

      // sortable children table
      const s = state.filesSort;
      const rows = kids.slice().sort((a, b) => {
        if (s.key === "name") return s.dir * a.name.localeCompare(b.name);
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
        return s.dir * ((a[s.key] ?? 0) - (b[s.key] ?? 0));
      });
      const th = (key, label, num, def) => `<th class="${num ? "num " : ""}sortable" data-sort="${key}" title="${def ? DEF[def] : ""}">${label}${s.key === key ? ` <span class="arrow">${s.dir > 0 ? "\u25B4" : "\u25BE"}</span>` : ""}</th>`;
      ctx.$("#files-table").innerHTML = `<table class="data"><thead><tr>
        ${th("name", "name", false)} ${th("added", "l\u207A", true, "added")} ${th("removed", "l\u207B", true, "removed")}
        ${th("growth", "\u03B4", true, "growth")} ${th("churn", "\u03BB", true, "churn")} ${th("modifications", "n", true, "modifications")}
        ${th("mod_frequency", "\u03B7", true, "eta")} ${th("churn_rate", "\u03C1", true, "rho")}</tr></thead>
        <tbody>${rows.map((k) => `<tr class="clickable" data-path="${esc(k.path)}" data-dir="${k.is_dir ? 1 : 0}">
          <td><span class="file-kind">${k.is_dir ? "&#128193;" : "&#128196;"}</span><span class="mono">${esc(k.name)}</span>${k.is_dir ? "/" : ""}</td>
          <td class="num">${fmtInt(k.added)}</td><td class="num">${fmtInt(k.removed)}</td>
          <td class="num ${k.growth >= 0 ? "add-pos" : "add-neg"}">${k.growth > 0 ? "+" : ""}${fmtInt(k.growth)}</td>
          <td class="num">${fmtInt(k.churn)}</td><td class="num">${fmtInt(k.modifications)}</td>
          <td class="num">${fmtF(k.mod_frequency, 3)}</td><td class="num">${fmtF(k.churn_rate, 2)}</td></tr>`).join("")
        || `<tr><td colspan="8" class="empty-note">this directory has no children in the analysed history</td></tr>`}</tbody></table>`;
      ctx.$$("#files-table th.sortable").forEach((h) => h.addEventListener("click", () => {
        const key = h.dataset.sort;
        if (s.key === key) s.dir = -s.dir;
        else { s.key = key; s.dir = key === "name" ? 1 : -1; }
        ctx.render();
      }));
      ctx.$$("#files-table tr[data-path]").forEach((tr) => tr.addEventListener("click", () =>
        navPath(tr.dataset.path, tr.dataset.dir === "1")));
    } else {
      ctx.$("#files-chart-card").hidden = true;
      ctx.$("#files-chart-title").textContent = "";
      ctx.$("#files-owners-card").hidden = false;
      ctx.$("#files-list-title").textContent = "File";
      ctx.$("#files-table").innerHTML = `<table class="data"><thead><tr><th>file</th><th class="num">l\u207A</th>
        <th class="num">l\u207B</th><th class="num">\u03B4</th><th class="num">\u03BB</th><th class="num">n</th></tr></thead>
        <tbody><tr><td class="mono">${esc(path)}</td><td class="num">${fmtInt(m.added)}</td>
        <td class="num">${fmtInt(m.removed)}</td><td class="num ${m.growth >= 0 ? "add-pos" : "add-neg"}">${m.growth > 0 ? "+" : ""}${fmtInt(m.growth)}</td>
        <td class="num">${fmtInt(m.churn)}</td><td class="num">${fmtInt(m.modifications)}</td></tr></tbody></table>`;

      // ownership of this file
      const bd = await api(`/api/repos/${state.repoId}/authors_breakdown${qs({ path, is_dir: 0, limit: 12 })}`);
      const top = bd.authors.filter((a) => a.churn > 0).slice(0, 10);
      const oc = ctx.chart("#files-owners");
      oc.setOption(barOption(
        top.map((a) => a.name),
        top.map((a) => a.churn),
        "#4657d4", true), true);
    }

    // top files in subtree
    if (isDir) {
      const tf = (await api(`/api/repos/${state.repoId}/top_files${qs({ path, is_dir: 1, sort: ctx.$("#files-top-sort").value, limit: 25 })}`)).files;
      ctx.$("#files-top-table").innerHTML = fileTableHTML(tf, true);
      ctx.$$("#files-top-table tr[data-path]").forEach((tr) => tr.addEventListener("click", () =>
        navPath(tr.dataset.path, false)));
    } else {
      ctx.$("#files-top-table").innerHTML = `<div class="empty-note">select a directory to rank its files</div>`;
    }
  };

  // ---------------------------------------------------------------- authors
  V.authors = async function (ctx) {
    const { state, api, qs, esc, fmtInt, fmtF } = ctx;
    const [groups, bd] = await Promise.all([
      api(`/api/repos/${state.repoId}/authors`).then((d) => d.groups),
      api(`/api/repos/${state.repoId}/authors_breakdown${qs({ path: "", is_dir: 1, limit: 500 })}`),
    ]);
    const metricsByGroup = {};
    bd.authors.forEach((a) => { metricsByGroup[a.group_id] = a; });
    const rows = groups.map((g) => ({ g, m: metricsByGroup[g.id] || null }));
    rows.sort((a, b) => ((b.m ? b.m.churn : 0) - (a.m ? a.m.churn : 0)));

    ctx.$("#authors-note").textContent = `${groups.length} canonical authors - ownership is measured against the current commit set` +
      (state.filters.author_group != null ? " (context: author filter removed, per the \u03C9 definition)" : "");

    ctx.$("#authors-table").innerHTML = `<table class="data"><thead><tr>
      <th>author</th><th>origin</th><th class="num">identities</th><th class="num">commits</th>
      <th class="num">l\u207A</th><th class="num">l\u207B</th><th class="num">\u03BB</th><th class="num">n</th>
      <th>ownership \u03C9</th><th></th></tr></thead>
      <tbody>${rows.map(({ g, m }) => {
        const own = m ? m.ownership : 0;
        return `<tr class="clickable" data-group="${g.id}">
          <td>${esc(g.display_name)} <span class="muted-small">${esc(g.email)}</span></td>
          <td>${ORIGIN_BADGE[g.origin] || esc(g.origin)}</td>
          <td class="num">${g.members.length}</td>
          <td class="num">${fmtInt(g.commits)}</td>
          <td class="num">${fmtInt(m ? m.added : 0)}</td>
          <td class="num">${fmtInt(m ? m.removed : 0)}</td>
          <td class="num">${fmtInt(m ? m.churn : 0)}</td>
          <td class="num">${fmtInt(m ? m.modifications : 0)}</td>
          <td><span class="owner-bar" style="width:${Math.max(2, Math.round(90 * own))}px"></span>${(100 * own).toFixed(1)}%</td>
          <td>${g.members.length > 1 || g.origin !== "raw" ? `<button class="btn btn-sm btn-ghost" data-split="${g.id}" title="Split this merged author into separate identities">split</button>` : ""}</td>
        </tr>
        <tr class="detail-row" data-detail="${g.id}" hidden><td colspan="10">
          <table class="data"><tbody>${g.members.map((mb) => `<tr>
            <td class="mono">${esc(mb.name)}</td><td class="muted-small">${esc(mb.email)}</td>
            <td class="num">${fmtInt(mb.commits)} commits</td><td class="num">churn ${fmtInt(mb.churn)}</td>
            <td>${g.members.length > 1 ? `<button class="btn btn-sm btn-ghost" data-detach="${mb.raw_id}" title="Detach this identity into its own author">detach</button>` : ""}</td>
          </tr>`).join("")}</tbody></table></td></tr>`;
      }).join("") || `<tr><td colspan="10" class="empty-note">no authors</td></tr>`}</tbody></table>`;

    ctx.$$("#authors-table tr[data-group]").forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      const dr = ctx.$(`#authors-table tr[data-detail="${tr.dataset.group}"]`);
      if (dr) dr.hidden = !dr.hidden;
    }));
    ctx.$$("#authors-table [data-split]").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      ctx.unmerge(null, Number(b.dataset.split));
    }));
    ctx.$$("#authors-table [data-detach]").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      ctx.unmerge(Number(b.dataset.detach), null);
    }));
  };

  // ---------------------------------------------------------------- commits
  V.commits = async function (ctx) {
    const { state, api, qs, esc, fmtInt } = ctx;
    const cs = state.commits;
    const scope = cs.scope;

    ctx.$("#cm-scope").hidden = !scope;
    if (scope) {
      ctx.$("#cm-scope").innerHTML = `touching ${esc(scope.path || "root")}${scope.is_dir ? "/" : ""} <span class="x">&times;</span>`;
    }
    ctx.$("#cm-search").value = cs.q;
    ctx.$("#cm-per").value = String(cs.per);

    const params = qs({
      page: cs.page, per: cs.per, q: cs.q || undefined,
      path: scope ? scope.path : undefined,
      is_dir: scope ? (scope.is_dir ? 1 : 0) : undefined,
    });
    const data = await api(`/api/repos/${state.repoId}/commits${params}`);
    const pages = Math.max(1, Math.ceil(data.total / cs.per));
    if (cs.page > pages) { cs.page = pages; writeAndRender(ctx); return; }

    ctx.$("#commits-table").innerHTML = `<table class="data"><thead><tr>
      <th style="width:30px"><input type="checkbox" id="cm-page-all"></th>
      <th>sha</th><th>date</th><th>author</th><th>subject</th>
      <th class="num">l\u207A</th><th class="num">l\u207B</th></tr></thead>
      <tbody>${data.commits.map((c) => `<tr class="clickable" data-commit="${c.id}">
        <td><input type="checkbox" data-sel="${c.id}" ${cs.sel.has(c.id) ? "checked" : ""}></td>
        <td class="mono">${esc(c.sha.slice(0, 10))}</td>
        <td>${ctx.fmtDay(c.ts)}</td>
        <td>${esc(c.author)}</td>
        <td class="subj" title="${esc(c.subject)}">${esc(c.subject)}</td>
        <td class="num add-pos">+${fmtInt(c.added)}</td>
        <td class="num add-neg">-${fmtInt(c.removed)}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty-note">no commits match the current filters</td></tr>`}</tbody></table>`;

    ctx.$("#cm-pager").innerHTML = `<span>${fmtInt(data.total)} commits - page ${data.page} of ${pages}</span>
      <button class="btn btn-sm" id="cm-prev" ${data.page <= 1 ? "disabled" : ""}>&larr; newer</button>
      <button class="btn btn-sm" id="cm-next" ${data.page >= pages ? "disabled" : ""}>older &rarr;</button>`;
    ctx.$("#cm-prev").onclick = () => { cs.page--; writeAndRender(ctx); };
    ctx.$("#cm-next").onclick = () => { cs.page++; writeAndRender(ctx); };

    const pageIds = data.commits.map((c) => c.id);
    const pageAll = ctx.$("#cm-page-all");
    if (pageAll) {
      pageAll.checked = pageIds.length > 0 && pageIds.every((id) => cs.sel.has(id));
      pageAll.addEventListener("change", () => {
        pageIds.forEach((id) => { if (pageAll.checked) cs.sel.add(id); else cs.sel.delete(id); });
        updateSelUI(ctx);
        V.commits(ctx);
      });
    }
    ctx.$$("#commits-table [data-sel]").forEach((cb) => cb.addEventListener("change", (e) => {
      e.stopPropagation();
      const id = Number(cb.dataset.sel);
      if (cb.checked) cs.sel.add(id); else cs.sel.delete(id);
      updateSelUI(ctx);
    }));
    ctx.$$("#commits-table tr[data-commit]").forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.closest("input")) return;
      ctx.openCommitDrawer(Number(tr.dataset.commit));
    }));
    updateSelUI(ctx);
  };

  function updateSelUI(ctx) {
    const cs = ctx.state.commits;
    ctx.$("#cm-sel-info").textContent = cs.sel.size ? `${cs.sel.size} selected` : "";
    ctx.$("#cm-use-cset").hidden = cs.sel.size === 0;
  }

  function writeAndRender(ctx) {
    ctx.writeHash();
    ctx.render();
  }
})();
