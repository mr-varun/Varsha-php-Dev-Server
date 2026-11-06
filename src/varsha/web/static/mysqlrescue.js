// varsha control panel: the MySQL Rescue page (Tools menu). Loaded after app.js and uses its helpers.
// The page manages itself: app.js builds the skeleton once; live progress arrives as "mysql_rescue" events.
"use strict";

const MYR = {
  listing: null,     // /api/mysql-rescue: available, run, recent, jobs, databases
  folder: "",        // id of the chosen data folder
  mode: "recover",
  job: null,         // /api/mysql-rescue/job/<job>: the results being shown
  lastStatus: "",
};
const MYR_MODES = [
  ["recover", "✔ Recover my databases", "Recommended. A normal start on a copy first, then recovery levels 1–6 only for what still fails; damaged pages are read around."],
  ["force", "⚠ Server won't start at all", "Straight to forced recovery (innodb_force_recovery 1–6), for a folder whose server crashes while starting."],
  ["diagnose", "⌕ Just diagnose", "Reads the folder only and explains what it finds: version, databases, tables, damage. Nothing is recovered."],
];
const MYR_API = "/api/mysql-rescue";

function viewMysqlRescueSkeleton() {
  return `<div id="mysqlrescue" class="rescue-page"><div id="myr-setup"></div><div id="myr-run"></div>
    <div id="myr-result"></div><div id="myr-jobs"></div></div>`;
}

async function renderMysqlRescue() {
  await loadMysqlRescue();
  renderMyrSetup();
  renderMyrRun();
  renderMyrJobs();
  if (MYR.job) renderMyrResult();
  else if (MYR.listing && MYR.listing.jobs.length && !myrRunning()) await showMyrJob(MYR.listing.jobs[0].job);
}

async function loadMysqlRescue() {
  try { MYR.listing = await api.get(MYR_API); } catch (err) { toast({ ok: false, title: "MySQL Rescue", message: err.message }); }
  if (MYR.listing && !MYR.folder && MYR.listing.recent.length) MYR.folder = MYR.listing.recent[0].id;
}

function myrRunning() { return Boolean(MYR.listing && MYR.listing.run && MYR.listing.run.status === "running"); }

const myrJob = (job) => `${MYR_API}/job/${encodeURIComponent(job)}`;
const myrNum = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());

// ---------------------------------------------------------------- 1 + 2: folder and choice

