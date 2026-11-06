// varsha control panel: Import Banks Data (top bar). Loaded after app.js and uses its helpers.
// A blank page with one drop area: drop bankdb.sql and/or inventory1.sql (exactly those names) and
// the import starts by itself; live progress arrives as "bank_import" events.
"use strict";

const BANK = { state: null, uploading: null };
const BANK_FILES = ["bankdb.sql", "inventory1.sql"];

function viewBankImportSkeleton() {
  return `<div id="bankimport" class="bank-page">
    ${MINI ? `<div class="row bank-mini-head"><b class="grow">Import Banks Data</b>
      <button class="btn btn-sm" data-b="close" title="Close this window (an import keeps running in varsha)">\u2715 Close</button></div>` : ""}
    <div id="bank-drop" class="bank-drop" tabindex="0" aria-label="Drop bankdb.sql and inventory1.sql here">
      <div class="bank-drop-inner"><b>Drop bankdb.sql and / or inventory1.sql here</b>
        <small class="muted" id="bank-drop-note"></small></div>
    </div>
    <div id="bank-run"></div></div>`;
}

async function renderBankImport() {
  try { BANK.state = await api.get("/api/bank-import"); } catch (err) { toast({ ok: false, title: "Import Banks Data", message: err.message }); }
  drawBankImport();
}

function bankBusy() { return Boolean(BANK.uploading || (BANK.state && BANK.state.status === "running")); }

