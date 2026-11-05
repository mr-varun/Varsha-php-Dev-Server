// varsha control panel: the MDB Rescue page (Tools menu). Loaded after app.js and uses its helpers.
// The page manages itself: app.js builds the skeleton once; live progress arrives as "rescue" events.
"use strict";

const RES = {
  listing: null,     // /api/rescue: available, run, recent, jobs, databases
  file: "",          // id of the chosen file
  preset: "recommended",
  options: { sql: true, mdb: true, csv: false, deleted: false },
  password: "",
  job: null,         // /api/rescue/job/<job>: the results being shown
  lastStatus: "",
};
const RES_PRESETS = [
  ["recommended", "\u2714 Recover my database", "Recommended. Access repair, the driver copy and the raw page reader, cross-checked and merged."],
  ["raw", "\u26a0 File won't open at all", "Raw page read only: works when Access can't open the file (also encrypted files)."],
  ["carve", "\u25a3 Disk image / cut-off file", "Carving: finds Access pages anywhere (disk images, undeleted or cut-off files)."],
  ["diagnose", "\u2315 Just diagnose", "Checks the file read-only and explains what is wrong. Nothing is recovered."],
];

function viewRescueSkeleton() {
  return `<div id="rescue" class="rescue-page"><div id="rescue-setup"></div><div id="rescue-run"></div>
    <div id="rescue-result"></div><div id="rescue-jobs"></div></div>`;
}

async function renderRescue() {
  await loadRescue();
  renderRescueSetup();
  renderRescueRun();
  renderRescueJobs();
  if (RES.job) renderRescueResult();
  else if (RES.listing && RES.listing.jobs.length && !(RES.listing.run && RES.listing.run.status === "running")) {
    await showRescueJob(RES.listing.jobs[0].job);  // the latest results, as the tool's window does
  }
}

async function loadRescue() {
  try { RES.listing = await api.get("/api/rescue"); } catch (err) { toast({ ok: false, title: "MDB Rescue", message: err.message }); }
  if (RES.listing && !RES.file && RES.listing.recent.length) RES.file = RES.listing.recent[0].id;
}

function rescueRunning() { return Boolean(RES.listing && RES.listing.run && RES.listing.run.status === "running"); }

// ---------------------------------------------------------------- 1 + 2: file and choices

