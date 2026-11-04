/* varsha control panel. Plain JS, no build step, no external code (CSP: script-src 'self').
   State comes from GET /api/state; the server pushes "state" and "toast" events over SSE.
   The home view is a bento grid whose tiles refresh in place, so the live log and the share
   field survive the constant state updates. */
"use strict";

const VIEWS = {
  stack: { title: "Stack", eyebrow: "home" },
  sites: { title: "Sites", eyebrow: "www \u2192 *.test" },
  databases: { title: "Databases", eyebrow: "mysql \u00b7 mariadb \u00b7 remote" },
  share: { title: "Share", eyebrow: "cloudflare tunnel" },
  logs: { title: "Logs", eyebrow: "live output" },
  system: { title: "System", eyebrow: "php \u00b7 https \u00b7 dns \u00b7 folders" },
};
const BUSY = new Set(["starting", "stopping"]);
const OFF = new Set(["missing", "disabled"]);
const LOCK = "\ud83d\udd12";
const FOLDER = "\ud83d\udcc2";

let S = null;                 // last /api/state
let view = (location.hash || "#stack").slice(1);
if (!VIEWS[view]) view = "stack";
let openMenu = null;          // id of the open ⋯ menu
let refreshTimer = null;

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const fmtDate = (t) => new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const svc = (id) => S.services.find((s) => s.id === id);
const remoteDb = (id) => S.remote_dbs.find((r) => r.id === id);
const apacheUp = () => S.services.some((s) => s.type === "apache" && s.state === "running");
const dbs = () => S.services.filter((s) => s.is_database && s.state !== "disabled");
const pmaUrl = (s) => (s && s.pma_server ? `http://localhost/phpmyadmin/index.php?server=${s.pma_server}` : "http://localhost/phpmyadmin/");

const api = {
  async get(path) {
    const r = await fetch(path, { credentials: "same-origin" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  },
  async post(path, body = {}, extra = {}) {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "X-Varsha": "1", "Content-Type": "application/json", ...extra.headers },
      body: extra.raw ?? JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  },
};

async function act(path, body) {
  try { await api.post(path, body); } catch (err) { toast({ ok: false, title: "Not done", message: err.message }); }
}

// ---------------------------------------------------------------- live log tails

class LogTail {
  constructor(name, source) { this.name = name; this.source = source; this.offset = -1; this.follow = true; this.timer = null; }
  get pre() { return document.getElementById(`${this.name}-text`); }
  start(source = this.source) {
    this.source = source;
    this.offset = -1;
    clearInterval(this.timer);
    if (this.pre) this.pre.textContent = "";
    this.poll();
    this.timer = setInterval(() => this.poll(), 1000);
  }
  stop() { clearInterval(this.timer); this.timer = null; }
  async poll() {
    const pre = this.pre;
    if (!pre) { this.stop(); return; }
    try {
      const data = await api.get(`/api/logs/${encodeURIComponent(this.source)}?offset=${this.offset}`);
      const path = document.getElementById(`${this.name}-path`);
      if (path) path.textContent = data.path;
      if (data.missing) { pre.textContent = "No log yet. It appears when the service first starts."; this.offset = 0; return; }
      if (this.offset < 0) pre.textContent = "";
      if (data.text) {
        pre.append(document.createTextNode(data.text));
        if (pre.textContent.length > 400000) pre.textContent = pre.textContent.slice(-300000);
        if (this.follow) pre.scrollTop = pre.scrollHeight;
      }
      this.offset = data.offset;
    } catch { /* reconnecting */ }
  }
}
const logsTail = new LogTail("log", "apache");
const homeTail = new LogTail("home-log", "apache");

function logSourceList() {
  const list = S.services.filter((s) => s.state !== "disabled").map((s) => [s.id, s.name]);
  list.push(["create", "Quick Create"], ["tunnel", "Tunnels"], ["varsha", "varsha itself"]);
  return list;
}

// ---------------------------------------------------------------- toasts

function toast({ ok, title, message = "", service = "" }) {
  const el = document.createElement("div");
  el.className = `toast${ok ? "" : " bad"}`;
  el.innerHTML = `<div class="toast-title">${ok ? "\u2714" : "\u2716"} ${esc(title)}</div>
    ${message ? `<div class="toast-msg">${esc(message)}</div>` : ""}
    <div class="row">${service ? `<button class="btn btn-sm" data-act="view-log" data-id="${esc(service)}">View log</button>` : ""}
    <button class="btn btn-sm btn-ghost" data-act="dismiss">Dismiss</button></div>`;
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), ok ? 6000 : 15000);
}

// ---------------------------------------------------------------- shared pieces

function switchFor(s) {
  const on = s.state === "running";
  const cls = BUSY.has(s.state) ? "busy" : s.state === "error" ? "error" : "";
  const disabled = OFF.has(s.state) || BUSY.has(s.state);
  const label = on ? `Stop ${s.name}` : `Start ${s.name}`;
  return `<button class="switch ${cls}" role="switch" aria-checked="${on}" aria-label="${esc(label)}" title="${esc(label)}"
    data-act="${on ? "stop" : "start"}" data-id="${esc(s.id)}" ${disabled ? "disabled" : ""}></button>`;
}

function serviceMenu(s) {
  const items = [];
  for (const link of s.links) items.push(`<a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(link.label)} \u2197</a>`);
  if (s.links.length) items.push("<hr>");
  items.push(`<button data-act="restart" data-id="${esc(s.id)}" ${s.state === "running" ? "" : "disabled"}>Restart</button>`);
  items.push(`<button data-act="view-log" data-id="${esc(s.id)}">Show full log</button>`);
  if (s.is_database) items.push(`<button data-act="goto" data-view="databases">Database tools \u2192</button>`);
  if (s.id === "dns") items.push(`<button data-act="goto" data-view="system">DNS routing \u2192</button>`);
  if (s.type === "apache") items.push(`<button data-act="goto" data-view="sites">Sites \u2192</button>`);
  const open = openMenu === s.id;
  return `<div class="menu">
      <button class="btn btn-sm" data-act="menu" data-id="${esc(s.id)}" aria-expanded="${open}" aria-label="More for ${esc(s.name)}">\u22ef</button>
      ${open ? `<div class="menu-list" role="menu">${items.join("")}</div>` : ""}
    </div>`;
}

const ports = (s) => s.ports.map((p) => `<span class="port">:${p}</span>`).join("");
const usablePhp = () => S.php.filter((p) => p.cgi);
const defaultPhp = () => S.php.find((p) => p.default);