function drawBankImport() {
  const drop = $("#bank-drop");
  if (!drop) return;
  const s = BANK.state || {};
  drop.classList.toggle("is-busy", bankBusy());
  // once files are dropped, the drop area gives way to the log; "Import again" brings it back
  drop.hidden = Boolean(BANK.uploading || (s.status && s.status !== "idle" && !BANK.again));
  $("#bank-drop-note").textContent = bankBusy()
    ? "An import is running…"
    : `Into ${s.service_name || "MySQL 5.6"}. Checked first; the current data is backed up, then replaced. Only the dropped ones change.`;
  const box = $("#bank-run");
  if (BANK.uploading) {
    const u = BANK.uploading;
    box.innerHTML = `<div class="card bank-card"><div class="card-head"><h3 class="card-title">Uploading ${esc(u.name)}</h3>
        <span class="chip chip-blue">${fmtBytes(u.done)} of ${fmtBytes(u.total)}</span></div>
      <span class="rescue-bar bank-bar"><span class="rescue-fill" data-pct="${u.total ? Math.round((u.done / u.total) * 100) : 0}"></span></span></div>`;
  } else if (!s.status || s.status === "idle" || BANK.again) {
    box.innerHTML = "";
    return;
  } else {
    const label = { running: "Running", done: "Done", failed: "Stopped" }[s.status] || s.status;
    const p = s.progress;
    const pct = p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
    const restore = s.status === "failed" ? Object.keys(s.backups || {}).filter((db) => (s.changed || []).includes(db)) : [];
    box.innerHTML = `<div class="card bank-card">
      <div class="card-head"><h3 class="card-title">${esc((s.files || []).join(" + "))}</h3>
        <span class="chip ${s.status === "done" ? "chip-mint" : s.status === "running" ? "chip-blue" : "chip-coral"}">${esc(label)}</span></div>
      ${p ? `<div class="row bank-progress"><b>${esc(p.label)}</b><span class="rescue-bar bank-bar"><span class="rescue-fill" data-pct="${pct}"></span></span>
        <small class="mono">${p.total ? `${pct}% · ${fmtBytes(p.done)} of ${fmtBytes(p.total)}` : "…"}</small></div>` : ""}
      ${s.error ? `<div class="banner banner-coral"><p>${esc(s.error)}</p></div>` : ""}
      ${restore.length ? `<div class="row bank-restore">${restore.map((db) => `<button class="btn btn-coral" data-b="restore" data-db="${esc(db)}">
          Restore ${esc(db)} (${esc(s.backups[db])})</button>`).join("")}</div>` : ""}
      ${Object.keys(s.backups || {}).length && !restore.length ? `<p class="muted bank-backups">Backups of the previous data: ${Object.values(s.backups).map((n) => `<code>${esc(n)}</code>`).join(", ")}</p>` : ""}
      <div class="terminal bank-log"><pre id="bank-log">${esc((s.log || []).join("\n"))}</pre></div>
      ${s.status !== "running" ? `<div class="row bank-again"><button class="btn btn-yellow" data-b="again">Import again</button></div>` : ""}
    </div>`;
  }
  box.querySelectorAll(".rescue-fill").forEach((el) => { el.style.width = `${el.dataset.pct}%`; });  // CSP: no inline styles
  const log = $("#bank-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function onBankImportEvent(data) {
  const was = BANK.state && BANK.state.status;
  BANK.state = data;
  if (view !== "bankimport" || !$("#bankimport")) return;
  drawBankImport();
  if (was === "running" && data.status !== "running") {
    toast({ ok: data.status === "done", title: data.status === "done" ? "Import done" : "Import stopped",
            message: data.status === "done" ? (data.files || []).join(" and ") + " replaced" : data.error });
  }
}

function uploadBankFile(run, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/bank-import/upload?run=${encodeURIComponent(run)}`);
    xhr.setRequestHeader("X-Varsha", "1");
    xhr.setRequestHeader("X-Filename", file.name);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { BANK.uploading = { name: file.name, done: e.loaded, total: e.total || file.size }; drawBankImport(); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (_e) { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(data.error || xhr.statusText || "upload failed"));
    };
    xhr.onerror = () => reject(new Error("The upload failed (is varsha still running?)"));
    xhr.send(file);
  });
}

async function bankDropped(fileList) {
  const files = [...fileList];
  if (bankBusy()) { toast({ ok: false, title: "Import Banks Data", message: "An import is running: wait for it to finish." }); return; }
  if (!files.length) return;
  const names = files.map((f) => f.name.toLowerCase());
  const wrong = files.filter((f) => !BANK_FILES.includes(f.name.toLowerCase()));
  if (wrong.length || new Set(names).size !== names.length) {
    toast({ ok: false, title: "Not started", message: wrong.length
      ? `Only bankdb.sql and inventory1.sql are accepted (not ${wrong.map((f) => f.name).join(", ")}). Nothing was changed.`
      : "The same file was dropped twice. Nothing was changed." });
    return;
  }
  BANK.again = false;
  try {
    const { run } = await api.post("/api/bank-import/new", {});
    for (const file of files.sort((a, b) => BANK_FILES.indexOf(a.name.toLowerCase()) - BANK_FILES.indexOf(b.name.toLowerCase()))) {
      BANK.uploading = { name: file.name, done: 0, total: file.size };
      drawBankImport();
      await uploadBankFile(run, file);
    }
    BANK.uploading = null;
    await api.post("/api/bank-import/start", { run });
    await renderBankImport();
  } catch (err) {
    BANK.uploading = null;
    drawBankImport();
    toast({ ok: false, title: "Not started", message: err.message });
  }
}

// The whole page takes drops while it is open (a file dropped elsewhere would open in the browser).
for (const type of ["dragenter", "dragover"]) {
  document.addEventListener(type, (event) => {
    if (view !== "bankimport" || !$("#bankimport")) return;
    event.preventDefault();
    $("#bank-drop").classList.add("is-over");
  });
}
document.addEventListener("dragleave", (event) => {
  if (view === "bankimport" && $("#bank-drop") && !event.relatedTarget) $("#bank-drop").classList.remove("is-over");
});
document.addEventListener("drop", (event) => {
  if (view !== "bankimport" || !$("#bankimport")) return;
  event.preventDefault();
  $("#bank-drop").classList.remove("is-over");
  bankDropped(event.dataTransfer ? event.dataTransfer.files : []);
});

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-b]");
  if (!el || !el.closest("#bankimport")) return;
  if (el.dataset.b === "close") {
    window.close();  // allowed: the app window opened with only this page in its history
    setTimeout(() => toast({ ok: true, title: "Import Banks Data", message: "Close this window with its own \u2715 (the browser kept it open)." }), 300);
    return;
  }
  if (el.dataset.b === "again") { BANK.again = true; drawBankImport(); return; }
  if (el.dataset.b === "restore") {
    const db = el.dataset.db;
    confirmDialog(`Restore ${db}?`, `${db} is dropped again and the backup taken before this import is put back.`, `Restore ${db}`, async () => {
      await api.post("/api/bank-import/restore", { database: db });
      toast({ ok: true, title: "Restoring…", message: `${db}: you get a message when it's done.` });
    });
  }
});