function renderMyrSetup() {
  const box = $("#myr-setup");
  if (!box || !MYR.listing) return;
  const L = MYR.listing;
  if (L.available) {
    box.innerHTML = `<div class="banner banner-coral"><p><b>MySQL Rescue isn't set up.</b> ${esc(L.available)}</p></div>`;
    return;
  }
  const running = myrRunning();
  const folders = L.recent.map((f) => `<span class="data-file${f.id === MYR.folder ? " is-open" : ""}">
      <button class="data-file-open" data-m="folder" data-id="${esc(f.id)}" title="${esc(f.path)}" ${f.exists === false || running ? "disabled" : ""}>
        <b>${esc(f.name)}</b><small>${f.exists === false ? "missing" : esc(f.path.replace(/[\\/][^\\/]*$/, ""))}</small></button>
      <button class="data-file-x" data-m="forget" data-id="${esc(f.id)}" title="Remove from the list (the folder stays)" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
  const chosen = L.recent.find((f) => f.id === MYR.folder);
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">1</span> The damaged data folder</h3>
        <span class="chip chip-ink" title="The tool copies the folder first and checks at the end that the original is unchanged">🔒 the original is only read</span></div>
      <div class="toolbar rescue-pick"><button class="btn btn-yellow" data-m="pick" ${running ? "disabled" : ""}>📂 Choose folder…</button>
        <p class="muted">A MySQL or MariaDB <code>data</code> folder (with <code>ibdata1</code>, <code>mysql\\</code> and a folder per database),
          or the server folder that contains it. It is copied first; a temporary server on the copy does the work, using varsha's own
          MySQL 5.6 / 8.4 or MariaDB builds. A running varsha database can't be rescued: stop it first.</p></div>
      ${folders ? `<div class="data-files"><span class="data-files-label">Recent</span>${folders}</div>` : ""}
    </div>
    <div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">2</span> What do you need?</h3></div>
      <div class="kinds">${MYR_MODES.map(([k, t, d]) => `<label class="kind"><input type="radio" name="myr-mode" value="${k}" data-m="mode"
          ${MYR.mode === k ? "checked" : ""} ${running ? "disabled" : ""}><b>${esc(t)}</b><small>${esc(d)}</small></label>`).join("")}</div>
      <div class="row rescue-go">
        ${running ? `<button class="btn btn-coral" data-m="stop">■ Stop</button><span class="muted">Stops at the next safe point; the temporary server is stopped and a report is still written.</span>`
          : `<button class="btn btn-ink rescue-start" data-m="start" ${chosen && chosen.exists !== false ? "" : "disabled"}>▶ Start rescue</button>
             <span class="muted">${chosen ? `${esc(chosen.name)} · results go to <code>data\\mysql-rescue-jobs</code>` : "Choose a folder first."}</span>`}
      </div>
    </div>`;
}

// ---------------------------------------------------------------- 3: progress

function renderMyrRun() {
  const box = $("#myr-run");
  if (!box || !MYR.listing) return;
  const run = MYR.listing.run;
  if (!run) { box.innerHTML = ""; return; }
  const order = run.steps;
  const current = order.indexOf(run.stage);
  const rows = order.map((s, i) => {
    const p = run.stages[s];
    const done = (p && p.total && p.done >= p.total) || (run.status === "running" ? i < current : Boolean(p));
    const active = run.status === "running" && s === run.stage && !(p && p.total && p.done >= p.total);
    const pct = p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
    const mark = active ? "◔" : done ? "✔" : (run.status !== "running" ? "–" : "○");
    return `<li class="rescue-stage${done && !active ? " done" : ""}${active ? " active" : ""}"><span class="rescue-mark">${mark}</span>
      <span class="grow"><b>${esc(run.stage_names[s] || s)}</b>${active && p && p.note ? `<small> · ${esc(p.note)}</small>` : ""}</span>
      ${active && p && p.total > 1 ? `<span class="rescue-bar"><span class="rescue-fill" data-pct="${pct}"></span></span><small class="mono">${pct}%</small>` : ""}</li>`;
  }).join("");
  const label = { running: "Running", completed: "Finished", partial: "Partly recovered", stopped: "Stopped", failed: "Failed" }[run.status] || run.status;
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">3</span> ${esc(run.file)}</h3>
        <span class="chip ${run.status === "running" ? "chip-blue" : run.status === "completed" ? "chip-mint" : "chip-coral"}">${esc(label)}</span></div>
      ${run.error ? `<div class="banner banner-coral"><p>${esc(run.error)}</p></div>` : ""}
      <div class="rescue-progress"><ol class="rescue-stages">${rows}</ol>
        <div class="terminal rescue-log"><pre id="myr-log">${esc(run.log.join("\n"))}</pre></div></div>
    </div>`;
  box.querySelectorAll(".rescue-fill").forEach((el) => { el.style.width = `${el.dataset.pct}%`; });  // CSP: no inline styles
  const log = $("#myr-log");
  if (log) log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- 4: results

async function showMyrJob(job) {
  try { MYR.job = await api.get(myrJob(job)); }
  catch (err) { toast({ ok: false, title: "MySQL Rescue", message: err.message }); return; }
  renderMyrResult();
  renderMyrJobs();
}

function renderMyrResult() {
  const box = $("#myr-result");
  if (!box) return;
  const j = MYR.job;
  if (!j) { box.innerHTML = ""; return; }
  const t = j.totals || {};
  const o = j.outputs || {};
  const source = j.source.split(/[\\/]/).filter(Boolean).slice(-2).join("\\");
  const statusChip = { ok: "chip-mint", partial: "chip-yellow", view: "chip-ink" };
  const dbs = j.databases.map((db) => {
    const key = `sql:${db.database}`;
    const rows = db.tables.map((x) => `<tr><td><b>${esc(x.table)}</b></td><td>${esc(x.engine || "")}</td>
        <td class="data-num">${x.status === "view" ? "" : myrNum(x.rows)}</td>
        <td><span class="chip ${statusChip[x.status] || "chip-coral"}">${esc(x.status)}</span></td>
        <td class="data-num">${x.level === null || x.level === undefined ? "" : x.level}</td>
        <td class="myr-note">${esc(x.error || "")}</td></tr>`).join("");
    return `<div class="myr-db">
        <div class="row myr-db-head"><h4 class="grow">${esc(db.database)}</h4>
          <small class="muted">${plural(db.ok || 0, "table")} recovered${db.partial ? `, ${db.partial} in part` : ""}${db.failed ? `, ${db.failed} failed` : ""} · ${myrNum(db.rows)} rows</small>
          ${o[key] ? `<small class="muted mono">${esc(o[key].name)} · ${fmtBytes(o[key].size || 0)}</small>
            <button class="btn btn-sm btn-mint" data-m="import" data-db="${esc(db.database)}">Import into MariaDB / MySQL…</button>` : ""}</div>
        ${rows ? `<div class="data-grid-wrap"><table class="data-grid"><thead><tr><th>Table</th><th>Engine</th><th>Rows</th><th>Status</th><th>Level</th><th>Note</th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : ""}</div>`;
  }).join("");
  const attempts = (j.attempts || []).map((a) => `<li><b>${esc(a.server)}</b>, level ${esc(a.level)}: ${esc(a.result)}</li>`).join("");
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">4</span> Results: ${esc(source)}</h3>
        <span class="chip ${j.status === "completed" ? "chip-mint" : "chip-coral"}">${esc(j.status)}</span></div>
      <div class="rescue-tiles">
        <div class="rescue-tile"><b>${myrNum(t.databases)}</b><small>databases</small></div>
        <div class="rescue-tile ${t.tables && t.tables_ok === t.tables ? "good" : ""}"><b>${myrNum(t.tables_ok)}<span class="muted"> / ${myrNum(t.tables)}</span></b><small>tables recovered</small></div>
        <div class="rescue-tile ${t.tables_partial || t.tables_failed ? "bad" : ""}"><b>${myrNum(t.tables_partial || 0)} · ${myrNum(t.tables_failed || 0)}</b><small>in part · failed</small></div>
        <div class="rescue-tile"><b>${myrNum(t.rows)}</b><small>rows saved</small></div>
      </div>
      ${j.detected ? `<p class="rescue-verdict"><b>Detected:</b> ${esc(j.detected)}${j.generation && !j.detected.toLowerCase().startsWith(j.generation.toLowerCase()) ? ` (${esc(j.generation)})` : ""}.
        ${j.original_unchanged === true ? "The original folder was checked again at the end: unchanged." : j.original_unchanged === false ? "<b>The original folder changed during the rescue</b> (was a server still running on it?)." : ""}</p>` : ""}
      ${j.warnings.length ? `<ul class="data-list rescue-warnings">${j.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <div class="row rescue-actions">
        ${j.has_report ? `<button class="btn" data-m="open" data-what="report">Open report</button>` : ""}
        <button class="btn" data-m="open" data-what="folder">Open job folder</button>
        <span class="muted">Each <code>.sql</code> has no CREATE DATABASE: it imports into a new database you name.</span>
      </div>
      ${dbs}
      ${attempts ? `<details class="myr-attempts"><summary>Server starts (${(j.attempts || []).length})</summary><ul class="data-list">${attempts}</ul></details>` : ""}
    </div>`;
}

function myrImportDialog(database) {
  const j = MYR.job;
  const dbs = (MYR.listing.databases || []).filter((d) => d.running);
  const name = `rescued_${database.toLowerCase().replace(/[^a-z0-9_]+/g, "_")}`.slice(0, 64);
  dialog(`<h2>Import ${esc(database)}</h2>
    <p>The rescued <code>${esc(database)}.sql</code> goes into a <b>new</b> database. An existing database is never touched.</p>
    <label>Server<select class="select" name="service">${dbs.map((d) => `<option value="${esc(d.id)}" ${/maria/i.test(d.name) ? "selected" : ""}>${esc(d.name)} (:${d.port})</option>`).join("")}</select>
      ${dbs.length ? "" : '<small class="muted">Start MariaDB or MySQL first.</small>'}</label>
    <label>New database name<input class="input mono" name="database" value="${esc(name)}" pattern="[A-Za-z0-9_$-]{1,64}" required spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-mint" value="ok" ${dbs.length ? "" : "disabled"}>Import</button></div>`,
  async (form) => {
    await api.post(`${myrJob(j.job)}/import`, { service: form.get("service"), database: form.get("database"), key: `sql:${database}` });
    toast({ ok: true, title: "Importing…", message: `Into ${form.get("database")}; you get a message when it's done.` });
  });
}

// ---------------------------------------------------------------- earlier jobs

function renderMyrJobs() {
  const box = $("#myr-jobs");
  if (!box || !MYR.listing) return;
  const jobs = MYR.listing.jobs;
  if (!jobs.length) { box.innerHTML = ""; return; }
  const working = MYR.listing.jobs_working || 0;
  box.innerHTML = `<h2 class="section-title">Rescue jobs</h2>
    <p class="muted rescue-jobs-note">${plural(jobs.length, "job")} ${jobs.length === 1 ? "uses" : "use"} <b>${fmtBytes(MYR.listing.jobs_size || 0)}</b>${working
      ? `, of which <b>${fmtBytes(working)}</b> are working files (the copy of the data folder and the per-table dumps) that <b>Clean</b> removes` : ""}.
      Nothing is deleted unless you choose it.</p>
    <div class="rescue-joblist">${jobs.map((j) => {
      const busy = j.status === "running";
      return `<div class="rescue-job${MYR.job && MYR.job.job === j.job ? " is-open" : ""}">
      <button class="rescue-job-open" data-m="job" data-job="${esc(j.job)}" title="Show its results"><b>${esc(j.source)}</b>
      <span class="chip ${j.status === "completed" ? "chip-mint" : busy ? "chip-blue" : "chip-coral"}">${esc(j.status)}</span>
      <span class="muted">${j.tables === null || j.tables === undefined ? "" : `${myrNum(j.tables_ok)} / ${myrNum(j.tables)} tables · ${myrNum(j.rows)} rows`}</span>
      <span class="grow"></span><small class="muted">${fmtBytes(j.size || 0)} · ${esc(fmtDate(j.modified))}</small></button>
      <div class="row rescue-job-actions">
        <button class="btn btn-sm" data-m="browse-job" data-job="${esc(j.job)}" title="Open the job folder in Explorer">Browse</button>
        <button class="btn btn-sm btn-mint" data-m="clean-job" data-job="${esc(j.job)}" ${busy || !j.working ? "disabled" : ""}
          title="Keep the rescued files (.sql per database, report, log); remove the working copies">Clean${j.working ? ` (${fmtBytes(j.working)})` : ""}</button>
        <button class="btn btn-sm btn-coral" data-m="delete-job" data-job="${esc(j.job)}" ${busy ? "disabled" : ""}
          title="Delete the whole job, rescued files included">Full clean</button>
      </div></div>`;
    }).join("")}</div>`;
}

// ---------------------------------------------------------------- events

function onMysqlRescueEvent(data) {
  if (!MYR.listing) return;
  MYR.listing.available = data.available;
  MYR.listing.run = data.run;
  if (view !== "mysqlrescue" || !$("#mysqlrescue")) return;
  const status = data.run ? data.run.status : "";
  renderMyrRun();
  if (status !== MYR.lastStatus) {
    const finished = MYR.lastStatus === "running" && status !== "running";
    MYR.lastStatus = status;
    renderMyrSetup();  // Start <-> Stop
    if (finished) {
      loadMysqlRescue().then(() => { renderMyrJobs(); if (data.run.job) showMyrJob(data.run.job); });
      toast({ ok: status === "completed", title: `Rescue ${status}`, message: data.run.file });
    }
  }
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-m]");
  if (!el || !el.closest("#mysqlrescue") || el.tagName === "INPUT") return;
  event.preventDefault();
  const m = el.dataset.m;
  try {
    if (m === "pick") {
      el.disabled = true;
      const picked = (await api.post(`${MYR_API}/pick`, {})).file;
      if (picked) { MYR.folder = picked.id; await loadMysqlRescue(); renderMyrSetup(); }
    } else if (m === "folder") { MYR.folder = el.dataset.id; renderMyrSetup(); }
    else if (m === "forget") {
      await api.post(`${MYR_API}/forget`, { file: el.dataset.id });
      if (MYR.folder === el.dataset.id) MYR.folder = "";
      await loadMysqlRescue(); renderMyrSetup();
    } else if (m === "start") {
      MYR.job = null; renderMyrResult();
      await api.post(`${MYR_API}/start`, { file: MYR.folder, preset: MYR.mode, options: {} });
      MYR.lastStatus = "running";
      await loadMysqlRescue(); renderMyrSetup(); renderMyrRun(); renderMyrJobs();
    } else if (m === "stop") { toast({ ok: true, title: "MySQL Rescue", message: (await api.post(`${MYR_API}/stop`, {})).message }); }
    else if (m === "job") { await showMyrJob(el.dataset.job); $("#myr-result").scrollIntoView({ behavior: "smooth" }); }
    else if (m === "open") { await api.post(`${myrJob(MYR.job.job)}/open`, { what: el.dataset.what }); }
    else if (m === "import") { myrImportDialog(el.dataset.db); }
    else if (m === "browse-job") { await api.post(`${myrJob(el.dataset.job)}/open`, { what: "folder" }); }
    else if (m === "clean-job") {
      toast({ ok: true, title: "MySQL Rescue", message: (await api.post(`${myrJob(el.dataset.job)}/clean`, {})).message });
      await loadMysqlRescue(); renderMyrJobs();
      if (MYR.job && MYR.job.job === el.dataset.job) await showMyrJob(el.dataset.job);
    } else if (m === "delete-job") {
      const job = el.dataset.job;
      confirmDialog("Full clean: delete this whole job?", "Everything in it goes, the rescued .sql files and the report too. Your original data folder is not touched.",
        "Full clean", async () => {
          toast({ ok: true, title: "MySQL Rescue", message: (await api.post(`${myrJob(job)}/delete`, {})).message });
          if (MYR.job && MYR.job.job === job) { MYR.job = null; renderMyrResult(); }
          await loadMysqlRescue(); renderMyrJobs();
        });
    }
  } catch (err) {
    toast({ ok: false, title: "MySQL Rescue", message: err.message });
  } finally {
    if (m === "pick") el.disabled = false;
  }
});

document.addEventListener("change", (event) => {
  const el = event.target.closest('[data-m="mode"]');
  if (el && el.closest("#mysqlrescue")) { MYR.mode = el.value; renderMyrSetup(); }
});