function phpSelect(site) {
  const def = defaultPhp();
  // site.php may be a prefix ("8.5"): mark the build the server actually resolved it to.
  const options = [`<option value="" ${site.php ? "" : "selected"}>Default (${esc(def ? def.version : "none")})</option>`]
    .concat(usablePhp().map((p) => `<option value="${esc(p.version)}" ${site.php && p.label === site.php_label ? "selected" : ""}>${esc(p.label)}</option>`));
  return `<select class="select" data-site-php="${esc(site.domain)}" aria-label="PHP version for ${esc(site.domain)}">${options.join("")}</select>`;
}
const siteChips = (site) => `<span class="chip ${site.https ? "chip-mint" : ""}">${site.https ? `${LOCK} HTTPS` : "HTTP only"}</span>
  <span class="chip ${site.in_hosts ? "chip-blue" : "chip-yellow"}">${site.in_hosts ? "hosts \u2714" : "not in hosts"}</span>`;

// ---------------------------------------------------------------- shell

function renderShell() {
  document.querySelectorAll(".nav-item").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    b.querySelector(".nav-badge")?.remove();
    const n = { sites: S.sites.length, share: S.tunnels.filter((t) => t.public_url).length }[b.dataset.view];
    if (n) b.insertAdjacentHTML("beforeend", `<span class="nav-badge">${n}</span>`);
  });
  $("#page-head").hidden = view === "stack";
  $("#title").textContent = VIEWS[view].title;
  $("#eyebrow").textContent = VIEWS[view].eyebrow;
  document.title = view === "stack" ? "varsha" : `varsha \u00b7 ${VIEWS[view].title}`;

  const usable = S.services.filter((s) => !OFF.has(s.state));
  const running = usable.filter((s) => s.state === "running").length;
  $("#meter").innerHTML = `<div class="meter" title="${esc(S.busy.join(", "))}">
      <span class="meter-num">${running}<small> / ${usable.length} running</small></span>
      <span class="meter-bar"><span class="meter-fill" id="meter-fill"></span></span>
      ${S.busy.length ? `<span class="meter-busy">\u23f3 ${esc(S.busy[0])}${S.busy.length > 1 ? ` +${S.busy.length - 1}` : ""}</span>` : ""}
    </div>`;
  $("#meter-fill").style.width = `${usable.length ? (running / usable.length) * 100 : 0}%`;
}

// ---------------------------------------------------------------- home: bento

function homeSkeleton() {
  const sources = logSourceList().map(([id, name]) => `<option value="${esc(id)}" ${id === homeTail.source ? "selected" : ""}>${esc(name)}</option>`).join("");
  return `<div class="bento" id="bento">
      <section class="tile t-services" aria-labelledby="h-svc">
        <div class="tile-head"><h2 class="tile-title" id="h-svc">Services</h2><span id="svc-count"></span></div>
        <div class="svc-grid" id="home-services"></div>
      </section>
      <section class="tile t-sites" aria-labelledby="h-sites">
        <div class="tile-head"><h2 class="tile-title" id="h-sites">Sites in www</h2><button class="tile-link" data-act="goto" data-view="sites">All sites \u2192</button></div>
        <div id="home-sites"></div>
      </section>
      <section class="tile t-dbs" aria-labelledby="h-dbs">
        <div class="tile-head"><h2 class="tile-title" id="h-dbs">Databases</h2><span id="home-pma"></span></div>
        <div class="db-rows" id="home-dbs"></div>
      </section>
      <section class="tile t-log" aria-labelledby="h-log">
        <div class="tile-head"><h2 class="tile-title" id="h-log">Live log</h2>
          <div class="row"><label class="visually-hidden" for="home-log-src">Log source</label>
            <select class="select" id="home-log-src">${sources}</select>
            <button class="btn btn-sm" data-act="view-log" data-id="${esc(homeTail.source)}" id="home-log-full">Full log</button></div></div>
        <div class="terminal"><pre id="home-log-text" tabindex="0"></pre></div>
      </section>
      <section class="tile t-share" aria-labelledby="h-share">
        <div class="tile-head"><h2 class="tile-title" id="h-share">Share online</h2><span class="chip chip-ink">cloudflare</span></div>
        <form class="share-form" data-form="share">
          <input class="input" id="share-home" name="target" placeholder="myapp.${esc(S.tld)}  or  localhost:8080" autocomplete="off" spellcheck="false" aria-label="Local address to share">
          <button class="btn btn-blue" type="submit" ${S.system.cloudflared ? "" : "disabled"}>Share \u2197</button>
        </form>
        <div class="mini-tunnels" id="home-tunnels"></div>
      </section>
      <section class="tile t-system" aria-labelledby="h-sys">
        <div class="tile-head"><h2 class="tile-title" id="h-sys">PHP &amp; system</h2><button class="tile-link" data-act="goto" data-view="system">Details \u2192</button></div>
        <div class="sys-rows" id="home-system"></div>
      </section>
    </div>`;
}

