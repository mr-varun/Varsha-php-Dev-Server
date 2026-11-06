// varsha control panel: the Settings page (System menu). Loaded after app.js and uses its helpers.
// The page manages itself: app.js builds the skeleton once, so state refreshes never reset a field.
"use strict";

const SET = { data: null, pick: null };  // pick: the service ids ticked in "Pick" mode

function viewSettingsSkeleton() {
  return `<div id="settings" class="settings-page"></div>`;
}

async function renderSettings() {
  try { SET.data = await api.get("/api/settings"); }
  catch (err) { toast({ ok: false, title: "Settings", message: err.message }); return; }
  const enabled = SET.data.services.filter((s) => s.enabled);
  SET.pick = new Set(enabled.filter((s) => s.autostart).map((s) => s.id));
  drawSettings();
}

function autostartMode() {
  const enabled = SET.data.services.filter((s) => s.enabled);
  if (!SET.pick.size) return "none";
  if (enabled.every((s) => SET.pick.has(s.id))) return "all";
  return "pick";
}

function drawSettings() {
  const box = $("#settings");
  if (!box || !SET.data) return;
  const d = SET.data;
  const st = d.startup;
  const services = d.services.filter((s) => s.enabled);
  const mode = SET.mode || autostartMode();
  box.innerHTML = `
    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Startup</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Start varsha with Windows</b>
          <small class="muted">At login varsha starts quietly in the tray, through your user's own startup entry (no admin rights).</small>
          ${st.enabled && !st.matches ? `<small class="settings-warn">The entry points to another copy of varsha: switch it off and on to use this one.</small>` : ""}</div>
        <button class="switch" role="switch" data-s="startup" aria-checked="${st.enabled}" aria-label="Start varsha with Windows"></button>
      </div>
      <div class="settings-row">
        <div class="grow"><b>Open the control panel when varsha starts</b>
          <small class="muted">Off: varsha starts in the tray only; click the tray icon for the panel. Starting with Windows is always quiet.</small></div>
        <button class="switch" role="switch" data-s="open-panel" aria-checked="${d.open_panel_on_start}" aria-label="Open the control panel when varsha starts"></button>
      </div>
      <div class="settings-row">
        <div class="grow"><b>When you click Quit</b>
          <small class="muted">The tray menu always offers both ways. Shared links and LAN shares close in every case.</small></div>
        <div class="seg" role="group" aria-label="When you click Quit">
          ${[["ask", "Ask"], ["stop", "Stop all"], ["leave", "Leave running"]].map(([k, l]) =>
            `<button class="seg-btn" data-s="quit" data-quit="${k}" aria-pressed="${d.quit_action === k}">${l}</button>`).join("")}</div>
      </div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Services</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Start services with varsha</b>
          <small class="muted">These start by themselves every time varsha starts. Services already running are left alone.</small></div>
        <div class="seg" role="group" aria-label="Auto-start">
          ${[["none", "None"], ["all", "All"], ["pick", "Pick"]].map(([k, l]) =>
            `<button class="seg-btn" data-s="mode" data-mode="${k}" aria-pressed="${mode === k}">${l}</button>`).join("")}</div>
      </div>
      <div class="settings-services${mode === "pick" ? "" : " is-muted"}">
        ${services.map((s) => `<label class="settings-service"><input type="checkbox" data-s="svc" data-id="${esc(s.id)}"
            ${SET.pick.has(s.id) ? "checked" : ""} ${mode === "pick" ? "" : "disabled"}>
            <b>${esc(s.name)}</b><small class="muted">${s.ports.length ? ":" + s.ports.join(" :") : ""}</small></label>`).join("")}
      </div>
      <div class="row settings-actions"><button class="btn btn-yellow" data-s="save-autostart">Save</button>
        <span class="muted" id="settings-autostart-note"></span></div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Control panel</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Port</b>
          <small class="muted">The control panel runs on <code>http://127.0.0.1:${d.running_port}</code>, on this PC only.
            A new port is checked (free, not used by a service) and takes effect when varsha restarts.</small>
          ${d.fallback ? `<small class="settings-warn">Port ${d.panel_port} was busy when varsha started (another varsha?), so it took ${d.running_port}.</small>` : ""}</div>
        <form class="row" data-s="port-form">
          <input class="input mono settings-port" name="port" type="number" min="1024" max="65535" value="${d.panel_port}" aria-label="Panel port">
          <button class="btn btn-yellow" type="submit">Save</button></form>
      </div>
      ${d.restart_needed ? `<div class="banner"><p>Saved port <b>${d.panel_port}</b>; varsha is still on <b>${d.running_port}</b>.
        Restart varsha to move (services keep running, a new tab opens).</p>
        <button class="btn btn-ink" data-s="restart">Restart varsha now</button></div>` : ""}
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">PHP</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Timezone</b>
          <small class="muted"><code>date.timezone</code> in every PHP version's php.ini. Apache reloads to apply it (no downtime).</small>
          ${d.timezone_overridden.length ? `<small class="settings-warn">Your own php.ini setting for <code>date.timezone</code> wins for
            ${esc(d.timezone_overridden.join(", "))} (System → PHP settings).</small>` : ""}</div>
        <form class="row" data-s="tz-form">
          ${timezoneSelect(d)}
          <button class="btn btn-yellow" type="submit">Save</button></form>
      </div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Sites</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Address ending (TLD)</b>
          <small class="muted">Every folder in <code>www\\</code> becomes <code>folder.${esc(d.tld)}</code>. Changing it renames every site,
            its HTTPS certificate, the hosts entries and the DNS route. Your projects' own settings (APP_URL in .env) don't change.</small></div>
        <form class="row" data-s="tld-form">
          <span class="settings-tld-dot">.</span>
          <input class="input mono settings-tld" name="tld" value="${esc(d.tld)}" spellcheck="false" autocomplete="off"
            pattern="[A-Za-z][A-Za-z0-9-]{0,62}" aria-label="Site address ending">
          <button class="btn btn-coral" type="submit">Change…</button></form>
      </div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Logs</h3></div>
      <div class="settings-row">
        <div class="grow"><b>Maximum size of each log</b>
          <small class="muted">A log in <code>logs\\</code> that grows past this is rolled over: the previous part is kept as <code>.log.1</code>
            (one copy), checked every 10 minutes. The logs folder uses <span class="settings-em">${fmtBytes(d.logs.total)}</span> now${d.logs.biggest.length
              ? ` (biggest: ${d.logs.biggest.slice(0, 3).map((f) => `${esc(f.name)} ${fmtBytes(f.size)}`).join(", ")})` : ""}.</small></div>
        <form class="row" data-s="log-form">
          <input class="input mono settings-port" name="mb" type="number" min="1" max="1024" step="any" value="${d.log_max_mb}" aria-label="Maximum log size in MB">
          <span class="muted">MB</span>
          <button class="btn btn-yellow" type="submit">Save</button>
          <button class="btn" type="button" data-s="trim-logs">Trim now</button></form>
      </div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Backups</h3></div>
      <div class="settings-row settings-row-wrap">
        <div class="grow"><b>Backup folder</b>
          <small class="muted">Database backups (one folder per server) go to <code>${esc(d.backup_path)}</code>.
            A folder outside varsha survives a reinstall. Earlier backups stay where they are.</small></div>
        <form class="row settings-backup" data-s="backup-form">
          <input class="input mono grow" name="folder" value="${esc(d.backup_dir)}" placeholder="data\\backups (the default)"
            spellcheck="false" autocomplete="off" aria-label="Backup folder">
          <button class="btn" type="button" data-s="backup-browse">Browse…</button>
          <button class="btn btn-yellow" type="submit">Save</button></form>
      </div>
    </section>

    <section class="card settings-card">
      <div class="card-head"><h3 class="card-title">Tray notifications</h3></div>
      <div class="settings-services">
        ${d.notify_kinds.map((k) => `<label class="settings-service"><input type="checkbox" data-s="notify" data-id="${esc(k.id)}"
            ${d.notify.includes(k.id) ? "checked" : ""}><b>${esc(k.label)}</b></label>`).join("")}
      </div>
      <div class="row settings-actions"><button class="btn btn-yellow" data-s="save-notify">Save</button>
        <button class="btn" data-s="notify-test">Send a test</button>
        <span class="muted" id="settings-notify-note">The panel's own messages always show.</span></div>
    </section>

    <section class="card settings-card settings-wide">
      <div class="card-head"><h3 class="card-title">Export / import</h3></div>
      <div class="settings-row">
        <div class="grow"><b>All of varsha's settings in one .zip</b>
          <small class="muted">Settings, services, PHP, sites, scheduled jobs, remote databases and the PHP error watcher. Saved passwords are
            <span class="settings-em">not</span> included (they only work for this Windows account): enter them again after an import.</small></div>
        <div class="row">
          <a class="btn btn-yellow" href="/api/settings/export" download>Export .zip</a>
          <button class="btn" data-s="import">Import…</button></div>
      </div>
    </section>`;
}

