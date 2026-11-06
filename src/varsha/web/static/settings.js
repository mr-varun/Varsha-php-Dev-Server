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
    </section>`;
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-s]");
  if (!el || !el.closest("#settings")) return;
  const s = el.dataset.s;
  if (s === "svc") return;  // handled on change
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
  const form = event.target.closest('form[data-s="port-form"]');
  if (!form) return;
  event.preventDefault();
  try {
    const result = await api.post("/api/settings/panel-port", { port: new FormData(form).get("port") });
    toast({ ok: true, title: "Settings", message: result.message });
    await renderSettings();
  } catch (err) {
    toast({ ok: false, title: "Settings", message: err.message });
  }
});