function fillHome() {
  const usable = S.services.filter((s) => !OFF.has(s.state));
  const running = usable.filter((s) => s.state === "running").length;
  $("#svc-count").innerHTML = `<span class="chip chip-ink">${running} / ${usable.length} up</span>`;

  // Disabled services ("enabled": false in services.json) stay out of the home grid.
  $("#home-services").innerHTML = S.services.filter((s) => s.state !== "disabled").map((s) => {
    const msg = s.state === "error" ? s.message : s.message || (s.state === "running" && s.pid ? `PID ${s.pid}` : s.label);
    return `<div class="svc state-${s.state}${OFF.has(s.state) ? " is-off" : ""}">
        <div class="svc-top"><span class="led ${s.state}" title="${esc(s.label)}"></span>
          <span class="svc-name">${esc(s.name)}<small>${esc(s.version ? "v" + s.version : s.type)}</small></span>${switchFor(s)}</div>
        <div class="svc-foot"><span class="unit-ports">${ports(s)}</span>${serviceMenu(s)}</div>
        <div class="svc-msg" title="${esc(s.message)}">${esc(msg)}</div>
      </div>`;
  }).join("");

  const sites = S.sites;
  $("#home-sites").innerHTML = `<div class="hero-count"><span class="hero-num">${sites.length}</span><span class="hero-sub">folder${sites.length === 1 ? "" : "s"} \u2192 *.${esc(S.tld)}</span></div>
    ${S.system.hosts_missing.length ? `<div class="row"><span class="chip chip-yellow">${plural(S.system.hosts_missing.length, "name")} not in hosts</span>
      <button class="btn btn-sm btn-ink" data-act="hosts">Fix (admin)</button></div>` : ""}
    ${sites.length ? `<div class="site-rows">${sites.slice(0, 5).map((site) => `<a class="site-row" href="${esc(site.url)}" target="_blank" rel="noopener">
        <strong>${esc(site.domain)}</strong><span class="chip chip-mono" title="${esc(site.php_label)}">php ${esc(site.php_label.split(" ")[0])}</span>${siteChips(site)}</a>`).join("")}</div>
      ${sites.length > 5 ? `<button class="tile-link" data-act="goto" data-view="sites">+ ${sites.length - 5} more</button>` : ""}`
      : `<div class="hero-empty">Make a folder in <code>www\\</code>, e.g. <code>www\\shop</code>, and <b>shop.${esc(S.tld)}</b> appears here.
         <div class="row"><button class="btn btn-sm btn-yellow" data-act="open" data-what="www">Open www folder</button></div></div>`}`;

  const pmaOk = apacheUp();
  $("#home-pma").innerHTML = `<a class="btn btn-sm btn-blue" href="${pmaUrl()}" target="_blank" rel="noopener" ${pmaOk ? "" : 'data-blocked="apache"'}>phpMyAdmin \u2197</a>`;
  $("#home-dbs").innerHTML = dbs().map((s) => {
    const up = s.state === "running";
    const action = up ? `<button class="btn btn-sm btn-yellow" data-act="backup" data-id="${esc(s.id)}">Back up</button>`
      : `<button class="btn btn-sm btn-mint" data-act="start" data-id="${esc(s.id)}" ${BUSY.has(s.state) ? "disabled" : ""}>Start</button>`;
    return `<div class="db-row"><span class="led ${s.state}" title="${esc(s.label)}"></span>
        <strong title="${esc(s.message)}">${esc(s.name)}</strong><span class="port">:${s.ports[0]}</span>${action}</div>`;
  }).join("") + S.remote_dbs.map((r) => `<div class="db-row db-remote"><span class="cloud" title="remote">\u2601</span>
        <strong title="${esc(r.host)}">${esc(r.name)}</strong><span class="chip chip-blue">remote</span>
        <a class="btn btn-sm btn-blue" href="${esc(pmaUrl(r))}" target="_blank" rel="noopener" ${pmaOk ? "" : 'data-blocked="apache"'}>Open \u2197</a></div>`).join("")
    || `<p class="muted">No databases configured.</p>`;

  const tunnels = S.tunnels;
  $("#home-tunnels").innerHTML = tunnels.length ? tunnels.map((t) => `<div class="mini-tunnel">
      ${t.public_url ? `<a href="${esc(t.public_url + (t.path || "/"))}" target="_blank" rel="noopener">${esc(t.public_url.replace("https://", ""))}</a>`
        : `<span class="grow">${t.state === "error" ? "\u2716 " + esc(t.message) : "connecting\u2026"}</span>`}
      <span class="muted">${esc(t.target)}</span>
      ${t.public_url ? `<button class="btn btn-sm" data-act="copy" data-text="${esc(t.public_url)}">Copy</button>` : ""}
      <button class="btn btn-sm btn-coral" data-act="unshare" data-id="${esc(t.id)}">${t.state === "error" ? "Dismiss" : "Stop"}</button></div>`).join("")
    : `<p class="muted">Nothing shared. A link works only while varsha runs; phpMyAdmin and this panel are never exposed.</p>`;

  const sys = S.system;
  const https = !sys.mkcert ? `<span class="chip chip-coral">mkcert missing</span>`
    : sys.ca_trusted ? `<span class="chip chip-mint">trusted</span>` : `<span class="chip chip-yellow">not trusted</span><button class="btn btn-sm btn-ink" data-act="trust">Trust</button>`;
  $("#home-system").innerHTML = `
    <div class="sys-row"><b>PHP</b>${S.php.map((p) => `<span class="php-chip${p.default ? " default" : ""}" title="${esc(p.label)}">${esc(p.version)}</span>`).join("")}</div>
    <div class="sys-row"><b>HTTPS</b>${https}</div>
    <div class="sys-row"><b>Hosts</b><span class="chip ${sys.hosts_in_sync ? "chip-mint" : "chip-yellow"}">${sys.hosts_in_sync ? "in sync" : plural(sys.hosts_missing.length, "name") + " missing"}</span></div>
    <div class="sys-row"><b>DNS</b><span class="chip ${sys.dns_route ? "chip-mint" : ""}">.${esc(S.tld)} route ${sys.dns_route ? "on" : "off"}</span></div>`;

  const select = $("#home-log-src");
  const current = select.value;
  select.innerHTML = logSourceList().map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`).join("");
  select.value = current;
}

// ---------------------------------------------------------------- sites view

function viewSites() {
  const parts = [];
  if (!apacheUp()) {
    parts.push(`<div class="banner banner-blue"><p><b>Apache is stopped.</b> Sites are served by Apache; start it to open them.</p>
      <button class="btn btn-mint" data-act="start" data-id="apache">\u25b6 Start Apache</button></div>`);
  }
  if (S.site_error) parts.push(`<div class="banner banner-coral"><p><b>Last site update failed:</b> ${esc(S.site_error)}</p></div>`);
  const missing = S.system.hosts_missing;
  if (missing.length) {
    parts.push(`<div class="banner"><p><b>${plural(missing.length, "name")} not in the hosts file yet</b>
      (${esc(missing.slice(0, 4).join(", "))}${missing.length > 4 ? ", \u2026" : ""}). Windows asks for admin rights once.</p>
      <button class="btn btn-ink" data-act="hosts">Update hosts file</button></div>`);
  }
  parts.push(`<div class="toolbar"><button class="btn btn-yellow" data-act="create">+ Quick Create</button>
    <button class="btn" data-act="open" data-what="www">${FOLDER} Open www folder</button>
    <button class="btn" data-act="terminal">Terminal</button>
    <button class="btn" data-act="sync-sites">\u21bb Rescan sites</button>
    <span class="muted">Every folder in <code>www\\</code> becomes <b>folder.${esc(S.tld)}</b>; a <code>public\\</code> subfolder is used as the web root.</span></div>`);
  if (!S.sites.length) {
    parts.push(`<div class="empty"><h3>No sites yet</h3>
      <p>Create a folder in <code>www\\</code>, for example <code>www\\shop</code>, and it appears here as
      <b>shop.${esc(S.tld)}</b> within a second, with a virtual host${S.system.mkcert ? " and an HTTPS certificate" : ""}.</p>
      <div class="row"><button class="btn btn-yellow" data-act="create">+ Quick Create</button>
      <button class="btn" data-act="open" data-what="www">Open www folder</button></div></div>`);
  } else {
    parts.push(`<div class="tickets">${S.sites.map((site) => `<article class="ticket">
        <a class="ticket-domain" href="${esc(site.url)}" target="_blank" rel="noopener">${esc(site.domain)}</a>
        <div class="row">${siteChips(site)}</div>
        <div class="ticket-meta">folder&nbsp; ${esc(site.folder)}<br>root&nbsp;&nbsp;&nbsp; ${esc(site.docroot)}</div>
        <label class="php-pick"><span>PHP</span>${phpSelect(site)}</label>
        <div class="ticket-actions">
          <a class="btn btn-sm btn-blue" href="${esc(site.url)}" target="_blank" rel="noopener">Open \u2197</a>
          ${site.https ? `<a class="btn btn-sm" href="${esc(site.url.replace("https://", "http://"))}" target="_blank" rel="noopener">http</a>` : ""}
          <button class="btn btn-sm" data-act="open" data-what="site" data-ref="${esc(site.domain)}">Folder</button>
          <button class="btn btn-sm" data-act="terminal" data-ref="${esc(site.domain)}">Terminal</button>
          <button class="btn btn-sm" data-act="share-site" data-ref="${esc(site.domain)}">Share</button>
          <button class="btn btn-sm btn-coral" data-act="site-delete" data-ref="${esc(site.domain)}" title="Move to the Recycle Bin">Delete</button>
        </div></article>`).join("")}</div>`);
  }
  return parts.join("");
}