// Every timezone PHP knows, grouped by region (Asia/Kolkata, the default, first). Without PHP's
// list (no PHP build found) it falls back to a text field.
function timezoneSelect(d) {
  const current = d.php_timezone;
  if (!d.timezones.length) {
    return `<input class="input mono settings-tz" name="timezone" value="${esc(current)}" spellcheck="false" autocomplete="off" aria-label="PHP timezone">`;
  }
  const groups = new Map();
  for (const zone of d.timezones) {
    const region = zone.includes("/") ? zone.split("/")[0] : "Other";
    if (!groups.has(region)) groups.set(region, []);
    groups.get(region).push(zone);
  }
  const top = [["Asia/Kolkata", "India: Asia/Kolkata (IST, UTC+5:30)"], ["UTC", "UTC"]];
  const known = new Set(d.timezones);
  const inTop = top.some(([z]) => z === current && known.has(z));  // then only the top entry is selected
  const option = (z, label, pick) => `<option value="${esc(z)}" ${pick && z === current ? "selected" : ""}>${esc(label || z.replace(/_/g, " "))}</option>`;
  return `<select class="select settings-tz" name="timezone" aria-label="PHP timezone">
      <optgroup label="Common">${top.filter(([z]) => known.has(z)).map(([z, l]) => option(z, l, true)).join("")}</optgroup>
      ${[...groups].map(([region, zones]) => `<optgroup label="${esc(region)}">${zones.map((z) => option(z, "", !inTop)).join("")}</optgroup>`).join("")}
    </select>`;
}

