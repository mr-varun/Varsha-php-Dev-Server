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
          <input class="input mono settings-tz" name="timezone" list="settings-zones" value="${esc(d.php_timezone)}"
            spellcheck="false" autocomplete="off" aria-label="PHP timezone">
          <datalist id="settings-zones">${d.timezones.map((z) => `<option value="${esc(z)}">`).join("")}</datalist>
          <button class="btn btn-yellow" type="submit">Save</button></form>
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
    </section>`;
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
  } catch (err) {
    toast({ ok: false, title: "Settings", message: err.message });
  }
});
