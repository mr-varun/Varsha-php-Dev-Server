// varsha control panel: the Data page (SQLite files). Loaded after app.js and uses its helpers
// ($, esc, api, toast, dialog, confirmDialog, fmtBytes, fmtDate, plural). The page manages itself:
// app.js only builds the skeleton once, so live state refreshes never reset a grid or the SQL box.
"use strict";

const DATA = {
  list: { recent: [], found: [] },
  file: null,       // overview of the open file
  table: "",
  tab: "rows",      // rows | structure | sql
  page: null,       // last /rows answer
  offset: 0, order: "", desc: false, q: "", column: "",
  selected: new Set(),
  structure: null,
  sql: "",
  result: null,
};
const DATA_HISTORY_KEY = "varsha.data.sqlHistory";

function viewDataSkeleton() {
  return `<div id="data" class="data-page"><div id="data-files"></div><div id="data-main"></div></div>`;
}

async function renderData() {
  await loadDataList();
  if (DATA.file) await openDataFile(DATA.file.id, { keepTable: true });
  else renderDataMain();
}

async function loadDataList() {
  try { DATA.list = await api.get("/api/data"); } catch (err) { toast({ ok: false, title: "Data Browser", message: err.message }); }
  renderDataFiles();
}

// ---------------------------------------------------------------- the file list

function renderDataFiles() {
  const box = $("#data-files");
  if (!box) return;
  const { recent, found } = DATA.list;
  const item = (f, extra) => `<span class="data-file${DATA.file && DATA.file.id === f.id ? " is-open" : ""}">
      <button class="data-file-open" data-d="open" data-id="${esc(f.id)}" title="${esc(f.path)}" ${f.exists === false ? "disabled" : ""}>
        <b>${esc(f.name)}</b><small>${esc(extra)}</small></button>
      ${f.site ? "" : `<button class="data-file-x" data-d="forget" data-id="${esc(f.id)}" title="Remove from the list (the file stays)" aria-label="Remove ${esc(f.name)} from the list">\u00d7</button>`}</span>`;
  box.innerHTML = `<div class="toolbar">
      <button class="btn btn-yellow" data-d="pick">\u{1f4c2} Open file\u2026</button>
      <p class="muted">Open a SQLite (<code>.db</code>, <code>.sqlite</code>, \u2026), Access (<code>.mdb</code>, <code>.accdb</code>)
        or CSV / TSV file. It opens <b>read-only</b>; SQLite and CSV files can be edited after switching on Edit (a backup is saved
        first, and a CSV keeps its own format). Access files are read from a copy, so the original is never touched.</p></div>
    ${recent.length ? `<div class="data-files"><span class="data-files-label">Recent</span>${recent.map((f) =>
      item(f, f.exists === false ? "missing" : `${fmtBytes(f.size)} \u00b7 ${f.path.replace(/[\\/][^\\/]*$/, "")}`)).join("")}</div>` : ""}
    ${found.length ? `<div class="data-files"><span class="data-files-label">In your sites</span>${found.map((f) =>
      item(f, `${f.site} \u00b7 ${fmtBytes(f.size)}`)).join("")}</div>` : ""}`;
}

// ---------------------------------------------------------------- the open file

async function openDataFile(id, { keepTable = false } = {}) {
  try {
    DATA.file = await api.get(`/api/data/${id}`);
  } catch (err) {
    toast({ ok: false, title: "Couldn't open", message: err.message });
    DATA.file = null;
    renderDataMain();
    return;
  }
  const names = DATA.file.tables.map((t) => t.name);
  if (!keepTable || !names.includes(DATA.table)) {
    DATA.table = names[0] || "";
    resetDataView();
  }
  renderDataFiles();
  renderDataMain();
  await loadDataTab();
}

function resetDataView() {
  Object.assign(DATA, { offset: 0, order: "", desc: false, q: "", column: "", page: null, structure: null });
  DATA.selected.clear();
}