// What an import changes, before anything is written.
function importDialog(p) {
  const label = { changed: "changes", new: "new", same: "same as now", kept: "not in the zip: stays" };
  const changes = p.files.filter((f) => f.status === "changed" || f.status === "new").length;
  dialog(`<h2>Import ${esc(p.source)}?</h2>
    <p class="muted">${p.exported ? `Exported ${esc(p.exported)}` : ""}${p.version ? ` from varsha ${esc(p.version)}` : ""}</p>
    <ul class="data-list">${p.files.map((f) => `<li><code>${esc(f.name)}</code>: ${f.status === "changed" || f.status === "new" ? `<b>${label[f.status]}</b>` : esc(label[f.status] || f.status)}</li>`).join("")}</ul>
    <p>The current files are copied to <code>data\\settings-backups</code> first. varsha then restarts to use the new ones
      (services keep running). Saved passwords aren't part of it.</p>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button>
      <button class="btn btn-coral" value="ok" ${changes ? "" : "disabled"}>${changes ? "Import and restart" : "Nothing to change"}</button></div>`,
  async () => {
    toast({ ok: true, title: "Settings", message: (await api.post("/api/settings/import", { token: p.token })).message });
  });
}

// The warning before a TLD change: what moves, what ends, and the admin prompt.
async function tldDialog(value) {
  const p = await api.post("/api/settings/tld-preview", { tld: value.replace(/^\.+/, "") });
  if (p.old === p.new) { toast({ ok: true, title: "Settings", message: `Sites already end in .${p.new}` }); return; }
  const more = p.site_count > p.renamed.length ? `<li class="muted">and ${p.site_count - p.renamed.length} more</li>` : "";
  dialog(`<h2>Change .${esc(p.old)} to .${esc(p.new)}?</h2>
    <p>Every site gets a new address:</p>
    <ul class="data-list">${p.renamed.map((r) => `<li><code>${esc(r)}</code></li>`).join("") || "<li class=\"muted\">(no sites in www\\ yet)</li>"}${more}</ul>
    <ul class="data-list">
      <li>Windows asks for <b>admin rights</b> to rewrite the hosts file${p.dns_route ? ", and once more to move the DNS route" : ""}.</li>
      <li>New HTTPS certificates are made; Apache reloads (no downtime).</li>
      ${p.tunnels ? "<li><b>Shared online links stop</b> (they use the old names).</li>" : ""}
      ${p.lan_shares ? `<li><b>${p.lan_shares} LAN share${p.lan_shares === 1 ? "" : "s"} stop</b>.</li>` : ""}
      ${p.ignored_sites.length ? `<li>Error checking stays off for ${esc(p.ignored_sites.map((s) => s.replace(new RegExp(`\\.${p.old}$`), `.${p.new}`)).join(", "))}.</li>` : ""}
      <li><b>Your projects aren't changed:</b> update their own URLs (APP_URL in .env, WordPress site URL) to <code>.${esc(p.new)}</code>.</li>
    </ul>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button>
      <button class="btn btn-coral" value="ok">Change to .${esc(p.new)}</button></div>`, async () => {
    toast({ ok: true, title: "Settings", message: (await api.post("/api/settings/tld", { tld: p.new })).message });
    await renderSettings();
  });
}