// ---------------------------------------------------------------- databases view

function viewDatabases() {
  const list = dbs();
  const note = apacheUp() ? "" : `<div class="banner banner-blue"><p>phpMyAdmin runs inside Apache: start Apache to use it.</p>
    <button class="btn btn-mint" data-act="start" data-id="apache">\u25b6 Start Apache</button></div>`;
  const local = list.length ? `<div class="db-grid">${list.map(dbCard).join("")}</div>`
    : `<div class="empty"><h3>No local databases</h3><p>Add MySQL or MariaDB entries to <code>config\\services.json</code>.</p></div>`;
  return note + local + remoteSection();
}

function dbCard(s) {
  const up = s.state === "running";
  const port = s.ports[0];
  const cmd = `mysql -h 127.0.0.1 -P ${port} -u root${s.has_password ? " -p" : ""}`;
  const pmaOk = up && apacheUp();
  return `<article class="card db-card">
      <div class="db-head">
        <div class="grow"><h3 class="card-title">${esc(s.name)}</h3>
          <div class="row"><span class="chip chip-mono">${esc(s.version || "?")}</span>
          <span class="chip ${up ? "chip-mint" : s.state === "error" ? "chip-coral" : ""}">${esc(s.label)}</span>
          <span class="chip">${s.has_password ? "root \u00b7 password saved" : "root \u00b7 no password"}</span></div></div>
        <span class="db-port">:${port}</span>
        ${switchFor(s)}
      </div>
      ${s.state === "error" ? `<p class="muted">${esc(s.message)}</p>` : ""}
      <div class="conn"><code>${esc(cmd)}</code><button class="btn btn-sm" data-act="copy" data-text="${esc(cmd)}">Copy</button></div>
      <div class="db-actions">
        <a class="btn btn-blue" href="${esc(pmaUrl(s))}" target="_blank" rel="noopener" ${pmaOk ? "" : 'data-blocked="db"'}>phpMyAdmin \u2197</a>
        <button class="btn" data-act="heidisql" data-id="${esc(s.id)}" ${up && S.system.heidisql ? "" : "disabled"}>HeidiSQL</button>
        <button class="btn btn-yellow" data-act="backup" data-id="${esc(s.id)}" ${up ? "" : "disabled"}>Back up now</button>
        <button class="btn" data-act="restore" data-id="${esc(s.id)}" ${up ? "" : "disabled"}>Restore\u2026</button>
        <button class="btn" data-act="password" data-id="${esc(s.id)}" ${s.can_reset_password && !BUSY.has(s.state) ? "" : "disabled"}>Set root password\u2026</button>
        <button class="btn" data-act="open" data-what="backups" data-ref="${esc(s.id)}">Backups folder</button>
      </div>
    </article>`;
}

function remoteSection() {
  const cards = S.remote_dbs.map((r) => `<article class="card db-card remote-card">
      <div class="db-head">
        <div class="grow"><h3 class="card-title">${esc(r.name)}</h3>
          <div class="row"><span class="chip chip-blue">remote</span>
            <span class="chip ${r.ssl ? "chip-mint" : "chip-yellow"}">${r.ssl ? (r.ssl_verify ? `${LOCK} SSL verified` : `${LOCK} SSL`) : "no SSL"}</span>
            <span class="chip">${r.has_password ? (r.remember ? "auto-login" : "password saved") : "asks for password"}</span></div></div>
        <span class="db-port">:${r.port}</span>
      </div>
      <div class="conn"><code>${esc(r.user)}@${esc(r.host)}</code><button class="btn btn-sm" data-act="copy" data-text="${esc(r.host)}">Copy host</button></div>
      <div class="db-actions">
        <a class="btn btn-blue" href="${esc(pmaUrl(r))}" target="_blank" rel="noopener" ${apacheUp() ? "" : 'data-blocked="apache"'}>phpMyAdmin \u2197</a>
        <button class="btn btn-yellow" data-act="remote-test" data-id="${esc(r.id)}">Test connection</button>
        <button class="btn" data-act="remote-edit" data-id="${esc(r.id)}">Edit\u2026</button>
        <button class="btn btn-coral" data-act="remote-delete" data-id="${esc(r.id)}">Remove</button>
      </div>
    </article>`).join("");
  return `<h2 class="section-title">Remote databases</h2>
    <div class="banner banner-blue"><p><b>Manage AWS RDS, Aurora or any MySQL/MariaDB server from your PC.</b>
      It appears in phpMyAdmin's server list; with a saved password phpMyAdmin logs in by itself.
      The RDS security group must allow your IP address.</p>
      <button class="btn btn-ink" data-act="remote-add">+ Add remote database</button></div>
    ${S.remote_dbs.length ? `<div class="db-grid">${cards}</div>` : ""}`;
}

// ---------------------------------------------------------------- share view