function renderRescueSetup() {
  const box = $("#rescue-setup");
  if (!box || !RES.listing) return;
  const L = RES.listing;
  if (L.available) {
    box.innerHTML = `<div class="banner banner-coral"><p><b>MDB Rescue isn't set up.</b> ${esc(L.available)}</p></div>`;
    return;
  }
  const running = rescueRunning();
  const files = L.recent.map((f) => `<span class="data-file${f.id === RES.file ? " is-open" : ""}">
      <button class="data-file-open" data-r="file" data-id="${esc(f.id)}" title="${esc(f.path)}" ${f.exists === false || running ? "disabled" : ""}>
        <b>${esc(f.name)}</b><small>${f.exists === false ? "missing" : `${fmtBytes(f.size)} \u00b7 ${esc(f.path.replace(/[\\/][^\\/]*$/, ""))}`}</small></button>
      <button class="data-file-x" data-r="forget" data-id="${esc(f.id)}" title="Remove from the list (the file stays)" aria-label="Remove ${esc(f.name)}">\u00d7</button></span>`).join("");
  const chosen = L.recent.find((f) => f.id === RES.file);
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">1</span> The damaged file</h3>
        <span class="chip chip-ink" title="The tool copies the file first and checks the copy (SHA-256)">\u{1f512} the original is only read</span></div>
      <div class="toolbar rescue-pick"><button class="btn btn-yellow" data-r="pick" ${running ? "disabled" : ""}>\u{1f4c2} Choose file\u2026</button>
        <p class="muted">An Access database (.mdb, Access 97 to 2003) that won't open, says "needs to be repaired" or "unrecognized format",
          or a disk image / cut-off copy. It is copied first; all work happens on the copy.</p></div>
      ${files ? `<div class="data-files"><span class="data-files-label">Recent</span>${files}</div>` : ""}
    </div>
    <div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">2</span> What do you need?</h3></div>
      <div class="kinds kinds-4">${RES_PRESETS.map(([k, t, d]) => `<label class="kind"><input type="radio" name="rescue-preset" value="${k}" data-r="preset"
          ${RES.preset === k ? "checked" : ""} ${running ? "disabled" : ""}><b>${esc(t)}</b><small>${esc(d)}</small></label>`).join("")}</div>
      <div class="rescue-options">
        <span class="muted rescue-options-label">Results</span>
        ${[["sql", "MySQL / MariaDB .sql"], ["mdb", "Clean rebuilt .mdb"], ["csv", "CSV per table"], ["deleted", "Deleted / old rows"]].map(([k, l]) =>
          `<label class="toggle"><input type="checkbox" data-r="opt" data-k="${k}" ${RES.options[k] ? "checked" : ""}
            ${running || RES.preset === "diagnose" ? "disabled" : ""}> ${l}</label>`).join("")}
        <input class="input rescue-password" type="password" data-r="password" placeholder="Database password (if it has one)"
          autocomplete="off" value="${esc(RES.password)}" ${running ? "disabled" : ""} aria-label="Database password">
      </div>
      <div class="row rescue-go">
        ${running ? `<button class="btn btn-coral" data-r="stop">\u25a0 Stop</button><span class="muted">Stops at the next safe point; a report is still written.</span>`
          : `<button class="btn btn-ink rescue-start" data-r="start" ${chosen && chosen.exists !== false ? "" : "disabled"}>\u25b6 Start rescue</button>
             <span class="muted">${chosen ? `${esc(chosen.name)} \u00b7 results go to <code>data\\mdb-rescue-jobs</code>` : "Choose a file first."}</span>`}
      </div>
    </div>`;
}

// ---------------------------------------------------------------- 3: progress

function renderRescueRun() {
  const box = $("#rescue-run");
  if (!box || !RES.listing) return;
  const run = RES.listing.run;
  if (!run) { box.innerHTML = ""; return; }
  const order = run.steps;
  const current = order.indexOf(run.stage);
  const rows = order.map((s, i) => {
    const p = run.stages[s];
    const done = p && p.total && p.done >= p.total;
    const active = run.status === "running" && s === run.stage && !done;
    const pct = p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
    const mark = done ? "\u2714" : active ? "\u25d4" : (run.status !== "running" && i > current ? "\u2013" : "\u25cb");
    return `<li class="rescue-stage${done ? " done" : ""}${active ? " active" : ""}"><span class="rescue-mark">${mark}</span>
      <span class="grow"><b>${esc(run.stage_names[s] || s)}</b>${active && p && p.note ? `<small> \u00b7 ${esc(p.note)}</small>` : ""}</span>
      ${active ? `<span class="rescue-bar"><span class="rescue-fill" data-pct="${pct}"></span></span><small class="mono">${pct}%</small>` : ""}</li>`;
  }).join("");
  const label = { running: "Running", completed: "Finished", stopped: "Stopped", failed: "Failed" }[run.status] || run.status;
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">3</span> ${esc(run.file)}</h3>
        <span class="chip ${run.status === "running" ? "chip-blue" : run.status === "completed" ? "chip-mint" : "chip-coral"}">${esc(label)}</span></div>
      ${run.error ? `<div class="banner banner-coral"><p>${esc(run.error)}</p></div>` : ""}
      <div class="rescue-progress"><ol class="rescue-stages">${rows}</ol>
        <div class="terminal rescue-log"><pre id="rescue-log">${esc(run.log.join("\n"))}</pre></div></div>
    </div>`;
  box.querySelectorAll(".rescue-fill").forEach((el) => { el.style.width = `${el.dataset.pct}%`; });  // CSP: no inline styles
  const log = $("#rescue-log");
  if (log) log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- 4: results

async function showRescueJob(job) {
  try { RES.job = await api.get(`/api/rescue/job/${encodeURIComponent(job)}`); }
  catch (err) { toast({ ok: false, title: "MDB Rescue", message: err.message }); return; }
  renderRescueResult();
  renderRescueJobs();
}

function renderRescueResult() {
  const box = $("#rescue-result");
  if (!box) return;
  const j = RES.job;
  if (!j) { box.innerHTML = ""; return; }
  const t = j.totals || {};
  const o = j.outputs || {};
  const source = j.source.split(/[\\/]/).pop();
  const fmt = (n) => (n === null || n === undefined ? "\u2013" : Number(n).toLocaleString());
  const rows = j.tables.map((x) => `<tr><td><b>${esc(x.table)}</b></td><td class="data-num">${fmt(x.rows_found)}</td>
      <td class="data-num">${fmt(x.rows_recovered)}</td><td class="data-num${x.rows_damaged ? " rescue-bad" : ""}">${fmt(x.rows_damaged)}</td>
      <td>${esc(x.source || "\u2013")}</td></tr>`).join("");
  const actions = [
    o.recovered_sqlite ? `<button class="btn btn-blue" data-r="to-data" data-k="recovered_sqlite" title="${esc(o.recovered_sqlite.name)}">Browse the recovered data \u2192</button>` : "",
    o.rebuilt_mdb ? `<button class="btn" data-r="to-data" data-k="rebuilt_mdb" title="${esc(o.rebuilt_mdb.name)}">Open the rebuilt .mdb</button>` : "",
    o.repaired_mdb ? `<button class="btn" data-r="to-data" data-k="repaired_mdb" title="${esc(o.repaired_mdb.name)}">Open the repaired .mdb</button>` : "",
    o.sql_dump ? `<button class="btn btn-mint" data-r="import">Import into MariaDB / MySQL\u2026</button>` : "",
    j.has_report ? `<button class="btn" data-r="open" data-what="report">Open report</button>` : "",
    `<button class="btn" data-r="open" data-what="folder">Open job folder</button>`,
  ].join("");
  box.innerHTML = `<div class="card rescue-card">
      <div class="card-head"><h3 class="card-title"><span class="rescue-step">4</span> Results: ${esc(source)}</h3>
        <span class="chip ${j.status === "completed" ? "chip-mint" : "chip-coral"}">${esc(j.status)}</span></div>
      <div class="rescue-tiles">
        <div class="rescue-tile"><b>${fmt(t.tables)}</b><small>tables</small></div>
        <div class="rescue-tile"><b>${fmt(t.rows_recovered)}<span class="muted"> / ${fmt(t.rows_found)}</span></b><small>rows recovered</small></div>
        <div class="rescue-tile ${t.percent === 100 ? "good" : ""}"><b>${t.percent === undefined ? "\u2013" : `${t.percent}%`}</b><small>of the rows</small></div>
        <div class="rescue-tile ${t.rows_damaged ? "bad" : ""}"><b>${fmt(t.rows_damaged)}</b><small>damaged / flagged</small></div>
      </div>
      ${j.verdict ? `<p class="rescue-verdict"><b>Diagnosis:</b> ${esc(j.verdict)}</p>` : ""}
      ${j.warnings.length ? `<ul class="data-list rescue-warnings">${j.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <div class="row rescue-actions">${actions}</div>
      ${rows ? `<div class="data-grid-wrap"><table class="data-grid"><thead><tr><th>Table</th><th>Rows found</th><th>Recovered</th><th>Damaged</th><th>Source</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : ""}
    </div>`;
}

function importDialog() {
  const j = RES.job;
  const dbs = (RES.listing.databases || []).filter((d) => d.running);
  const name = `rescued_${(j.source.split(/[\\/]/).pop() || "db").replace(/\.[^.]*$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_")}`.slice(0, 64);
  dialog(`<h2>Import the recovered data</h2>
    <p>The <code>.sql</code> dump goes into a <b>new</b> database. An existing database is never touched.</p>
    <label>Server<select class="select" name="service">${dbs.map((d) => `<option value="${esc(d.id)}" ${/maria/i.test(d.name) ? "selected" : ""}>${esc(d.name)} (:${d.port})</option>`).join("")}</select>
      ${dbs.length ? "" : '<small class="muted">Start MariaDB or MySQL first.</small>'}</label>
    <label>New database name<input class="input mono" name="database" value="${esc(name)}" pattern="[A-Za-z0-9_$-]{1,64}" required spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-mint" value="ok" ${dbs.length ? "" : "disabled"}>Import</button></div>`,
  async (form) => {
    await api.post(`/api/rescue/job/${encodeURIComponent(j.job)}/import`, { service: form.get("service"), database: form.get("database") });
    toast({ ok: true, title: "Importing\u2026", message: `Into ${form.get("database")}; you get a message when it's done.` });
  });
}

// ---------------------------------------------------------------- earlier jobs

function renderRescueJobs() {
  const box = $("#rescue-jobs");
  if (!box || !RES.listing) return;
  const jobs = RES.listing.jobs;
  if (!jobs.length) { box.innerHTML = ""; return; }
  const working = RES.listing.jobs_working || 0;
  box.innerHTML = `<h2 class="section-title">Rescue jobs</h2>
    <p class="muted rescue-jobs-note">${plural(jobs.length, "job")} use <b>${fmtBytes(RES.listing.jobs_size || 0)}</b>${working
      ? `, of which <b>${fmtBytes(working)}</b> are working files (the tool's copies and stage folders) that <b>Clean</b> removes` : ""}.
      Nothing is deleted unless you choose it.</p>
    <div class="rescue-joblist">${jobs.map((j) => {
      const busy = j.status === "running";
      return `<div class="rescue-job${RES.job && RES.job.job === j.job ? " is-open" : ""}">
      <button class="rescue-job-open" data-r="job" data-job="${esc(j.job)}" title="Show its results"><b>${esc(j.source)}</b>
      <span class="chip ${j.status === "completed" ? "chip-mint" : busy ? "chip-blue" : "chip-coral"}">${esc(j.status)}</span>
      <span class="muted">${j.rows_found === null || j.rows_found === undefined ? "" : `${Number(j.rows_recovered || 0).toLocaleString()} / ${Number(j.rows_found).toLocaleString()} rows`}</span>
      <span class="grow"></span><small class="muted">${fmtBytes(j.size || 0)} · ${esc(fmtDate(j.modified))}</small></button>
      <div class="row rescue-job-actions">
        <button class="btn btn-sm" data-r="browse-job" data-job="${esc(j.job)}" title="Open the job folder in Explorer">Browse</button>
        <button class="btn btn-sm btn-mint" data-r="clean-job" data-job="${esc(j.job)}" ${busy || !j.working ? "disabled" : ""}
          title="Keep the rescued files (results, report, log); remove the working copies">Clean${j.working ? ` (${fmtBytes(j.working)})` : ""}</button>
        <button class="btn btn-sm btn-coral" data-r="delete-job" data-job="${esc(j.job)}" ${busy ? "disabled" : ""}
          title="Delete the whole job, rescued files included">Full clean</button>
      </div></div>`;
    }).join("")}</div>`;
}

// ---------------------------------------------------------------- events

function onRescueEvent(data) {
  if (!RES.listing) return;
  RES.listing.available = data.available;
  RES.listing.run = data.run;
  if (view !== "rescue" || !$("#rescue")) return;
  const status = data.run ? data.run.status : "";
  renderRescueRun();
  if (status !== RES.lastStatus) {
    const finished = RES.lastStatus === "running" && status !== "running";
    RES.lastStatus = status;
    renderRescueSetup();  // Start <-> Stop
    if (finished) {
      loadRescue().then(() => { renderRescueJobs(); if (data.run.job) showRescueJob(data.run.job); });
      toast({ ok: status === "completed", title: `Rescue ${status}`, message: data.run.file });
    }
  }
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-r]");
  if (!el || !el.closest("#rescue") || el.tagName === "INPUT") return;
  event.preventDefault();
  const r = el.dataset.r;
  try {
    if (r === "pick") {
      el.disabled = true;
      const picked = (await api.post("/api/rescue/pick", {})).file;
      if (picked) { RES.file = picked.id; await loadRescue(); renderRescueSetup(); }
    } else if (r === "file") { RES.file = el.dataset.id; renderRescueSetup(); }
    else if (r === "forget") {
      await api.post("/api/rescue/forget", { file: el.dataset.id });
      if (RES.file === el.dataset.id) RES.file = "";
      await loadRescue(); renderRescueSetup();
    } else if (r === "start") {
      RES.job = null; renderRescueResult();
      await api.post("/api/rescue/start", { file: RES.file, preset: RES.preset, options: { ...RES.options, password: RES.password } });
      RES.lastStatus = "running";
      await loadRescue(); renderRescueSetup(); renderRescueRun(); renderRescueJobs();
    } else if (r === "stop") { toast({ ok: true, title: "MDB Rescue", message: (await api.post("/api/rescue/stop", {})).message }); }
    else if (r === "job") { await showRescueJob(el.dataset.job); $("#rescue-result").scrollIntoView({ behavior: "smooth" }); }
    else if (r === "open") { await api.post(`/api/rescue/job/${encodeURIComponent(RES.job.job)}/open`, { what: el.dataset.what }); }
    else if (r === "to-data") {
      const file = (await api.post(`/api/rescue/job/${encodeURIComponent(RES.job.job)}/data`, { key: el.dataset.k })).file;
      DATA.file = { id: file.id };  // data.js opens it when its page renders
      go("data");
    } else if (r === "import") { importDialog(); }
    else if (r === "browse-job") { await api.post(`/api/rescue/job/${encodeURIComponent(el.dataset.job)}/open`, { what: "folder" }); }
    else if (r === "clean-job") {
      toast({ ok: true, title: "MDB Rescue", message: (await api.post(`/api/rescue/job/${encodeURIComponent(el.dataset.job)}/clean`, {})).message });
      await loadRescue(); renderRescueJobs();
      if (RES.job && RES.job.job === el.dataset.job) await showRescueJob(el.dataset.job);
    } else if (r === "delete-job") {
      const job = el.dataset.job;
      confirmDialog("Full clean: delete this whole job?", "Everything in it goes, the rescued files (.sql, recovered and rebuilt files, report) too. Your original file is not touched.",
        "Full clean", async () => {
          toast({ ok: true, title: "MDB Rescue", message: (await api.post(`/api/rescue/job/${encodeURIComponent(job)}/delete`, {})).message });
          if (RES.job && RES.job.job === job) { RES.job = null; renderRescueResult(); }
          await loadRescue(); renderRescueJobs();
        });
    }
  } catch (err) {
    toast({ ok: false, title: "MDB Rescue", message: err.message });
  } finally {
    if (r === "pick") el.disabled = false;
  }
});

document.addEventListener("change", (event) => {
  const el = event.target.closest("[data-r]");
  if (!el || !el.closest("#rescue")) return;
  if (el.dataset.r === "preset") { RES.preset = el.value; renderRescueSetup(); }
  else if (el.dataset.r === "opt") RES.options[el.dataset.k] = el.checked;
});

document.addEventListener("input", (event) => {
  const el = event.target.closest('[data-r="password"]');
  if (el && el.closest("#rescue")) RES.password = el.value;
});