function renderDataMain() {
  const box = $("#data-main");
  if (!box) return;
  const f = DATA.file;
  if (!f) {
    box.innerHTML = `<div class="empty"><h3>No file open</h3>
      <p>Pick a SQLite file with <b>Open file\u2026</b>, or one from the lists above. Nothing is changed until you switch on Edit.</p></div>`;
    return;
  }
  const edit = f.edit;
  const access = f.kind === "access";
  const csv = f.kind === "csv";
  const queries = f.tables.filter((t) => t.type === "view").length;
  const tables = f.tables.map((t) => `<button class="data-table" data-d="table" data-name="${esc(t.name)}" aria-current="${t.name === DATA.table}">
      <span class="grow">${esc(t.name)}</span>${t.type === "view" ? '<span class="chip">view</span>' : ""}
      <small>${t.rows === null ? "?" : t.rows.toLocaleString()}</small></button>`).join("");
  box.innerHTML = `<div class="card data-head">
      <div class="data-head-main">
        <h2 class="data-name">${esc(f.name)}</h2>
        <div class="muted mono data-path" title="${esc(f.path)}">${esc(f.path)}</div>
        <div class="row data-facts"><span class="chip">${fmtBytes(f.size)}</span><span class="chip">changed ${esc(fmtDate(f.modified))}</span>
          ${access ? `<span class="chip chip-blue">${esc(f.engine)}</span>
            <span class="chip" title="varsha reads a copy; it is refreshed when the file changes">copy from ${esc(fmtDate(f.snapshot))}</span>
            <span class="chip">${plural(f.tables.filter((t) => t.type === "table").length, "table")}, ${queries} ${queries === 1 ? "query" : "queries"}</span>`
          : csv ? `<span class="chip chip-blue">CSV \u00b7 ${esc(f.csv.delimiter)}</span><span class="chip">${esc(f.csv.encoding)}</span>
            <span class="chip">${esc(f.csv.newline)}</span><span class="chip">${f.csv.header ? "first row is the header" : "no header row"}</span>
            <span class="chip">${plural(f.tables[0] ? f.tables[0].rows : 0, "row")}</span>`
          : `<span class="chip">${esc(f.pragmas.journal_mode)} journal</span><span class="chip">SQLite ${esc(f.sqlite)}</span>
            <span class="chip">${plural(f.tables.length, "table")}, ${plural(f.indexes, "index")}${f.triggers ? `, ${plural(f.triggers, "trigger")}` : ""}</span>`}</div>
      </div>
      <div class="data-head-side">
        ${access ? `<span class="chip chip-ink" title="Access files are read from a copy and never changed">\u{1f512} read-only (Access)</span>`
        : csv && !f.editable ? `<span class="chip chip-ink" title="Big CSV files open read-only">\u{1f512} read-only (over 50 MB)</span>`
        : `<div class="seg" role="group" aria-label="Mode">
          <button class="seg-btn" data-d="mode" data-edit="0" aria-pressed="${!edit}">\u{1f512} Read-only</button>
          <button class="seg-btn" data-d="mode" data-edit="1" aria-pressed="${edit}">\u270f\ufe0f Edit</button></div>`}
        <div class="row">
          <button class="btn btn-sm" data-d="integrity">${access ? "Reload copy" : csv ? "Read again" : "Check"}</button>
          ${access ? "" : `${csv ? "" : `<button class="btn btn-sm" data-d="vacuum" ${edit ? "" : "disabled"} title="${edit ? "Compact the file" : "Switch on Edit first"}">Vacuum</button>`}
          <button class="btn btn-sm" data-d="backup">Backup now</button>
          <button class="btn btn-sm" data-d="restore" ${f.backups.length ? "" : "disabled"}>Restore\u2026</button>
          ${csv ? "" : `<a class="btn btn-sm" href="/api/data/${esc(f.id)}/export.sql" download>Export .sql</a>`}`}</div>
      </div></div>
    ${edit ? `<div class="banner"><p><b>Edit mode:</b> changes are written to the file right away.
      ${f.backups.length ? `Latest backup: ${esc(f.backups[0].name)}.` : "A backup is saved before your first change."}</p></div>` : ""}
    <div class="data-body">
      <nav class="data-tables" aria-label="Tables">${tables || '<p class="muted">No tables.</p>'}</nav>
      <section class="data-panel card">
        <div class="data-tabs seg" role="tablist">
          ${[["rows", "Data"], ["structure", "Structure"], ["sql", "SQL"]].map(([k, l]) =>
            `<button class="seg-btn" role="tab" data-d="tab" data-tab="${k}" aria-pressed="${DATA.tab === k}">${l}</button>`).join("")}</div>
        <div id="data-tab"></div>
      </section></div>`;
}