function viewShare() {
  const parts = [];
  if (!S.system.cloudflared) {
    parts.push(`<div class="banner banner-coral"><p><b>cloudflared is missing.</b> Put it at <code>bin\\cloudflared\\cloudflared.exe</code>.</p></div>`);
  }
  parts.push(`<div class="card"><div class="card-head"><h3 class="card-title">Put a local site online</h3>
      <span class="chip chip-ink">no account needed</span></div>
      <form class="share-form" data-form="share">
        <input class="input" id="share-target" name="target" placeholder="myapp.${esc(S.tld)}  or  localhost:8080" autocomplete="off" spellcheck="false" aria-label="Local address to share">
        <button class="btn btn-blue" type="submit" ${S.system.cloudflared ? "" : "disabled"}>Share \u2197</button>
      </form>
      <p class="muted">Gives a temporary <b>https://\u2026.trycloudflare.com</b> link. Anyone with it can open the site while varsha runs;
      every link closes when you quit. phpMyAdmin and this panel are never reachable through it.</p></div>`);
  parts.push(`<h2 class="section-title">Shared right now</h2>`);
  if (!S.tunnels.length) {
    parts.push(`<div class="empty"><h3>Nothing is shared</h3><p>Type a site above, or use \u201cShare online\u201d on a site.</p></div>`);
  } else {
    parts.push(S.tunnels.map((t) => `<div class="tunnel">
        <div class="grow"><div class="muted">${esc(t.target)}</div>
          ${t.public_url ? `<a class="tunnel-url" href="${esc(t.public_url + (t.path || "/"))}" target="_blank" rel="noopener">${esc(t.public_url)}</a>`
            : `<div class="tunnel-url">${t.state === "error" ? "\u2716 " + esc(t.message) : "connecting to Cloudflare\u2026"}</div>`}
          ${t.message && t.public_url ? `<div class="muted">${esc(t.message)}</div>` : ""}</div>
        <div class="row">${t.public_url ? `<button class="btn btn-sm" data-act="copy" data-text="${esc(t.public_url)}">Copy</button>` : ""}
          <button class="btn btn-sm btn-coral" data-act="unshare" data-id="${esc(t.id)}">${t.state === "error" ? "Dismiss" : "Stop"}</button></div>
      </div>`).join(""));
  }
  return parts.join("");
}

// ---------------------------------------------------------------- logs view

function logSourceButtons() {
  return logSourceList().map(([id, name]) => `<button class="log-source" data-act="log-source" data-id="${esc(id)}" aria-current="${id === logsTail.source}">${esc(name)}</button>`).join("");
}

function viewLogs() {
  return `<div class="logs">
      <div class="log-sources" id="log-sources">${logSourceButtons()}</div>
      <div class="terminal">
        <div class="terminal-bar"><span class="path" id="log-path">\u2026</span>
          <label class="toggle"><input type="checkbox" id="log-follow" ${logsTail.follow ? "checked" : ""}> follow</label>
          <button class="btn btn-sm" data-act="log-clear">Clear view</button>
          <button class="btn btn-sm btn-yellow" data-act="open" data-what="logs">Folder</button></div>
        <pre id="log-text" tabindex="0"></pre>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- system view

function viewSystem() {
  const sys = S.system;
  const dns = svc("dns");
  const https = !sys.mkcert
    ? `<p><span class="chip chip-coral">mkcert missing</span></p><p>Put <code>mkcert.exe</code> in <code>bin\\mkcert\\</code> to get HTTPS for every site. Until then sites use HTTP.</p>`
    : sys.ca_trusted
      ? `<p><span class="chip chip-mint">trusted</span></p><p>Every site gets a certificate automatically. Firefox may need <code>security.enterprise_roots.enabled</code>.</p>`
      : `<p><span class="chip chip-yellow">not trusted yet</span></p><p>Certificates are created, but browsers warn until the local CA is trusted. Windows shows a confirmation once.</p>
         <button class="btn btn-ink" data-act="trust">Trust local HTTPS</button>`;
  return `<div class="sys-grid">
      <article class="card card-wide"><div class="card-head"><h3 class="card-title">PHP versions</h3><span class="chip">${plural(S.php.length, "build")}</span></div>
        <div class="php-list">${S.php.map((p) => `<div class="php-row${p.default ? " default" : ""}">
          <span class="php-ver">${esc(p.version)}</span>
          <span class="grow"><span class="muted mono">${esc(p.label.replace(p.version + " ", ""))}</span>${p.cgi ? "" : ' <span class="chip chip-coral">no php-cgi</span>'}</span>
          ${p.default ? '<span class="chip chip-ink">default</span>'
            : `<button class="btn btn-sm" data-act="php-default" data-id="${esc(p.folder)}" ${p.cgi ? "" : "disabled"}>Make default</button>`}
          <button class="btn btn-sm btn-yellow" data-act="php-config" data-id="${esc(p.folder)}">Configure…</button></div>`).join("")}</div>
        <p class="muted">The default runs phpMyAdmin and every site without its own choice. Pick a version per site on its ticket in Sites.</p></article>

      <article class="card"><div class="card-head"><h3 class="card-title">HTTPS</h3></div>${https}</article>

      <article class="card"><div class="card-head"><h3 class="card-title">Hosts file</h3>
          <span class="chip ${sys.hosts_in_sync ? "chip-mint" : "chip-yellow"}">${sys.hosts_in_sync ? "in sync" : plural(sys.hosts_missing.length, "name") + " missing"}</span></div>
        <p>varsha writes only between <code># varsha-start</code> and <code># varsha-end</code>; other entries (Laragon's too) are left alone. A backup is kept as <code>hosts.varsha.bak</code>.</p>
        <button class="btn btn-ink" data-act="hosts" ${sys.hosts_in_sync ? "disabled" : ""}>Update hosts file</button></article>

      <article class="card"><div class="card-head"><h3 class="card-title">.${esc(S.tld)} DNS</h3>
          <span class="chip ${sys.dns_route ? "chip-mint" : ""}">${sys.dns_route ? "route on" : "route off"}</span></div>
        <p>Optional. With varsha's DNS running and the route on, <b>any</b> <code>*.${esc(S.tld)}</code> name works (wildcards too), with no hosts entries.
        Only .${esc(S.tld)} lookups go to varsha; everything else uses your normal DNS.</p>
        <div class="row">${dns ? switchFor(dns) : ""}<span class="muted">${dns ? esc(dns.label) : "no DNS service"}</span>
          <span class="grow"></span>
          <button class="btn ${sys.dns_route ? "" : "btn-ink"}" data-act="dns-route" data-enable="${sys.dns_route ? "0" : "1"}">${sys.dns_route ? "Remove route" : "Turn route on"}</button></div></article>

      <article class="card"><div class="card-head"><h3 class="card-title">Folders</h3></div>
        <div class="row">${[["root", "varsha"], ["www", "www"], ["config", "config"], ["logs", "logs"], ["bin", "bin"]]
          .map(([w, l]) => `<button class="btn btn-sm" data-act="open" data-what="${w}">${FOLDER} ${l}</button>`).join("")}</div>
        <dl class="kv"><dt>folder</dt><dd>${esc(S.root)}</dd><dt>version</dt><dd>${esc(S.version)}</dd><dt>domain</dt><dd>*.${esc(S.tld)}</dd></dl>
        <div class="row"><button class="btn btn-sm" data-act="rescan">Rescan bin</button></div></article>
    </div>`;
}

// ---------------------------------------------------------------- render

const RENDER = { sites: viewSites, databases: viewDatabases, share: viewShare, logs: viewLogs, system: viewSystem };

function render() {
  if (!S) return;
  renderShell();
  const host = $("#view");
  if (view === "stack") {
    if (!$("#bento")) { host.innerHTML = homeSkeleton(); homeTail.start(); }
    fillHome();
    return;
  }
  homeTail.stop();
  if (view === "logs") {
    if (!$("#log-text")) { host.innerHTML = viewLogs(); logsTail.start(); }
    else $("#log-sources").innerHTML = logSourceButtons();
    return;
  }
  logsTail.stop();
  const shareValue = $("#share-target")?.value;
  const focused = document.activeElement?.id;
  host.innerHTML = RENDER[view]();
  if (shareValue !== undefined && $("#share-target")) $("#share-target").value = shareValue;
  if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
}

async function refresh() {
  try {
    S = await api.get("/api/state");
    $("#offline").hidden = true;
    render();
  } catch {
    $("#offline").hidden = false;
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 120);
}

function go(next) {
  if (!VIEWS[next]) return;
  view = next;
  openMenu = null;
  history.replaceState(null, "", `#${next}`);
  $("#view").innerHTML = "";
  render();
  window.scrollTo({ top: 0 });
}