async function saveSettingForm(url, body) {
  const result = await api.post(url, body);
  toast({ ok: true, title: "Settings", message: result.message });
  await renderSettings();
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-s]");
  if (!el || !el.closest("#settings")) return;
  const s = el.dataset.s;
  if (s === "svc" || s === "notify") return;  // tick boxes: the browser toggles them (svc is handled on change)
  event.preventDefault();
  try {
    if (s === "startup") {
      const enable = el.getAttribute("aria-checked") !== "true";
      el.classList.add("busy");
      toast({ ok: true, title: "Settings", message: (await api.post("/api/settings/startup", { enable })).message });
      await renderSettings();
    } else if (s === "mode") {
      SET.mode = el.dataset.mode;
      const enabled = SET.data.services.filter((x) => x.enabled);
      if (SET.mode === "all") SET.pick = new Set(enabled.map((x) => x.id));
      if (SET.mode === "none") SET.pick = new Set();
      drawSettings();
      $("#settings-autostart-note").textContent = "Not saved yet";
    } else if (s === "save-autostart") {
      const result = await api.post("/api/settings/autostart", { services: [...SET.pick] });
      SET.mode = null;
      toast({ ok: true, title: "Settings", message: result.message });
      await renderSettings();
    } else if (s === "restart") {
      el.disabled = true;
      toast({ ok: true, title: "Settings", message: (await api.post("/api/settings/restart", {})).message });
    } else if (s === "open-panel") {
      await saveSettingForm("/api/settings/general", { open_panel_on_start: el.getAttribute("aria-checked") !== "true" });
    } else if (s === "quit") {
      await saveSettingForm("/api/settings/general", { quit_action: el.dataset.quit });
      if (S) S.quit_action = el.dataset.quit;  // the Quit button uses it right away
    } else if (s === "backup-browse") {
      const picked = (await api.post("/api/pick-folder", { title: "Choose the backup folder" })).path;
      if (picked) el.closest("form").elements.folder.value = picked;
    } else if (s === "save-notify") {
      const kinds = [...$("#settings").querySelectorAll('[data-s="notify"]:checked')].map((x) => x.dataset.id);
      await saveSettingForm("/api/settings/notify", { kinds });
    } else if (s === "import") {
      const staged = (await api.post("/api/settings/import-pick", {})).import;
      if (staged) importDialog(staged);
    } else if (s === "trim-logs") {
      await saveSettingForm("/api/settings/trim-logs", {});
    } else if (s === "notify-test") {
      toast({ ok: true, title: "Settings", message: (await api.post("/api/settings/notify-test", {})).message });
    }
  } catch (err) {
    toast({ ok: false, title: "Settings", message: err.message });
  }
});

document.addEventListener("change", async (event) => {
  const el = event.target.closest("[data-s]");
  if (!el || !el.closest("#settings")) return;
  try {
    if (el.dataset.s === "svc") {
      if (el.checked) SET.pick.add(el.dataset.id); else SET.pick.delete(el.dataset.id);
      $("#settings-autostart-note").textContent = "Not saved yet";
    }
  } catch (err) {
    toast({ ok: false, title: "Settings", message: err.message });
    await renderSettings();
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("#settings form[data-s]");
  if (!form) return;
  event.preventDefault();
  const data = new FormData(form);
  try {
    if (form.dataset.s === "port-form") await saveSettingForm("/api/settings/panel-port", { port: data.get("port") });
    else if (form.dataset.s === "tz-form") await saveSettingForm("/api/settings/timezone", { timezone: data.get("timezone") });
    else if (form.dataset.s === "backup-form") await saveSettingForm("/api/settings/backup-dir", { folder: data.get("folder") });
    else if (form.dataset.s === "tld-form") await tldDialog(String(data.get("tld") || ""));
    else if (form.dataset.s === "log-form") await saveSettingForm("/api/settings/log-size", { mb: data.get("mb") });
  } catch (err) {
    toast({ ok: false, title: "Settings", message: err.message });
  }
});