async function loadDataTab() {
  const f = DATA.file;
  if (!f) return;
  if (DATA.tab === "sql") { renderSqlTab(); return; }
  if (!DATA.table) { $("#data-tab").innerHTML = '<p class="muted">This file has no tables yet. Create one in the SQL tab (switch on Edit first).</p>'; return; }
  try {
    if (DATA.tab === "structure") {
      DATA.structure = await api.get(`/api/data/${f.id}/structure?table=${encodeURIComponent(DATA.table)}`);
      renderStructureTab();
    } else {
      const params = new URLSearchParams({ table: DATA.table, offset: DATA.offset, limit: 100, order: DATA.order,
        desc: DATA.desc ? "1" : "", q: DATA.q, column: DATA.column });
      DATA.page = await api.get(`/api/data/${f.id}/rows?${params}`);
      renderRowsTab();
    }
  } catch (err) {
    $("#data-tab").innerHTML = `<div class="banner banner-coral"><p>${esc(err.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------- values

function cellHtml(c) {
  switch (c.t) {
    case "null": return '<span class="data-null">NULL</span>';
    case "blob": return `<span class="data-blob" title="${esc(c.hex)}\u2026">BLOB ${fmtBytes(c.size)}</span>`;
    case "int": case "real": return `<span class="data-num">${esc(c.v)}</span>`;
    default: {
      const text = String(c.v);
      const short = text.length > 120 ? `${text.slice(0, 120)}\u2026` : text;
      return `<span class="data-text">${esc(short)}</span>${c.more ? `<small class="muted"> (${c.more.toLocaleString()} chars)</small>` : ""}`;
    }
  }
}

function affinity(type) {
  const t = (type || "").toUpperCase();
  if (t.includes("INT")) return "int";
  if (t.includes("REAL") || t.includes("FLOA") || t.includes("DOUB") || t.includes("NUM") || t.includes("DEC")) return "real";
  return "text";
}

// ---------------------------------------------------------------- Data tab

function renderRowsTab() {
  const p = DATA.page;
  const edit = DATA.file.edit && p.editable;
  const cols = p.columns;
  const from = p.total ? p.offset + 1 : 0;
  const to = p.offset + p.rows.length;
  const head = cols.map((c) => {
    const sorted = DATA.order === c.name;
    return `<th><button class="data-sort" data-d="sort" data-name="${esc(c.name)}" title="${esc(c.type || "any type")}${c.pk ? " \u00b7 primary key" : ""}">
      ${c.pk ? "\u{1f511} " : ""}${esc(c.name)}${sorted ? (DATA.desc ? " \u25bc" : " \u25b2") : ""}</button></th>`;
  }).join("");
  const body = p.rows.map((r, i) => `<tr${DATA.selected.has(i) ? ' class="is-selected"' : ""}>
      ${edit ? `<td class="data-check"><input type="checkbox" data-d="select" data-i="${i}" ${DATA.selected.has(i) ? "checked" : ""} aria-label="Select row"></td>` : ""}
      ${r.cells.map((c, j) => `<td${edit && c.t !== "blob" ? ` class="data-editable" data-d="cell" data-i="${i}" data-j="${j}" title="Click to edit"` : ""}>${cellHtml(c)}</td>`).join("")}</tr>`).join("");
  $("#data-tab").innerHTML = `<form class="data-search" data-d="search">
      <input class="input" name="q" value="${esc(DATA.q)}" placeholder="Search ${esc(DATA.table)}\u2026" aria-label="Search" spellcheck="false">
      <select class="select" name="column" aria-label="In column"><option value="">all columns</option>
        ${cols.map((c) => `<option value="${esc(c.name)}" ${DATA.column === c.name ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
      <button class="btn btn-sm" type="submit">Search</button>
      ${DATA.q ? '<button class="btn btn-sm" type="button" data-d="clear-search">Clear</button>' : ""}
      <span class="grow"></span>
      ${edit ? `<button class="btn btn-sm btn-mint" type="button" data-d="add-row">+ Row</button>
        <button class="btn btn-sm btn-coral" type="button" data-d="delete-rows" ${DATA.selected.size ? "" : "disabled"}>Delete ${DATA.selected.size || ""}</button>` : ""}
      <a class="btn btn-sm" href="/api/data/${esc(DATA.file.id)}/export.csv?table=${encodeURIComponent(DATA.table)}" download>CSV</a>
    </form>
    ${!p.editable && DATA.file.edit ? '<p class="muted">This is a view: its rows can\u2019t be edited here.</p>' : ""}
    <div class="data-grid-wrap"><table class="data-grid">
      <thead><tr>${edit ? '<th class="data-check"></th>' : ""}${head}</tr></thead>
      <tbody>${body || `<tr><td colspan="${cols.length + (edit ? 1 : 0)}" class="muted">No rows${DATA.q ? " match" : ""}.</td></tr>`}</tbody></table></div>
    <div class="row data-pager"><span class="muted">${p.total ? `${from.toLocaleString()}\u2013${to.toLocaleString()} of ${p.total.toLocaleString()}` : "0 rows"}</span>
      <span class="grow"></span>
      <button class="btn btn-sm" data-d="page" data-step="-1" ${p.offset > 0 ? "" : "disabled"}>\u2190 Previous</button>
      <button class="btn btn-sm" data-d="page" data-step="1" ${to < p.total ? "" : "disabled"}>Next \u2192</button></div>`;
}

async function cellDialog(i, j) {
  const p = DATA.page;
  const row = p.rows[i];
  const col = p.columns[j];
  let value = row.cells[j];
  if (value.more) {
    try {
      value = await api.get(`/api/data/${DATA.file.id}/cell?${new URLSearchParams({ table: DATA.table, key: JSON.stringify(row.key), column: col.name })}`);
    } catch (err) { toast({ ok: false, title: "Couldn't load the value", message: err.message }); return; }
  }
  const type = value.t === "null" ? "null" : value.t;
  dialog(`<h2>${esc(col.name)}</h2>
    <p class="muted">${esc(DATA.table)} \u00b7 ${esc(col.type || "any type")}${col.notnull ? " \u00b7 NOT NULL" : ""}${col.pk ? " \u00b7 primary key" : ""}</p>
    <label>Value<textarea class="input mono data-value" name="v" rows="${value.t === "text" && String(value.v).length > 80 ? 8 : 3}" spellcheck="false">${value.t === "null" ? "" : esc(value.v)}</textarea></label>
    <label>Store as<select class="select" name="t">
      ${[["text", "Text"], ["int", "Integer"], ["real", "Real number"], ["null", "NULL (empty)"]].map(([k, l]) =>
        `<option value="${k}" ${k === type ? "selected" : ""}>${l}</option>`).join("")}</select></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-yellow" value="ok">Save</button></div>`,
  async (form) => {
    const t = form.get("t");
    const result = await api.post(`/api/data/${DATA.file.id}/update`, { table: DATA.table, key: row.key, column: col.name,
      value: t === "null" ? { t: "null" } : { t, v: form.get("v") } });
    toast({ ok: true, title: DATA.file.name, message: result.message });
    await refreshDataFile();
  });
}

function addRowDialog() {
  const cols = DATA.page.columns;
  dialog(`<h2>New row in ${esc(DATA.table)}</h2>
    <p class="muted">Leave a field empty to use its default (or NULL). An empty integer primary key gets the next number.</p>
    <div class="data-form">${cols.map((c) => `<label>${esc(c.name)} <small class="muted">${esc(c.type || "any")}${c.notnull ? " \u00b7 required" : ""}${c.pk ? " \u00b7 key" : ""}</small>
      <input class="input mono" name="c:${esc(c.name)}" spellcheck="false" autocomplete="off" placeholder="${c.default !== null && c.default !== undefined ? `default ${esc(c.default)}` : ""}"></label>`).join("")}</div>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-mint" value="ok">Add row</button></div>`,
  async (form) => {
    const values = {};
    for (const c of cols) {
      const v = form.get(`c:${c.name}`);
      if (v !== null && v !== "") values[c.name] = { t: affinity(c.type), v };
    }
    const result = await api.post(`/api/data/${DATA.file.id}/insert`, { table: DATA.table, values });
    toast({ ok: true, title: DATA.file.name, message: result.message });
    await refreshDataFile();
  });
}

function deleteRowsDialog() {
  const keys = [...DATA.selected].map((i) => DATA.page.rows[i].key);
  confirmDialog(`Delete ${plural(keys.length, "row")}?`, `They are removed from ${DATA.table}. The backup made before your first change still has them.`,
    "Delete", async () => {
      const result = await api.post(`/api/data/${DATA.file.id}/delete`, { table: DATA.table, keys });
      toast({ ok: true, title: DATA.file.name, message: result.message });
      DATA.selected.clear();
      await refreshDataFile();
    });
}

// ---------------------------------------------------------------- Structure tab

function renderStructureTab() {
  const s = DATA.structure;
  const edit = DATA.file.edit;
  $("#data-tab").innerHTML = `<div class="data-grid-wrap"><table class="data-grid">
      <thead><tr><th>#</th><th>Column</th><th>Type</th><th>Not null</th><th>Default</th><th>Key</th></tr></thead>
      <tbody>${s.columns.map((c) => `<tr><td class="muted">${c.cid}</td><td><b>${esc(c.name)}</b></td><td class="mono">${esc(c.type || "\u2014")}</td>
        <td>${c.notnull ? "\u2714" : ""}</td><td class="mono">${c.default === null ? "" : esc(c.default)}</td><td>${c.pk ? `\u{1f511} ${c.pk}` : ""}</td></tr>`).join("")}</tbody></table></div>
    ${s.indexes.length ? `<h3 class="data-sub">Indexes</h3><ul class="data-list">${s.indexes.map((x) =>
      `<li><b>${esc(x.name)}</b> (${x.columns.map(esc).join(", ")})${x.unique ? ' <span class="chip">unique</span>' : ""}</li>`).join("")}</ul>` : ""}
    ${s.foreign_keys.length ? `<h3 class="data-sub">Foreign keys</h3><ul class="data-list">${s.foreign_keys.map((k) =>
      `<li><b>${esc(k.column)}</b> \u2192 ${esc(k.table)}.${esc(k.to)} <span class="muted">on delete ${esc(k.on_delete)}</span></li>`).join("")}</ul>` : ""}
    ${s.triggers.length ? `<h3 class="data-sub">Triggers</h3><p>${s.triggers.map(esc).join(", ")}</p>` : ""}
    ${s.sql ? `<h3 class="data-sub">CREATE statement</h3><pre class="data-sql-text">${esc(s.sql)}</pre>` : ""}
    ${edit && s.type === "table" ? `<form class="data-search" data-d="add-column">
        <input class="input" name="name" placeholder="new_column" pattern="[A-Za-z_][A-Za-z0-9_]*" required spellcheck="false" aria-label="Column name">
        <select class="select" name="type" aria-label="Type">${["TEXT", "INTEGER", "REAL", "NUMERIC", "BLOB"].map((t) => `<option>${t}</option>`).join("")}</select>
        <input class="input" name="default" placeholder="default (optional)" spellcheck="false" aria-label="Default value">
        <button class="btn btn-sm btn-mint" type="submit">Add column</button>
        <span class="grow"></span>
        ${DATA.file.kind === "csv" ? "" : `<button class="btn btn-sm btn-coral" type="button" data-d="drop-table">Drop ${s.type}</button>`}</form>` : ""}`;
}

// ---------------------------------------------------------------- SQL tab

function sqlHistory() {
  try { return JSON.parse(localStorage.getItem(DATA_HISTORY_KEY) || "[]"); } catch { return []; }
}

function renderSqlTab() {
  const r = DATA.result;
  const history = sqlHistory();
  let out = "";
  if (r && r.error) out = `<div class="banner banner-coral"><p>${esc(r.error)}</p></div>`;
  else if (r) {
    const info = `${plural(r.statements, "statement")} \u00b7 ${r.ms} ms${r.changed ? ` \u00b7 ${plural(r.changed, "row")} changed` : ""}${r.truncated ? " \u00b7 first 1,000 rows shown" : ""}`;
    out = `<p class="muted">${info}</p>${r.columns.length ? `<div class="data-grid-wrap"><table class="data-grid">
      <thead><tr>${r.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${r.rows.map((row) => `<tr>${row.map((c) => `<td>${cellHtml(c)}</td>`).join("")}</tr>`).join("") ||
        `<tr><td colspan="${r.columns.length}" class="muted">No rows.</td></tr>`}</tbody></table></div>` : ""}`;
  }
  $("#data-tab").innerHTML = `<form class="data-sql" data-d="sql">
      <textarea class="input mono" name="sql" rows="6" spellcheck="false" placeholder="${DATA.file.kind === "access"
        ? `SELECT TOP 10 * FROM [${esc(DATA.table || "my_table")}]` : `SELECT * FROM ${esc(DATA.table || "my_table")} LIMIT 10;`}">${esc(DATA.sql)}</textarea>
      <div class="row"><button class="btn btn-ink" type="submit">\u25b6 Run</button><span class="muted">Ctrl+Enter.
        ${DATA.file.kind === "access" ? "Access SQL, SELECT only: TOP n instead of LIMIT, [brackets] for names, #2026-11-04# for dates."
          : DATA.file.kind === "csv" ? `SQLite SQL on the file's rows (table <b>${esc(DATA.table)}</b>), SELECT only: edit rows in the Data tab.`
          : DATA.file.edit ? "Edit is on: the whole script runs in one transaction (all or nothing)." : "Read-only: SELECT and PRAGMA only."}</span></div>
    </form>
    <div id="data-sql-out">${out}</div>
    ${history.length ? `<h3 class="data-sub">History</h3><div class="data-history">${history.map((h, i) =>
      `<button class="data-history-item mono" data-d="history" data-i="${i}" title="${esc(h)}">${esc(h.length > 140 ? h.slice(0, 140) + "\u2026" : h)}</button>`).join("")}</div>` : ""}`;
  const box = $("#data-tab textarea");
  box.addEventListener("input", () => { DATA.sql = box.value; });
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); box.form.requestSubmit(); }
  });
}

async function runSql(sql) {
  DATA.sql = sql;
  try {
    DATA.result = (await api.post(`/api/data/${DATA.file.id}/sql`, { sql })).message;
    const history = [sql, ...sqlHistory().filter((h) => h !== sql)].slice(0, 20);
    try { localStorage.setItem(DATA_HISTORY_KEY, JSON.stringify(history)); } catch { /* private mode: no history */ }
    if (DATA.result.edit) await refreshDataFile({ keepTab: true });
  } catch (err) {
    DATA.result = { error: err.message };
  }
  if (DATA.tab === "sql") renderSqlTab();
}

// ---------------------------------------------------------------- actions

async function refreshDataFile({ keepTab = true } = {}) {
  const tab = DATA.tab;
  await openDataFile(DATA.file.id, { keepTable: true });
  if (keepTab && DATA.tab !== tab) { DATA.tab = tab; renderDataMain(); await loadDataTab(); }
}

async function dataAction(action, body = {}) {
  try {
    const result = await api.post(`/api/data/${DATA.file.id}/${action}`, body);
    toast({ ok: true, title: DATA.file.name, message: result.message });
    return true;
  } catch (err) {
    toast({ ok: false, title: DATA.file.name, message: err.message });
    return false;
  }
}

function restoreDataDialog() {
  const f = DATA.file;
  dialog(`<h2>Restore ${esc(f.name)}</h2>
    <p>The file goes back to the chosen backup. Its current state is saved as a new backup first, so this can be undone.</p>
    ${f.edit ? "" : '<p class="banner"><b>Switch on Edit first</b> to restore.</p>'}
    <div class="backup-list">${f.backups.map((b, i) => `<label class="backup-item"><input type="radio" name="name" value="${esc(b.name)}" ${i === 0 ? "checked" : ""}>
      <span class="grow mono">${esc(b.name)}</span><span class="muted">${fmtBytes(b.size)} \u00b7 ${esc(fmtDate(b.modified))}</span></label>`).join("")}</div>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok" ${f.edit ? "" : "disabled"}>Restore</button></div>`,
  async (form) => {
    const result = await api.post(`/api/data/${f.id}/restore`, { name: form.get("name") });
    toast({ ok: true, title: f.name, message: result.message });
    await refreshDataFile();
  });
}

function dropTableDialog() {
  const s = DATA.structure;
  dialog(`<h2>Drop ${esc(s.name)}?</h2>
    <p>The ${esc(s.type)} and all its rows are deleted from the file. The backup made before your first change still has it.</p>
    <label>Type <code>${esc(s.name)}</code> to confirm<input class="input" name="confirm" autocomplete="off" spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok">Drop</button></div>`,
  async (form) => {
    if (form.get("confirm") !== s.name) throw new Error(`Type ${s.name} exactly to confirm.`);
    const result = await api.post(`/api/data/${DATA.file.id}/drop-table`, { table: s.name, confirm: s.name });
    toast({ ok: true, title: DATA.file.name, message: result.message });
    DATA.table = "";
    await openDataFile(DATA.file.id);
  });
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-d]");
  if (!el || !el.closest("#data") || el.tagName === "FORM") return;
  const d = el.dataset.d;
  if (d === "select") {
    const i = Number(el.dataset.i);
    if (el.checked) DATA.selected.add(i); else DATA.selected.delete(i);
    el.closest("tr").classList.toggle("is-selected", el.checked);
    const del = $('#data-tab [data-d="delete-rows"]');
    if (del) { del.disabled = !DATA.selected.size; del.textContent = `Delete ${DATA.selected.size || ""}`; }
    return;
  }
  event.preventDefault();
  switch (d) {
    case "pick": {
      el.disabled = true;
      try {
        const picked = (await api.post("/api/data/pick", {})).file;
        if (picked) { await loadDataList(); await openDataFile(picked.id); }
      } catch (err) { toast({ ok: false, title: "Open file", message: err.message }); }
      finally { el.disabled = false; }
      return;
    }
    case "open": await openDataFile(el.dataset.id); return;
    case "forget":
      try { await api.post(`/api/data/${el.dataset.id}/forget`, {}); } catch { /* already gone */ }
      if (DATA.file && DATA.file.id === el.dataset.id) { DATA.file = null; renderDataMain(); }
      await loadDataList();
      return;
    case "table": DATA.table = el.dataset.name; resetDataView(); renderDataMain(); await loadDataTab(); return;
    case "tab": DATA.tab = el.dataset.tab; renderDataMain(); await loadDataTab(); return;
    case "mode":
      if (await dataAction("mode", { edit: el.dataset.edit === "1" })) await refreshDataFile();
      return;
    case "integrity":  // Access: "Reload copy" (the copy is refreshed if the file changed)
      if (await dataAction("integrity") && DATA.file.kind !== "sqlite") await refreshDataFile();
      return;
    case "vacuum": if (await dataAction("vacuum")) await refreshDataFile(); return;
    case "backup": if (await dataAction("backup")) await refreshDataFile(); return;
    case "restore": restoreDataDialog(); return;
    case "sort":
      if (DATA.order === el.dataset.name) DATA.desc = !DATA.desc; else { DATA.order = el.dataset.name; DATA.desc = false; }
      DATA.offset = 0; DATA.selected.clear(); await loadDataTab(); return;
    case "page":
      DATA.offset = Math.max(0, DATA.offset + Number(el.dataset.step) * 100); DATA.selected.clear(); await loadDataTab(); return;
    case "clear-search": DATA.q = ""; DATA.column = ""; DATA.offset = 0; await loadDataTab(); return;
    case "cell": cellDialog(Number(el.dataset.i), Number(el.dataset.j)); return;
    case "add-row": addRowDialog(); return;
    case "delete-rows": deleteRowsDialog(); return;
    case "drop-table": dropTableDialog(); return;
    case "history": DATA.sql = sqlHistory()[Number(el.dataset.i)] || ""; renderSqlTab(); $("#data-tab textarea").focus(); return;
    default:
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-d]");
  if (!form || !form.closest("#data")) return;
  event.preventDefault();
  const data = new FormData(form);
  if (form.dataset.d === "search") {
    DATA.q = String(data.get("q") || "").trim();
    DATA.column = String(data.get("column") || "");
    DATA.offset = 0; DATA.selected.clear();
    await loadDataTab();
  } else if (form.dataset.d === "sql") {
    await runSql(String(data.get("sql") || ""));
  } else if (form.dataset.d === "add-column") {
    const def = String(data.get("default") || "");
    const type = String(data.get("type"));
    const ok = await dataAction("add-column", { table: DATA.table, name: data.get("name"), type,
      default: def === "" ? null : { t: affinity(type), v: def } });
    if (ok) await refreshDataFile();
  }
});