// ---------------------------------------------------------------- dialogs

function dialog(html, onSubmit) {
  const dlg = $("#dialog");
  const form = $("#dialog-form");
  form.innerHTML = html;
  form.onsubmit = async (event) => {
    const button = event.submitter;
    if (!button || button.value === "cancel") return;
    event.preventDefault();
    const error = form.querySelector(".form-error");
    try {
      const done = await onSubmit(new FormData(form), button.value);
      if (done !== false) dlg.close();
    } catch (err) {
      if (error) error.textContent = err.message;
    }
  };
  dlg.showModal();
  form.querySelector("input:not([type=radio]):not([type=checkbox]), button[value=ok]")?.focus();
}

function passwordDialog(s) {
  dialog(`<h2>Set root password</h2>
    <p>${esc(s.name)} restarts once with a one-time init file, so this works even if the old password is forgotten.
    Leave both empty for <b>no password</b> (the local default). It's saved encrypted for your Windows user.</p>
    <label>New password<input class="input" type="password" name="p1" autocomplete="new-password"></label>
    <label>Repeat<input class="input" type="password" name="p2" autocomplete="new-password"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-ink" value="ok">Set password</button></div>`,
  async (data) => {
    if (data.get("p1") !== data.get("p2")) throw new Error("The two passwords don't match.");
    await api.post(`/api/services/${s.id}/password`, { password: data.get("p1") });
  });
}

async function restoreDialog(s) {
  let backups = [];
  try { backups = await api.get(`/api/services/${s.id}/backups`); } catch { /* none */ }
  const items = backups.map((b, i) => `<label class="backup-item"><input type="radio" name="file" value="${esc(b.name)}" ${i === 0 ? "checked" : ""}>
      <span class="grow mono">${esc(b.name)}</span><span class="muted">${fmtBytes(b.size)} \u00b7 ${esc(fmtDate(b.modified))}</span></label>`).join("");
  dialog(`<h2>Restore into ${esc(s.name)}</h2>
    <p>Databases in the file <b>replace</b> databases with the same names.</p>
    ${backups.length ? `<div class="backup-list">${items}</div>` : `<p class="muted">No backups yet for this server.</p>`}
    <label>\u2026or upload a .sql file<input class="input" type="file" name="upload" accept=".sql"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok">Restore</button></div>`,
  async (data) => {
    const upload = data.get("upload");
    if (upload && upload.size) {
      await api.post(`/api/services/${s.id}/restore-upload`, null, {
        raw: upload, headers: { "Content-Type": "application/octet-stream", "X-Filename": upload.name } });
    } else if (data.get("file")) {
      await api.post(`/api/services/${s.id}/restore`, { file: data.get("file") });
    } else {
      throw new Error("Pick a backup or choose a .sql file.");
    }
  });
}

function remoteDialog(existing) {
  const r = existing || { name: "", host: "", port: 3306, user: "admin", ssl: true, ssl_verify: false, ssl_ca: "", remember: true, has_password: false };
  dialog(`<h2>${existing ? "Edit" : "Add"} remote database</h2>
    <label>Name<input class="input" name="name" value="${esc(r.name)}" placeholder="Production RDS" required></label>
    <label>Host (endpoint)<input class="input" name="host" value="${esc(r.host)}" placeholder="mydb.abc123xyz.ap-south-1.rds.amazonaws.com" spellcheck="false" required></label>
    <div class="form-row">
      <label>Port<input class="input" name="port" type="number" min="1" max="65535" value="${esc(r.port)}"></label>
      <label class="grow">Username<input class="input" name="user" value="${esc(r.user)}" autocomplete="off" spellcheck="false" required></label>
    </div>
    <label>Password<input class="input" name="password" type="password" autocomplete="new-password"
      placeholder="${r.has_password ? "saved \u2014 leave empty to keep it" : "the database password"}"></label>
    <label class="check"><input type="checkbox" name="remember" ${r.remember ? "checked" : ""}> Remember password and log in to phpMyAdmin automatically
      <small>Saved encrypted for your Windows user; phpMyAdmin reads it from a file only you can open.</small></label>
    ${r.has_password ? `<label class="check"><input type="checkbox" name="forget_password"> Forget the saved password</label>` : ""}
    <label class="check"><input type="checkbox" name="ssl" ${r.ssl ? "checked" : ""}> Use SSL <small>Required by most RDS / Aurora servers.</small></label>
    <label class="check"><input type="checkbox" name="ssl_verify" ${r.ssl_verify ? "checked" : ""}> Verify the server certificate
      <small>Needs the CA file. For RDS: download <code>global-bundle.pem</code> from AWS and give its path.</small></label>
    <label>CA file (optional)<input class="input" name="ssl_ca" value="${esc(r.ssl_ca || "")}" placeholder="C:\\certs\\global-bundle.pem" spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button>
      <button class="btn btn-yellow" value="test">Test connection</button>
      <button class="btn btn-ink" value="ok">${existing ? "Save" : "Add"}</button></div>`,
  async (form, choice) => {
    const data = Object.fromEntries(form.entries());
    for (const key of ["remember", "ssl", "ssl_verify", "forget_password"]) data[key] = form.has(key);
    data.port = Number(data.port || 3306);
    if (choice === "test") {
      await api.post("/api/remote-dbs/test", { ...data, id: existing ? existing.id : "" });
      const error = $("#dialog-form .form-error");
      if (error) error.textContent = "Testing\u2026 the result appears as a message in a moment.";
      return false; // keep the dialog open
    }
    await api.post(existing ? `/api/remote-dbs/${existing.id}` : "/api/remote-dbs", data);
  });
}

async function phpDialog(folder) {
  let d;
  try { d = await api.get(`/api/php/${encodeURIComponent(folder)}`); } catch (err) { toast({ ok: false, title: "PHP", message: err.message }); return; }
  const exts = d.extensions.map((e) => `<label class="ext${e.enabled ? " on" : ""}" title="${e.zend ? "zend_extension" : "extension"}${e.default ? " · on by default" : ""}">
      <input type="checkbox" name="ext:${esc(e.name)}" ${e.enabled ? "checked" : ""}> ${esc(e.name)}${e.zend ? " <small>zend</small>" : ""}</label>`).join("");
  const rows = Object.entries(d.ini).concat([["", ""]]).map(([k, v], i) => iniRow(k, v, i)).join("");
  dialog(`<h2>PHP ${esc(d.label)}</h2>
    <p class="muted mono">${esc(d.ini_path)}</p>
    <h3 class="sub">Extensions <small class="muted">${d.extensions.filter((e) => e.enabled).length} of ${d.extensions.length} on</small></h3>
    <div class="ext-grid">${exts}</div>
    <h3 class="sub">php.ini settings <small class="muted">added at the end of the generated php.ini, so they win</small></h3>
    <div class="ini-rows" id="ini-rows">${rows}</div>
    <div class="row"><button class="btn btn-sm" type="button" data-act="ini-add">+ Add setting</button>
      <span class="muted">e.g. <code>memory_limit = 1G</code>, <code>display_errors = Off</code>, <code>xdebug.mode = debug</code></span></div>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-ink" value="ok">Save &amp; reload Apache</button></div>`,
  async (form) => {
    const extensions = {};
    for (const e of d.extensions) extensions[e.name] = form.has(`ext:${e.name}`);
    const ini = {};
    const keys = form.getAll("ini-key"), values = form.getAll("ini-value");
    keys.forEach((k, i) => { if (k.trim()) ini[k.trim()] = values[i] ?? ""; });
    await api.post(`/api/php/${encodeURIComponent(folder)}`, { extensions, ini });
  });
}

function iniRow(key, value, i) {
  return `<div class="ini-row"><input class="input" name="ini-key" value="${esc(key)}" placeholder="directive" aria-label="Directive ${i + 1}" spellcheck="false">
    <span>=</span><input class="input" name="ini-value" value="${esc(value)}" placeholder="value" aria-label="Value ${i + 1}" spellcheck="false"></div>`;
}

function createDialog() {
  const running = dbs().filter((s) => s.state === "running");
  const def = defaultPhp();
  const kinds = [["blank", "Blank PHP", "A public/index.php to start from."],
    ["laravel", "Laravel", S.composer ? "composer create-project laravel/laravel (needs internet, ~1 min)." : "Needs bin\\composer\\composer.phar."],
    ["wordpress", "WordPress", "Latest release from wordpress.org with wp-config.php filled in."]];
  dialog(`<h2>Quick Create</h2>
    <div class="kinds">${kinds.map(([k, t, d], i) => `<label class="kind"><input type="radio" name="kind" value="${k}" ${i === 0 ? "checked" : ""}
      ${k === "laravel" && !S.composer ? "disabled" : ""}><b>${t}</b><small>${esc(d)}</small></label>`).join("")}</div>
    <label>Folder name<input class="input" name="name" placeholder="shop" pattern="[a-z0-9][a-z0-9-]*" required spellcheck="false" autocomplete="off">
      <small class="muted">Becomes <b>name.${esc(S.tld)}</b> · lowercase letters, digits and "-".</small></label>
    <div class="form-row even">
      <label>PHP<select class="select" name="php"><option value="">Default (${esc(def ? def.version : "?")})</option>
        ${usablePhp().map((p) => `<option value="${esc(p.version)}">${esc(p.label)}</option>`).join("")}</select></label>
      <label class="grow">Database<select class="select" name="db"><option value="">No database</option>
        ${running.map((s) => `<option value="${esc(s.id)}" ${s.type === "mariadb" ? "selected" : ""}>${esc(s.name)} (:${s.ports[0]})</option>`).join("")}</select>
        ${running.length ? "" : '<small class="muted">Start a database to create one for the site.</small>'}</label>
    </div>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-yellow" value="ok">Create site</button></div>`,
  async (form) => {
    await api.post("/api/create", { kind: form.get("kind"), name: form.get("name"), php: form.get("php"), db: form.get("db") });
    toast({ ok: true, title: "Creating…", message: "Follow along in Logs → Quick Create." });
  });
}

function deleteSiteDialog(domain) {
  dialog(`<h2>Delete ${esc(domain)}?</h2>
    <p>The folder moves to the <b>Windows Recycle Bin</b>, so you can restore it from there. Its database is <b>not</b> deleted.</p>
    <label>Type <code>${esc(domain)}</code> to confirm<input class="input" name="confirm" autocomplete="off" spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok">Move to Recycle Bin</button></div>`,
  async (form) => {
    if (form.get("confirm") !== domain) throw new Error(`Type ${domain} exactly to confirm.`);
    await api.post(`/api/sites/${domain}/delete`, { confirm: domain });
  });
}

function confirmDialog(title, text, button, onOk) {
  dialog(`<h2>${esc(title)}</h2><p>${esc(text)}</p>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok">${esc(button)}</button></div>`, onOk);
}

function shareDialog(target) {
  dialog(`<h2>Make ${esc(target)} public?</h2>
    <p>Anyone who gets the link can open it from the internet while the tunnel runs. Don't share sites with real data or unprotected logins.</p>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-blue" value="ok">Share \u2197</button></div>`,
  async () => {
    await api.post("/api/tunnels", { target });
    for (const id of ["share-target", "share-home"]) { const el = document.getElementById(id); if (el) el.value = ""; }
  });
}

function quitDialog() {
  dialog(`<h2>Quit varsha?</h2>
    <p>Shared online links always close. Services you leave running are picked up again the next time varsha starts.</p>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button>
      <button class="btn" value="leave">Quit, leave running</button>
      <button class="btn btn-coral" value="stop">Stop all &amp; quit</button></div>`,
  async (_data, choice) => {
    await api.post("/api/quit", { stop_services: choice === "stop" });
    document.body.innerHTML = `<main class="main"><div class="empty"><h3>varsha is shutting down</h3><p>You can close this tab. Start varsha again to reopen the panel.</p></div></main>`;
  });
}

// ---------------------------------------------------------------- events

document.addEventListener("click", async (event) => {
  const blocked = event.target.closest("[data-blocked]");
  if (blocked) {
    event.preventDefault();
    toast({ ok: false, title: blocked.dataset.blocked === "db" ? "phpMyAdmin needs Apache and this database running" : "phpMyAdmin runs inside Apache: start Apache first" });
    return;
  }
  const el = event.target.closest("[data-act]");
  const nav = event.target.closest("[data-view]");
  if (nav && (!el || el.dataset.act === "goto")) { event.preventDefault(); go(nav.dataset.view); return; }
  if (!el) { if (openMenu && !event.target.closest(".menu")) { openMenu = null; render(); } return; }
  const { act: action, id, what, ref } = el.dataset;
  if (action !== "menu") openMenu = null;
  switch (action) {
    case "start": case "stop": case "restart": await act(`/api/services/${id}/${action}`); break;
    case "start-all": await act("/api/all/start"); break;
    case "stop-all": await act("/api/all/stop"); break;
    case "menu": openMenu = openMenu === id ? null : id; break;
    case "view-log": logsTail.source = id; el.closest(".toast")?.remove(); go("logs"); return;
    case "log-source": logsTail.start(id); $("#log-sources").innerHTML = logSourceButtons(); return;
    case "log-clear": $("#log-text").textContent = ""; return;
    case "dismiss": el.closest(".toast")?.remove(); return;
    case "rescan": await act("/api/rescan"); break;
    case "sync-sites": await act("/api/sites/sync"); break;
    case "hosts": await act("/api/hosts/update"); break;
    case "trust": await act("/api/https/trust"); break;
    case "dns-route": await act("/api/dns/route", { enable: el.dataset.enable === "1" }); break;
    case "open": await act("/api/open", { what, ref: ref || "" }); break;
    case "heidisql": await act(`/api/services/${id}/heidisql`); break;
    case "backup": await act(`/api/services/${id}/backup`); break;
    case "restore": restoreDialog(svc(id)); return;
    case "password": passwordDialog(svc(id)); return;
    case "unshare": await act(`/api/tunnels/${id}/stop`); break;
    case "share-site": go("share"); $("#share-target").value = ref; $("#share-target").focus(); return;
    case "remote-add": remoteDialog(null); return;
    case "remote-edit": remoteDialog(remoteDb(id)); return;
    case "remote-test": await act("/api/remote-dbs/test", { ...remoteDb(id), password: "" }); return;
    case "remote-delete": {
      const r = remoteDb(id);
      confirmDialog(`Remove ${r.name}?`, "It disappears from phpMyAdmin and its saved password is deleted. The remote database itself is not touched.",
        "Remove", async () => { await api.post(`/api/remote-dbs/${r.id}/delete`); });
      return;
    }
    case "copy":
      try { await navigator.clipboard.writeText(el.dataset.text); toast({ ok: true, title: "Copied", message: el.dataset.text }); }
      catch { toast({ ok: false, title: "Couldn't copy", message: el.dataset.text }); }
      return;
    case "create": createDialog(); return;
    case "site-delete": deleteSiteDialog(ref); return;
    case "terminal":
      try { toast({ ok: true, title: "Terminal", message: (await api.post(ref ? `/api/sites/${ref}/terminal` : "/api/terminal")).message }); }
      catch (err) { toast({ ok: false, title: "Terminal", message: err.message }); }
      return;
    case "php-default": await act(`/api/php/${encodeURIComponent(id)}/default`); return;
    case "php-config": phpDialog(id); return;
    case "ini-add": $("#ini-rows").insertAdjacentHTML("beforeend", iniRow("", "", $("#ini-rows").children.length)); return;
    case "quit": quitDialog(); return;
    default: return;
  }
  render();
});

document.addEventListener("submit", (event) => {
  if (event.target.dataset.form !== "share") return;
  event.preventDefault();
  const target = event.target.target.value.trim();
  if (target) shareDialog(target);
});

document.addEventListener("change", (event) => {
  if (event.target.id === "log-follow") logsTail.follow = event.target.checked;
  if (event.target.dataset.sitePhp) act(`/api/sites/${event.target.dataset.sitePhp}/php`, { php: event.target.value });
  if (event.target.name?.startsWith("ext:")) event.target.closest(".ext")?.classList.toggle("on", event.target.checked);
  if (event.target.id === "home-log-src") {
    homeTail.start(event.target.value);
    $("#home-log-full").dataset.id = event.target.value;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, dialog") || event.ctrlKey || event.metaKey || event.altKey) return;
  const index = "123456".indexOf(event.key);
  if (index >= 0) go(Object.keys(VIEWS)[index]);
  if (event.key === "Escape" && openMenu) { openMenu = null; render(); }
});

function connect() {
  const source = new EventSource("/api/events");
  let offlineTimer = null;
  source.addEventListener("hello", () => { clearTimeout(offlineTimer); $("#offline").hidden = true; refresh(); });
  source.addEventListener("state", scheduleRefresh);
  source.addEventListener("toast", (event) => { toast(JSON.parse(event.data)); scheduleRefresh(); });
  source.onerror = () => { clearTimeout(offlineTimer); offlineTimer = setTimeout(() => { $("#offline").hidden = false; }, 2500); };
}

refresh().then(connect);
