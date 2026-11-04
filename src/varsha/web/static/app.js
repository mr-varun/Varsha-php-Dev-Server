/* varsha control panel. Plain JS, no build step, no external code (CSP: script-src 'self').
   State comes from GET /api/state; the server pushes "state" and "toast" events over SSE.
   The home view is a bento grid whose tiles refresh in place, so the live log and the share
   field survive the constant state updates. */
"use strict";

const VIEWS = {
  stack: { title: "Stack", eyebrow: "home" },
  sites: { title: "Sites", eyebrow: "www \u2192 *.test" },
  databases: { title: "Databases", eyebrow: "mysql \u00b7 mariadb \u00b7 remote" },
  mail: { title: "Mail", eyebrow: "mailpit \u00b7 every email php sends" },
  errors: { title: "PHP errors", eyebrow: "live from php-*.log \u00b7 every site" },
  tasks: { title: "Tasks", eyebrow: "cron \u00b7 scheduled commands" },
  share: { title: "Share", eyebrow: "cloudflare tunnel" },
  logs: { title: "Logs", eyebrow: "live output" },
  system: { title: "System", eyebrow: "php \u00b7 https \u00b7 dns \u00b7 folders" },
  doctor: { title: "Doctor", eyebrow: "check everything \u00b7 one click" },
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
    if (!r.ok) {
      const err = new Error((await r.json().catch(() => ({}))).error || r.statusText);
      err.status = r.status;
      throw err;
    }
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
  for (const t of S.tasks || []) list.push([`task-${t.id}`, `Task: ${t.name}`]);
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
const siteChips = (site) => `${site.link ? `<span class="chip" title="links to ${esc(site.link)}">link</span>` : ""}<span class="chip ${site.https ? "chip-mint" : ""}">${site.https ? `${LOCK} HTTPS` : "HTTP only"}</span>
  <span class="chip ${site.in_hosts ? "chip-blue" : "chip-yellow"}">${site.in_hosts ? "hosts \u2714" : "not in hosts"}</span>${lanShare(site.domain)
    ? `<span class="chip chip-ink" title="${esc(lanShare(site.domain).url || "")}">LAN :${lanShare(site.domain).port}</span>` : ""}`;
const lanShare = (domain) => ((S.lan && S.lan.shares) || []).find((s) => s.domain === domain);

// ---------------------------------------------------------------- shell

function renderShell() {
  document.querySelectorAll(".nav-item").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    b.querySelector(".nav-badge")?.remove();
    const n = { sites: S.sites.length, share: S.tunnels.filter((t) => t.public_url).length + ((S.lan && S.lan.shares) || []).length, mail: S.mail.unread,
                tasks: (S.tasks || []).filter((t) => t.enabled && t.last_ok === false).length,
                errors: S.php_errors ? S.php_errors.unseen_errors + S.php_errors.unseen_warnings : 0 }[b.dataset.view];
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
      <section class="tile t-mail" aria-labelledby="h-mail">
        <div class="tile-head"><h2 class="tile-title" id="h-mail">Mail</h2>
          <div class="row"><span id="home-mail-count"></span><button class="tile-link" data-act="goto" data-view="mail">Inbox \u2192</button></div></div>
        <div id="home-mail"></div>
      </section>
    </div>`;
}

// Home "Sites in www" tile: 4 sites per page with ‹ › arrows (no scroll bar). The page survives
// the constant state refreshes and is clamped when sites come or go.
const HOME_SITES_PER_PAGE = 4;
let homeSitesPageNo = 0;
const homeSitesPages = (count) => Math.max(1, Math.ceil(count / HOME_SITES_PER_PAGE));

function homeSitesPage(sites) {
  homeSitesPageNo = Math.min(homeSitesPageNo, homeSitesPages(sites.length) - 1);
  const start = homeSitesPageNo * HOME_SITES_PER_PAGE;
  return sites.slice(start, start + HOME_SITES_PER_PAGE);
}

function sitesPager(count) {
  const pages = homeSitesPages(count);
  if (pages < 2) return "";
  const first = homeSitesPageNo * HOME_SITES_PER_PAGE + 1, last = Math.min(count, first + HOME_SITES_PER_PAGE - 1);
  return `<div class="sites-pager">
      <button class="pager-btn" data-act="sites-page" data-ref="-1" ${homeSitesPageNo === 0 ? "disabled" : ""} aria-label="Previous sites">‹</button>
      <span class="pager-info">${first}–${last} of ${count}</span>
      <button class="pager-btn" data-act="sites-page" data-ref="1" ${homeSitesPageNo >= pages - 1 ? "disabled" : ""} aria-label="Next sites">›</button>
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
    ${sites.length ? `<div class="site-rows">${homeSitesPage(sites).map((site) => `<a class="site-row" href="${esc(site.url)}" target="_blank" rel="noopener">
        <strong>${esc(site.domain)}</strong><span class="chip chip-mono" title="${esc(site.php_label)}">php ${esc(site.php_label.split(" ")[0])}</span>${siteChips(site)}</a>`).join("")}</div>
      ${sitesPager(sites.length)}`
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
    <div class="sys-row"><b>DNS</b><span class="chip ${sys.dns_route ? "chip-mint" : ""}">.${esc(S.tld)} route ${sys.dns_route ? "on" : "off"}</span></div>
    <div class="sys-row"><b>Errors</b>${homeErrorsChip()}</div>`;

  const select = $("#home-log-src");
  const current = select.value;
  select.innerHTML = logSourceList().map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`).join("");
  select.value = current;
  fillHomeMail();
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
    <button class="btn" data-act="git-refresh" title="Read every site's git status again (read-only, nothing is fetched)">\u2387 Git status</button>
    <span class="muted">Every folder in <code>www\\</code> becomes <b>folder.${esc(S.tld)}</b>; a <code>public\\</code> subfolder is used as the web root.</span></div>`);
  if (!S.sites.length) {
    parts.push(`<div class="empty"><h3>No sites yet</h3>
      <p>Create a folder in <code>www\\</code>, for example <code>www\\shop</code>, and it appears here as
      <b>shop.${esc(S.tld)}</b> within a second, with a virtual host${S.system.mkcert ? " and an HTTPS certificate" : ""}.</p>
      <div class="row"><button class="btn btn-yellow" data-act="create">+ Quick Create</button>
      <button class="btn" data-act="open" data-what="www">Open www folder</button></div></div>`);
  } else {
    parts.push(`<div class="tickets tickets-3">${S.sites.map((site) => `<article class="ticket">
        <div class="ticket-head"><a class="ticket-domain" href="${esc(site.url)}" target="_blank" rel="noopener">${esc(site.domain)}</a>
          <div class="row">${siteChips(site)}</div></div>
        <div class="ticket-mid">
          <div class="ticket-meta" title="folder ${esc(site.folder)}\nroot ${esc(site.docroot)}">${esc(site.folder)}${site.docroot !== site.folder
            ? `<br><span class="muted">root</span> ${esc(site.docroot)}` : ""}</div>
          <label class="php-pick"><span>PHP</span>${phpSelect(site)}</label>
        </div>
        <div class="ticket-git" data-git="${esc(site.domain)}">${gitLine(GIT.sites[site.domain])}</div>
        <div class="ticket-actions">
          <a class="btn btn-sm btn-blue" href="${esc(site.url)}" target="_blank" rel="noopener">Open \u2197</a>
          ${site.https ? `<a class="btn btn-sm" href="${esc(site.url.replace("https://", "http://"))}" target="_blank" rel="noopener">http</a>` : ""}
          <button class="btn btn-sm" data-act="open" data-what="site" data-ref="${esc(site.domain)}">Folder</button>
          <button class="btn btn-sm" data-act="terminal" data-ref="${esc(site.domain)}">Terminal</button>
          <button class="btn btn-sm" data-act="share-site" data-ref="${esc(site.domain)}" title="A public link through Cloudflare">Share</button>
          ${lanShare(site.domain)
            ? `<button class="btn btn-sm btn-ink" data-act="lan-stop" data-ref="${esc(site.domain)}" title="Stop sharing on the LAN">Stop LAN</button>`
            : `<button class="btn btn-sm" data-act="lan-share" data-ref="${esc(site.domain)}" title="Let colleagues on the same Wi-Fi open it">LAN</button>`}
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
  const local = list.length ? `<div class="db-grid db-row-3">${list.map(dbCard).join("")}</div>`
    : `<div class="empty"><h3>No local databases</h3><p>Add MySQL or MariaDB entries to <code>config\\services.json</code>.</p></div>`;
  const tools = `<div class="toolbar"><p class="muted">Copy databases in from Laragon, WAMP, XAMPP or another server: the source is only read, and every table is checked afterwards.</p>
      <button class="btn btn-ink" data-act="import-wizard">\u21e9 Import databases\u2026</button></div>`;
  return note + tools + local + remoteSection();
}

// ---------------------------------------------------------------- import wizard

async function importWizard() {
  let stage = "source", session = null, poll = null;
  const form = () => $("#dialog-form");
  const cancelBtn = (label = "Cancel") => `<button class="btn" value="cancel" formnovalidate>${label}</button>`;
  const sourceHtml = `<h2>Import databases</h2>
    <p>Bring databases into varsha from another MySQL or MariaDB. <b>The source is never changed</b>: a data folder is copied first
      and read from the copy.</p>
    <div class="kinds kinds-2">
      <label class="kind"><input type="radio" name="kind" value="folder" checked><b>A data folder</b><small>Laragon, WAMP, XAMPP or an old install. Its server can be stopped.</small></label>
      <label class="kind"><input type="radio" name="kind" value="server"><b>A running server</b><small>Any MySQL/MariaDB you can log in to, local or remote.</small></label>
    </div>
    <div id="imp-folder">
      <label>Data folder<input class="input mono" name="path" spellcheck="false" placeholder="e.g. D:\\backups\\mysql\\data (the folder with ibdata1 and mysql\\ inside)"></label>
      <div class="imp-picks" id="imp-picks"><span class="muted">Looking for Laragon / WAMP / XAMPP folders\u2026</span></div>
    </div>
    <div id="imp-server" hidden>
      <div class="form-row even"><label>Host<input class="input" name="host" value="127.0.0.1" spellcheck="false"></label>
        <label>Port<input class="input" type="number" name="port" value="3306" min="1" max="65535"></label></div>
      <div class="form-row even"><label>User<input class="input" name="user" value="root" spellcheck="false"></label>
        <label>Password<input class="input" type="password" name="password" autocomplete="off"></label></div>
    </div>
    <div class="form-error"></div>
    <div class="dialog-actions">${cancelBtn()}<button class="btn btn-ink" value="ok">Look inside \u2192</button></div>`;

  function pickHtml(s) {
    const running = dbs().filter((d) => d.state === "running");
    const fam = { mariadb: "mariadb", mysql8: "mysql", mysql5: "mysql" }[s.family];
    const preferred = running.find((d) => d.type === fam && (s.family !== "mysql5" || (d.version || "").startsWith("5")))
      || running.find((d) => d.type === fam) || running[0];
    const targets = dbs().map((d) => `<option value="${esc(d.id)}" ${d.state === "running" ? "" : "disabled"} ${preferred && d.id === preferred.id ? "selected" : ""}>
      ${esc(d.name)} :${d.ports[0]}${d.state === "running" ? "" : " (stopped: start it first)"}</option>`).join("");
    const rows = s.databases.map((d) => `<label class="imp-db"><input type="checkbox" name="db" value="${esc(d.name)}" checked>
      <span class="mono grow">${esc(d.name)}</span><span class="muted">${d.tables} tables</span><span class="muted">${fmtBytes(d.size)}</span></label>`).join("");
    return `<h2>Import from ${esc(s.kind === "folder" ? "folder" : s.label)}</h2>
      <p class="row"><span class="chip chip-blue">${esc(s.family_label)}</span>${s.version ? `<span class="chip chip-mono">${esc(s.version)}</span>` : ""}
        <span class="muted mono imp-label">${esc(s.label)}</span></p>
      ${s.databases.length ? `<div class="row"><b class="grow">${plural(s.databases.length, "database")} found</b>
          <button class="btn btn-sm" type="button" data-imp="all">All</button><button class="btn btn-sm" type="button" data-imp="none">None</button></div>
        <div class="imp-dbs">${rows}</div>` : `<p class="muted">No databases in it (only system schemas).</p>`}
      <label>Into<select class="input" name="target">${targets}</select></label>
      ${preferred ? "" : `<p class="form-hint">No varsha database is running. Close this, start MariaDB or MySQL on the Databases page, then import: the databases go into a running server.</p>`}
      <div class="kinds kinds-2">
        <label class="kind"><input type="radio" name="mode" value="skip" checked><b>Skip existing</b><small>Databases that already exist in the target are left alone.</small></label>
        <label class="kind"><input type="radio" name="mode" value="replace"><b>Replace existing</b><small>Same-named databases in the target are dropped and imported fresh.</small></label>
      </div>
      <div class="form-error"></div>
      <div class="dialog-actions">${cancelBtn()}<button class="btn btn-ink" value="ok" ${preferred && s.databases.length ? "" : "disabled"}>\u21e9 Import</button></div>`;
  }

  function progressHtml(s) {
    const icon = { ok: "\u2714", skipped: "\u21b7", failed: "\u2716", mismatch: "\u26a0" };
    const report = s.report.length ? `<table class="imp-report"><tbody>${s.report.map((r) => `<tr class="imp-${esc(r.status)}">
        <td>${icon[r.status] || "\u23f3"}</td><td class="mono">${esc(r.name)}</td><td>${esc(r.detail)}</td></tr>`).join("")}</tbody></table>` : "";
    const done = s.state === "done" || s.state === "failed";
    return `<h2>${done ? (s.state === "done" ? "Import finished" : "Import failed") : "Importing\u2026"}</h2>
      ${s.error ? `<div class="banner banner-coral"><p>${esc(s.error)}</p></div>` : ""}
      ${report}
      <div class="terminal imp-log"><pre>${esc(s.log.join("\n"))}</pre></div>
      ${done && s.dump_dir ? `<p class="muted">The dumps are kept as a safety copy in <code>${esc(s.dump_dir)}</code>.</p>` : ""}
      <div class="dialog-actions">${done ? cancelBtn("Close") : `<span class="muted">This keeps running if you close the window.</span>${cancelBtn("Hide")}`}</div>`;
  }

  async function follow() {
    clearTimeout(poll);
    if (!$("#dialog").open || stage !== "progress") return;
    try { session = await api.get(`/api/import/${session.id}`); } catch { return; }
    form().innerHTML = progressHtml(session);
    const pre = form().querySelector(".imp-log pre");
    if (pre) pre.scrollTop = pre.scrollHeight;
    if (session.state === "running" || session.state === "ready") poll = setTimeout(follow, 1000);
    else refresh();
  }

  dialog(sourceHtml, async (data) => {
    if (stage === "source") {
      const kind = data.get("kind");
      const body = kind === "folder" ? { kind, path: (data.get("path") || "").trim() }
        : { kind, host: data.get("host"), port: parseInt(data.get("port"), 10) || 3306, user: data.get("user"), password: data.get("password") };
      if (kind === "folder" && !body.path) throw new Error("Type or pick the data folder.");
      session = await api.post("/api/import/inspect", body);
      stage = "pick";
      form().innerHTML = pickHtml(session);
      return false;
    }
    if (stage === "pick") {
      const names = data.getAll("db");
      if (!names.length) throw new Error("Tick at least one database.");
      await api.post(`/api/import/${session.id}/run`, { databases: names, target: data.get("target"), mode: data.get("mode") });
      stage = "progress";
      form().innerHTML = progressHtml({ ...session, state: "running", log: ["Starting\u2026"], report: [], error: "" });
      follow();
      return false;
    }
    return false;
  });

  // The dialog's <form> is shared by every dialog: these listeners remove themselves on close.
  const box = form();
  const onChange = (event) => {
    if (event.target.name === "kind" && $("#imp-folder")) {
      $("#imp-folder").hidden = event.target.value !== "folder";
      $("#imp-server").hidden = event.target.value !== "server";
    }
  };
  const onClick = (event) => {
    const pick = event.target.closest("[data-pick]");
    if (pick) { box.querySelector("[name=path]").value = pick.dataset.pick; return; }
    const all = event.target.closest("[data-imp]");
    if (all) box.querySelectorAll("input[name=db]").forEach((c) => { c.checked = all.dataset.imp === "all"; });
  };
  box.addEventListener("change", onChange);
  box.addEventListener("click", onClick);
  $("#dialog").addEventListener("close", () => {
    box.removeEventListener("change", onChange);
    box.removeEventListener("click", onClick);
    clearTimeout(poll);
    stage = "closed";
  }, { once: true });
  try {
    const picks = await api.get("/api/import/suggest");
    const el = $("#imp-picks");
    if (el) el.innerHTML = picks.length ? `<span class="muted">Quick picks:</span>${picks.map((p) => `<button class="chip imp-pick" type="button" data-pick="${esc(p.path)}"
        title="${esc(p.family)} ${esc(p.version)}${p.recent ? " \u00b7 used before" : ""}">${p.recent ? "\u21ba " : ""}${esc(p.path)} <span class="muted">\u00b7 ${p.databases} db</span></button>`).join("")}`
      : `<span class="muted">No Laragon / WAMP / XAMPP data folders found in the usual places. Type the path; folders you import from are remembered here.</span>`;
  } catch { /* the field still works */ }
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
      ${autoBackupStrip(s)}
    </article>`;
}

const fmtWhen = (t) => {
  if (!t) return "";
  const d = new Date(t * 1000), days = Math.floor((d - new Date().setHours(0, 0, 0, 0)) / 86400000);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return days === 0 ? `today ${time}` : days === 1 ? `tomorrow ${time}` : days === -1 ? `yesterday ${time}`
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + ` ${time}`;
};

function autoBackupStrip(s) {
  const a = s.autobackup;
  if (!a) return "";
  let status;
  if (a.running) status = "⏳ backing up now…";
  else if (!a.enabled) status = "Off. Turn it on so you never lose work.";
  else if (a.waiting) status = `Due now: runs as soon as ${esc(s.name)} is running`;
  else status = `Next: ${esc(fmtWhen(a.next_due))}`;
  const last = a.last_run ? `<span class="${a.last_ok ? "" : "ab-bad"}">Last: ${a.last_ok ? "✔" : "✖"} ${esc(fmtWhen(a.last_run))} · ${esc(a.message)}</span>` : "";
  return `<div class="auto-backup${a.enabled ? " on" : ""}${a.last_ok === false ? " failed" : ""}">
      <div class="ab-head"><b>⏱ Auto backup</b><span class="chip ${a.enabled ? "chip-mint" : ""}">${esc(a.describe)}</span></div>
      <div class="ab-status">${status}</div>${last ? `<div class="ab-last">${last}</div>` : ""}
      <div class="row">
        <button class="btn btn-sm btn-ink" data-act="autobackup" data-id="${esc(s.id)}">Schedule…</button>
        ${a.enabled ? `<button class="btn btn-sm" data-act="autobackup-run" data-id="${esc(s.id)}" ${s.state === "running" && !a.running ? "" : "disabled"}>Run now</button>` : ""}
      </div>
    </div>`;
}

async function autoBackupDialog(s) {
  const a = s.autobackup;
  const opts = (pairs, current) => pairs.map(([v, l]) => `<option value="${v}" ${String(v) === String(current) ? "selected" : ""}>${l}</option>`).join("");
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  dialog(`<h2>Auto backup · ${esc(s.name)}</h2>
    <p>Backups go to <code>${esc(S.backup_root)}\\${esc(s.id)}</code>. Only automatic backups are cleaned up; your manual ones are kept.
      A backup missed while varsha or the database was off runs as soon as the database is running again.</p>
    <label class="check"><input type="checkbox" name="enabled" ${a.enabled ? "checked" : ""}> Back up automatically</label>
    <div class="form-row even">
      <label>How often<select class="input" name="every">${opts([["hourly", "Every hour"], ["6h", "Every 6 hours"], ["12h", "Every 12 hours"], ["daily", "Every day"], ["weekly", "Every week"]], a.every)}</select></label>
      <label>At (daily / weekly)<input class="input" type="time" name="at" value="${esc(a.at)}"></label>
      <label>Day (weekly)<select class="input" name="day">${opts(days.map((d, i) => [i, d]), a.day)}</select></label>
    </div>
    <label>Keep the newest automatic backups (older ones are deleted)<input class="input" type="number" name="keep" min="1" max="365" value="${esc(a.keep)}"></label>
    <fieldset class="ab-dbs"><legend>Databases</legend><div id="ab-dbs"><p class="muted">Loading…</p></div></fieldset>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-ink" value="ok">Save</button></div>`,
  async (data) => {
    const keep = parseInt(data.get("keep"), 10);
    if (!(keep >= 1 && keep <= 365)) throw new Error("Keep must be a number from 1 to 365.");
    const chosen = data.get("scope") === "some" ? data.getAll("db") : [];
    if (data.get("scope") === "some" && !chosen.length) throw new Error("Tick at least one database, or choose all.");
    await api.post(`/api/services/${s.id}/autobackup`, {
      enabled: data.has("enabled"), every: data.get("every"), at: data.get("at") || "13:00",
      day: parseInt(data.get("day"), 10), keep, databases: chosen });
    toast({ ok: true, title: `Auto backup of ${s.name}`, message: data.has("enabled") ? "Schedule saved" : "Turned off" });
  });
  const box = $("#ab-dbs");
  let names = [];
  if (s.state === "running") {
    try { names = await api.get(`/api/services/${s.id}/databases`); } catch { /* shown below */ }
  }
  names = [...new Set([...names, ...a.databases])].sort();
  const some = a.databases.length > 0;
  box.innerHTML = `<label class="check"><input type="radio" name="scope" value="all" ${some ? "" : "checked"}> All databases (including new ones)</label>
    <label class="check"><input type="radio" name="scope" value="some" ${some ? "checked" : ""} ${names.length ? "" : "disabled"}> Only these:</label>
    <div class="ab-db-list">${names.map((n) => `<label class="check"><input type="checkbox" name="db" value="${esc(n)}" ${a.databases.includes(n) ? "checked" : ""}> ${esc(n)}</label>`).join("")
      || `<p class="muted">Start ${esc(s.name)} to pick single databases.</p>`}</div>`;
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
    ${S.remote_dbs.length ? `<div class="db-grid db-row-3">${cards}</div>` : ""}`;
}

// ---------------------------------------------------------------- share view

function lanCard() {
  const lan = S.lan || { shares: [] };
  const notes = [];
  if (!lan.address) {
    notes.push(`<div class="banner banner-coral"><p><b>No network.</b> This PC has no LAN address: connect to the Wi-Fi first.</p></div>`);
  } else if (lan.category === "Public") {
    notes.push(`<div class="banner banner-yellow"><p><b>Windows treats \u201c${esc(lan.interface)}\u201d as a Public network</b>, so the firewall blocks colleagues.
      Make it Private: Settings \u2192 Network &amp; internet \u2192 ${esc(lan.interface)} \u2192 your network \u2192 Network profile type \u2192 <b>Private</b>.</p></div>`);
  }
  if (!S.system.mkcert) notes.push(`<div class="banner banner-coral"><p>LAN sharing uses HTTPS: put mkcert at <code>bin\\mkcert\\mkcert.exe</code>.</p></div>`);
  if (!lan.apache_running) {
    notes.push(`<div class="banner banner-blue"><p>LAN sharing runs inside Apache: start Apache first.</p>
      <button class="btn btn-mint" data-act="start" data-id="apache">\u25b6 Start Apache</button></div>`);
  }
  const shared = new Set(lan.shares.map((s) => s.domain));
  const free = S.sites.filter((s) => !shared.has(s.domain));
  const ready = lan.address && lan.apache_running && S.system.mkcert;
  const rows = lan.shares.map((s) => `<div class="tunnel">
      <div class="grow"><div class="muted">${esc(s.domain)}</div>
        <a class="tunnel-url" href="${esc(s.url || "#")}" target="_blank" rel="noopener">${esc(s.url || "waiting for a network\u2026")}</a></div>
      <div class="row">${s.url ? `<button class="btn btn-sm" data-act="copy" data-text="${esc(s.url)}">Copy</button>
        <button class="btn btn-sm" data-act="lan-qr" data-ref="${esc(s.domain)}" title="Show a QR code to scan with a phone">QR</button>` : ""}
        <button class="btn btn-sm btn-coral" data-act="lan-stop" data-ref="${esc(s.domain)}">Stop</button></div></div>`).join("");
  const firewall = lan.firewall
    ? `<span class="muted">\u2714 Firewall allows ports ${esc(lan.port_range)} on private networks.</span>
       <button class="btn btn-sm" data-act="lan-firewall-remove" title="Remove varsha's firewall rule (UAC prompt)">Remove rule</button>`
    : `<span class="muted">The first share asks Windows (one UAC prompt) to allow ports ${esc(lan.port_range)} for varsha's Apache, on private networks only.</span>`;
  return `<div class="card"><div class="card-head"><h3 class="card-title">On your Wi-Fi (LAN)</h3>
      <span class="chip chip-ink">${lan.address ? `this PC: ${esc(lan.address)}` : "offline"}</span></div>
    ${notes.join("")}
    <div class="share-form">
      <select class="input" id="lan-site" aria-label="Site to share on the LAN" ${free.length ? "" : "disabled"}>
        ${free.length ? free.map((s) => `<option value="${esc(s.domain)}">${esc(s.domain)}</option>`).join("") : "<option>every site is shared</option>"}</select>
      <button class="btn btn-blue" data-act="lan-share" ${ready && free.length ? "" : "disabled"}>Share on LAN</button>
    </div>
    <p class="muted">Colleagues on the same network open <b>https://${esc(lan.address || "this-pc")}:port/</b>. The site runs exactly as on
      <b>.${esc(S.tld)}</b>: it still sees its own name, and its links are rewritten to the LAN address. Their browser warns about the
      certificate once (Advanced \u2192 Continue). Only the shared site is reachable: phpMyAdmin, this panel and the databases stay on this PC.
      Every LAN share ends when you quit varsha.</p>
    ${rows ? `<div class="lan-list">${rows}</div>` : ""}
    <div class="row lan-firewall">${firewall}</div></div>`;
}

// A QR code as SVG from its module rows ("0"/"1"), with the 4-module quiet zone scanners need.
function qrSvg(rows) {
  const size = rows.length + 8;
  let d = "";
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) if (row[x] === "1") d += `M${x + 4} ${y + 4}h1v1h-1z`;
  });
  return `<svg class="qr-svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges" role="img" aria-label="QR code">
    <rect width="${size}" height="${size}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}

async function lanQrDialog(domain) {
  let qr;
  try { qr = await api.get(`/api/lan/${encodeURIComponent(domain)}/qr`); }
  catch (err) { toast({ ok: false, title: "QR code", message: err.message }); return; }
  dialog(`<h2>${esc(domain)} on the LAN</h2>
    <div class="qr-box">${qrSvg(qr.rows)}</div>
    <p class="qr-url mono">${esc(qr.url)}</p>
    <p class="muted">Scan it with a phone camera on the same Wi-Fi. The browser warns about the certificate once: Advanced \u2192 Continue.</p>
    <div class="dialog-actions"><button class="btn" data-act="copy" data-text="${esc(qr.url)}" type="button">Copy link</button>
      <button class="btn btn-ink" value="cancel">Close</button></div>`, async () => true);
}

function viewShare() {
  const parts = [lanCard()];
  if (!S.system.cloudflared) {
    parts.push(`<div class="banner banner-coral"><p><b>cloudflared is missing.</b> Put it at <code>bin\\cloudflared\\cloudflared.exe</code>.</p></div>`);
  }
  parts.push(`<div class="card"><div class="card-head"><h3 class="card-title">Put a local site online (internet)</h3>
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

// ---------------------------------------------------------------- mail view
// The inbox lives in MAIL, not in /api/state: the view is built once and updated in place, so the
// search box, the scroll position and the open email survive state refreshes and new-mail events.

const MAIL = { list: [], total: 0, count: 0, q: "", selected: null, detail: null, tab: "html",
               remote: false, headers: null, error: "", running: null, home: [], homeKey: "" };
const MAIL_PAGE = 50;

const mailState = () => S.mail || { available: false, running: false, unread: 0, total: 0 };
const mailSvc = () => (S.mail && S.mail.service ? svc(S.mail.service) : null);
const who = (a) => (a && (a.name || a.address)) || "(unknown sender)";
const whoFull = (a) => (a.name ? `${a.name} <${a.address}>` : a.address);
const mailUrl = (id, rest = "") => `/api/mail/message/${encodeURIComponent(id)}${rest}`;

function mailTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (d.toDateString() === new Date().toDateString()) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function mailBanner() {
  const m = mailState();
  if (!m.available) {
    return `<div class="banner banner-coral"><p><b>No mail service.</b> Add Mailpit to <code>config\\services.json</code> (a service with <code>"type": "mail"</code>).</p></div>`;
  }
  if (m.running) return "";
  const s = mailSvc();
  return `<div class="banner banner-blue"><p><b>Mailpit is stopped.</b> Start it to catch the mail your PHP apps send to <code>127.0.0.1:${esc(m.smtp_port)}</code>.</p>
    ${s ? `<button class="btn btn-mint" data-act="start" data-id="${esc(s.id)}" ${BUSY.has(s.state) ? "disabled" : ""}>▶ Start Mailpit</button>` : ""}</div>`;
}

function viewMailSkeleton() {
  return `<div class="mail" id="mail">
      <div id="mail-banner">${mailBanner()}</div>
      <div class="mail-panes">
        <section class="mail-pane mail-list-pane" aria-label="Inbox">
          <form class="mail-search" data-form="mail-search" role="search">
            <input class="input" id="mail-q" name="q" value="${esc(MAIL.q)}" autocomplete="off" spellcheck="false"
              placeholder="Search: words, from:, to:, subject:, is:unread, has:attachment" aria-label="Search mail">
            <button class="btn" type="submit">Search</button>
          </form>
          <div class="mail-tools">
            <span class="muted grow" id="mail-count"></span>
            <button class="btn btn-sm" data-act="mail-refresh" title="Reload the list" aria-label="Reload">↻</button>
            <button class="btn btn-sm" data-act="mail-read-all">Mark all read</button>
            <button class="btn btn-sm btn-coral" data-act="mail-delete-all">Delete all</button>
            ${S.mail.ui ? `<a class="btn btn-sm" href="${esc(S.mail.ui)}" target="_blank" rel="noopener" title="Mailpit's own page">Mailpit ↗</a>` : ""}
          </div>
          <div class="mail-list" id="mail-list"></div>
        </section>
        <section class="mail-pane mail-reader" id="mail-reader" aria-live="polite"></section>
      </div>
    </div>`;
}

function renderMailList() {
  const list = $("#mail-list");
  if (!list) return;
  const m = mailState();
  $("#mail-count").textContent = !m.running ? "" : MAIL.q ? `${plural(MAIL.count, "match")} for “${MAIL.q}”`
    : `${plural(MAIL.total, "email")} · ${m.unread} unread`;
  if (MAIL.error) { list.innerHTML = `<div class="mail-empty"><b>Couldn't load mail</b><p>${esc(MAIL.error)}</p></div>`; return; }
  if (!MAIL.list.length) {
    list.innerHTML = !m.running ? `<div class="mail-empty"><b>Mailpit is stopped</b><p>Start it and mail shows up here.</p></div>`
      : MAIL.q ? `<div class="mail-empty"><b>Nothing matches</b><p>Try fewer words, or <code>from:</code> / <code>subject:</code>.</p></div>`
      : `<div class="mail-empty"><b>No mail yet</b><p>PHP's <code>mail()</code> and any SMTP library pointed at <code>127.0.0.1:${esc(m.smtp_port)}</code>
         land here. Nothing ever leaves this PC.</p></div>`;
    return;
  }
  const more = MAIL.list.length < (MAIL.q ? MAIL.count : MAIL.total);
  list.innerHTML = MAIL.list.map((x) => `<button class="mail-item${x.read ? "" : " unread"}${x.id === MAIL.selected ? " selected" : ""}" data-act="mail-open" data-id="${esc(x.id)}">
      <span class="mail-dot" aria-hidden="true"></span>
      <span class="mail-from">${esc(who(x.from))}</span>
      <span class="mail-time" title="${esc(new Date(x.created).toLocaleString())}">${esc(mailTime(x.created))}</span>
      <span class="mail-subject">${x.attachments ? '<span class="mail-clip" title="Has attachments">📎</span>' : ""}${esc(x.subject || "(no subject)")}</span>
      <span class="mail-snippet">${esc(x.snippet)}</span>
    </button>`).join("") + (more ? `<button class="btn btn-sm mail-more" data-act="mail-more">Load more</button>` : "");
}

async function loadMail(append = false) {
  if (!mailState().running) {
    Object.assign(MAIL, { list: [], total: 0, count: 0, error: "" });
    renderMailList();
    return;
  }
  try {
    const start = append ? MAIL.list.length : 0;
    const limit = append ? MAIL_PAGE : Math.max(MAIL_PAGE, MAIL.list.length);
    const data = await api.get(`/api/mail/messages?start=${start}&limit=${limit}&q=${encodeURIComponent(MAIL.q)}`);
    MAIL.list = append ? MAIL.list.concat(data.messages) : data.messages;
    Object.assign(MAIL, { total: data.total, count: data.count, error: "" });
  } catch (err) {
    MAIL.error = err.message;
  }
  renderMailList();
}

async function openMail(id) {
  Object.assign(MAIL, { selected: id, headers: null, remote: false, compat: null, links: null });
  renderMailList();
  const box = $("#mail-reader");
  if (box) box.innerHTML = `<div class="mail-empty"><p>Loading…</p></div>`;
  try {
    MAIL.detail = await api.get(mailUrl(id));
    MAIL.tab = MAIL.detail.has_html ? "html" : "text";
    const item = MAIL.list.find((x) => x.id === id);
    if (item) item.read = true;
  } catch (err) {
    MAIL.detail = null;
    if (box) box.innerHTML = `<div class="mail-empty"><b>Couldn't open it</b><p>${esc(err.message)}</p></div>`;
    return;
  }
  renderMailList();
  renderReader();
}

function renderReader() {
  const box = $("#mail-reader");
  if (!box) return;
  const d = MAIL.detail;
  if (!d || d.id !== MAIL.selected) {
    box.innerHTML = `<div class="mail-empty"><b>Pick an email</b><p>HTML is shown in a locked-down frame: no scripts run,
      and remote images (trackers) load only if you allow them.</p></div>`;
    return;
  }
  const people = (label, list) => (list.length ? `<dt>${label}</dt><dd>${list.map((a) => esc(whoFull(a))).join(", ")}</dd>` : "");
  const tabs = [["html", "HTML", d.has_html], ["text", "Text", true], ["headers", "Headers", true],
    ["compat", "Compatibility", d.has_html], ["links", "Links", true]]
    .map(([key, label, ok]) => `<button class="mail-tab" data-act="mail-tab" data-tab="${key}" aria-pressed="${MAIL.tab === key}" ${ok ? "" : "disabled"}>${label}</button>`).join("");
  let body;
  if (MAIL.tab === "html" && d.has_html) {
    body = `<iframe class="mail-frame" title="Email content" sandbox="allow-popups allow-popups-to-escape-sandbox"
      referrerpolicy="no-referrer" src="${mailUrl(d.id, "/html")}${MAIL.remote ? "?remote=1" : ""}"></iframe>`;
  } else if (MAIL.tab === "compat" && d.has_html) {
    body = compatHtml(MAIL.compat);
  } else if (MAIL.tab === "links") {
    body = linksHtml(MAIL.links);
  } else if (MAIL.tab === "headers") {
    body = MAIL.headers ? `<dl class="kv mail-headers">${Object.entries(MAIL.headers)
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v.map(esc).join("<br>")}</dd>`).join("")}</dl>` : `<p class="muted">Loading…</p>`;
  } else {
    body = `<pre class="mail-text">${esc(d.text || "(this email has no plain-text part)")}</pre>`;
  }
  box.innerHTML = `<div class="mail-head">
      <h2 class="mail-title">${esc(d.subject || "(no subject)")}</h2>
      <div class="row">
        <button class="btn btn-sm" data-act="mail-unread" data-id="${esc(d.id)}">Mark unread</button>
        <a class="btn btn-sm" href="${mailUrl(d.id, "/raw")}" download>Download .eml</a>
        ${S.mail.ui ? `<a class="btn btn-sm" href="${esc(S.mail.ui)}/view/${encodeURIComponent(d.id)}" target="_blank" rel="noopener" title="Open this email in Mailpit's own page">Mailpit ↗</a>` : ""}
        <button class="btn btn-sm btn-coral" data-act="mail-delete" data-id="${esc(d.id)}">Delete</button>
      </div>
    </div>
    <dl class="kv mail-meta"><dt>From</dt><dd>${esc(whoFull(d.from))}</dd>${people("To", d.to)}${people("Cc", d.cc)}${people("Bcc", d.bcc)}${people("Reply-To", d.reply_to)}
      <dt>Date</dt><dd>${esc(new Date(d.date).toLocaleString())}</dd><dt>Size</dt><dd>${fmtBytes(d.size)}${d.inline ? ` · ${plural(d.inline, "inline image")}` : ""}</dd></dl>
    ${d.attachments.length ? `<div class="row mail-files">${d.attachments.map((a) => `<a class="chip" href="${mailUrl(d.id, `/part/${encodeURIComponent(a.part)}`)}" download
        title="${esc(a.type)}">📎 ${esc(a.name)} <span class="muted">${fmtBytes(a.size)}</span></a>`).join("")}</div>` : ""}
    <div class="row mail-tabs">${tabs}<span class="grow"></span>
      ${MAIL.tab === "html" && d.has_html ? `<label class="toggle"><input type="checkbox" id="mail-remote" ${MAIL.remote ? "checked" : ""}> load remote images</label>` : ""}</div>
    <div class="mail-body">${body}</div>`;
  if (MAIL.tab === "headers" && !MAIL.headers) loadHeaders(d.id);
  if (MAIL.tab === "compat" && d.has_html && !MAIL.compat) loadCompat(d.id);
  box.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${el.dataset.w}%`; });  // CSP: no style attributes
}

function compatHtml(c) {
  if (!c) return `<p class="muted">Checking the HTML against ~30 email apps…</p>`;
  if (c.error) return `<div class="mail-empty"><b>Couldn't check</b><p>${esc(c.error)}</p></div>`;
  const items = c.warnings.map((w) => `<div class="compat-item ${w.unsupported >= 50 ? "bad" : w.unsupported > 0 ? "warn" : "part"}">
      <div class="row"><b>${esc(w.title)}</b><span class="chip">${esc(w.category)}</span><span class="grow"></span>
        ${w.unsupported ? `<span class="chip chip-coral">${w.unsupported}% not supported</span>` : ""}
        ${w.partial ? `<span class="chip chip-yellow">${w.partial}% partly</span>` : ""}</div>
      ${w.description ? `<p class="muted">${esc(w.description)}</p>` : ""}
      ${w.not_in.length ? `<p><b>Not shown in:</b> ${esc(w.not_in.join(", "))}</p>` : ""}
      ${w.partly_in.length ? `<p><b>Partly in:</b> ${esc(w.partly_in.join(", "))}</p>` : ""}
      ${w.notes.map((n) => `<p class="compat-note">${esc(n)}</p>`).join("")}
      ${w.url ? `<a class="compat-link" href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">Details on caniemail.com ↗</a>` : ""}
    </div>`).join("");
  return `<div class="compat-sum">
      <div class="compat-bar" aria-hidden="true"><i class="cb-ok" data-w="${c.supported}"></i><i class="cb-part" data-w="${c.partial}"></i><i class="cb-no" data-w="${c.unsupported}"></i></div>
      <p><b>${c.supported}% supported</b> · ${c.partial}% partly · ${c.unsupported}% not supported
        <span class="muted">— ${c.tests} checks across ${c.apps} email apps (caniemail.com data, checked on this PC)</span></p>
    </div>
    ${items || `<div class="mail-empty"><b>No problems found</b><p>Everything this email uses works in every checked email app.</p></div>`}`;
}

async function loadCompat(id) {
  try { MAIL.compat = await api.get(mailUrl(id, "/html-check")); } catch (err) { MAIL.compat = { error: err.message }; }
  if (MAIL.selected === id && MAIL.tab === "compat") renderReader();
}

function linksHtml(l) {
  if (l === "running") return `<p class="muted">Visiting every link… (up to a minute for slow sites)</p>`;
  if (!l) {
    return `<div class="links-intro"><p>Visits every link and image in this email and reports broken ones.
        <b>This goes out to the internet</b> (from this PC), so it only runs when you click.</p>
      <div class="row"><button class="btn btn-blue" data-act="mail-links">Check links</button>
        <label class="toggle"><input type="checkbox" id="mail-follow" ${MAIL.follow ? "checked" : ""}> follow redirects</label></div></div>`;
  }
  if (l.error) return `<div class="mail-empty"><b>Couldn't check</b><p>${esc(l.error)}</p></div>`;
  const cls = (c) => (c >= 200 && c < 300 ? "chip-mint" : c >= 300 && c < 400 ? "chip-yellow" : "chip-coral");
  return `<p><b>${plural(l.links.length, "link")}</b>${l.errors ? ` · <span class="ab-bad">${l.errors} broken</span>` : " · all fine"}
      <button class="btn btn-sm" data-act="mail-links">Check again</button></p>
    ${l.links.length ? `<table class="links-table"><tbody>${l.links.map((x) => `<tr>
        <td><span class="chip ${cls(x.code)}">${x.code || "—"}</span></td>
        <td class="mono links-url"><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url)}</a></td>
        <td class="muted">${esc(x.status)}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">This email has no links or images.</p>`}`;
}

async function checkLinks() {
  const id = MAIL.selected;
  MAIL.links = "running";
  renderReader();
  try { MAIL.links = await api.get(mailUrl(id, `/link-check${MAIL.follow ? "?follow=1" : ""}`)); } catch (err) { MAIL.links = { error: err.message }; }
  if (MAIL.selected === id) renderReader();
}

async function loadHeaders(id) {
  try { MAIL.headers = await api.get(mailUrl(id, "/headers")); } catch (err) { MAIL.headers = { Error: [err.message] }; }
  if (MAIL.selected === id) renderReader();
}

async function mailPost(path, body) {
  try { await api.post(path, body); return true; } catch (err) { toast({ ok: false, title: "Mail", message: err.message }); return false; }
}

function closeMail() {
  Object.assign(MAIL, { selected: null, detail: null, headers: null });
  renderReader();
}

async function fillHomeMail(force = false) {
  const box = $("#home-mail");
  if (!box) return;
  const m = mailState();
  $("#home-mail-count").innerHTML = m.running ? `<span class="chip ${m.unread ? "chip-yellow" : ""}">${m.unread} unread</span>` : "";
  if (!m.available) { box.innerHTML = `<p class="muted">No mail service is configured.</p>`; MAIL.homeKey = ""; return; }
  if (!m.running) {
    const s = mailSvc();
    box.innerHTML = `<div class="row"><span class="muted grow">Mailpit is stopped. Start it to catch what PHP sends to 127.0.0.1:${esc(m.smtp_port)}.</span>
      ${s ? `<button class="btn btn-sm btn-mint" data-act="start" data-id="${esc(s.id)}" ${BUSY.has(s.state) ? "disabled" : ""}>Start Mailpit</button>` : ""}</div>`;
    MAIL.homeKey = "";
    return;
  }
  const key = `${m.total}:${m.unread}`;
  if (!force && key === MAIL.homeKey && box.childElementCount) return;
  MAIL.homeKey = key;
  try { MAIL.home = (await api.get("/api/mail/messages?limit=4")).messages; } catch { MAIL.home = []; }
  box.innerHTML = MAIL.home.length ? `<div class="mail-rows">${MAIL.home.map((x) => `<button class="mail-row${x.read ? "" : " unread"}" data-act="mail-home-open" data-id="${esc(x.id)}">
      <span class="mail-dot" aria-hidden="true"></span><span class="mail-from">${esc(who(x.from))}</span>
      <span class="mail-time">${esc(mailTime(x.created))}</span><span class="mail-subject">${esc(x.subject || "(no subject)")}</span></button>`).join("")}</div>`
    : `<p class="muted">No mail yet. Anything PHP sends to <code>127.0.0.1:${esc(m.smtp_port)}</code> shows up here, never on the internet.</p>`;
}

function mailToast(m) {
  const el = document.createElement("div");
  el.className = "toast toast-mail";
  el.innerHTML = `<div class="toast-title">✉ New mail</div>
    <div class="toast-msg"><b>${esc(m.subject || "(no subject)")}</b><br>from ${esc(whoFull(m.from))}</div>
    <div class="row"><button class="btn btn-sm btn-blue" data-act="mail-home-open" data-id="${esc(m.id)}">Open</button>
    <button class="btn btn-sm btn-ghost" data-act="dismiss">Dismiss</button></div>`;
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), 8000);
}

function onMailEvent(data) {
  if (!S) return;
  S.mail = { ...S.mail, running: data.running, unread: data.unread, total: data.total };
  renderShell();
  if (view === "mail" && $("#mail")) {
    $("#mail-banner").innerHTML = mailBanner();
    MAIL.running = data.running;
    loadMail();
  }
  if (view === "stack" && $("#bento")) fillHomeMail(true);
  for (const m of (data.new || []).slice(0, 3)) mailToast(m);
}

// ---------------------------------------------------------------- git status on site cards
// Read-only: the server runs git status/log without locks and never fetches. Cached here and on
// the server for 30 s; the Sites view re-renders often, so the cards are filled from GIT.

const GIT = { sites: {}, at: 0, loading: false, available: true };
const GIT_CODES = { M: "modified", A: "added", D: "deleted", R: "renamed", C: "copied", U: "conflict", "?": "new" };

function gitLine(g) {
  if (!GIT.available) return `<span class="muted git-none">Git isn't installed</span>`;
  if (!g) return `<span class="muted git-none">checking git…</span>`;
  if (!g.repo) return `<span class="muted git-none">${g.error ? esc(g.error) : "not a git repository"}</span>`;
  const dirty = (g.changed || 0) + (g.untracked || 0) + (g.conflicts || 0);
  const files = (g.files || []).map((f) => {
    const code = f.code === "??" ? "?" : (f.code.replace(".", "")[0] || "M");
    return `${GIT_CODES[code] || code}: ${f.path}`;
  }).join("\n") + (dirty > (g.files || []).length ? `\n… and ${dirty - g.files.length} more` : "");
  const chips = [
    `<span class="chip chip-mono git-branch" title="${esc(g.root)}">⎇ ${esc(g.detached ? "detached HEAD" : g.branch || "?")}</span>`,
    g.shared ? `<span class="chip git-shared" title="Shared repository: ${esc(g.root)}">${esc(g.repo_name)}</span>` : "",
    dirty ? `<span class="chip chip-yellow" title="${esc(files)}">${g.conflicts ? `${g.conflicts} conflict${g.conflicts > 1 ? "s" : ""} · ` : ""}${dirty} changed</span>`
      : `<span class="chip chip-mint">clean</span>`,
    g.ahead ? `<span class="chip chip-blue" title="Commits not pushed to ${esc(g.upstream)}">↑${g.ahead} to push</span>` : "",
    g.behind ? `<span class="chip chip-coral" title="Commits on ${esc(g.upstream)} you don't have (as of the last fetch)">↓${g.behind} to pull</span>` : "",
    g.upstream ? "" : `<span class="chip" title="This branch has no upstream">local only</span>`,
  ].join("");
  const last = g.last ? `<div class="git-last" title="${esc(g.last.subject)}\n${esc(g.last.author)} · ${esc(fmtDate(g.last.when))}"><span class="mono">${esc(g.last.hash)}</span>
      <span class="muted">${esc(mailTime(new Date(g.last.when * 1000).toISOString()))} ·</span> ${esc(g.last.subject)}</div>` : "";
  return `<div class="row git-chips">${chips}</div>${last}`;
}

function fillSiteGit() {
  document.querySelectorAll("[data-git]").forEach((el) => { el.innerHTML = gitLine(GIT.sites[el.dataset.git]); });
}

async function loadSiteGit(force = false) {
  if (GIT.loading || (!force && Date.now() - GIT.at < 30000)) { fillSiteGit(); return; }
  GIT.loading = true;
  try {
    const r = await api.get(`/api/sites/git${force ? "?refresh=1" : ""}`);
    GIT.available = r.available;
    GIT.sites = r.sites;
    GIT.at = Date.now();
  } catch { /* the cards keep showing what they had */ }
  GIT.loading = false;
  fillSiteGit();
}

// ---------------------------------------------------------------- doctor view
// Built once; a run replaces the results. Fixes map onto existing actions where one exists.

const DOC = { result: null, running: false };
const DOC_ICON = { fail: "✖", warn: "⚠", info: "ℹ", ok: "✔" };

function doctorFixButton(f) {
  if (!f.fix) return "";
  const [kind, arg] = [f.fix.split(":")[0], f.fix.split(":").slice(1).join(":")];
  const btn = (label, attrs) => `<button class="btn btn-sm btn-ink" ${attrs}>${label}</button>`;
  switch (kind) {
    case "hosts": return btn("Update hosts file", 'data-act="doc-hosts"');
    case "trust": return btn("Trust HTTPS", 'data-act="doc-trust"');
    case "renew-certs": return btn("Renew certificates", `data-act="doc-fix" data-ref="${esc(f.fix)}"`);
    case "stop-pid": return btn("Stop it", `data-act="doc-fix" data-ref="${esc(f.fix)}"`);
    case "goto": return btn(`Open ${esc(VIEWS[arg] ? VIEWS[arg].title : arg)} →`, `data-act="goto" data-view="${esc(arg)}"`);
    case "log": return btn("Show log", `data-act="view-log" data-id="${esc(arg)}"`);
    case "php": return btn("Configure PHP…", `data-act="php-config" data-id="${esc(arg)}"`);
    default: return "";
  }
}

function viewDoctorSkeleton() {
  return `<div class="doctor" id="doctor">
      <div class="doc-head card">
        <div class="grow"><h2 class="card-title">Check the whole setup</h2>
          <p class="muted">Services, ports, leftover processes, Apache's config, sites and hosts, HTTPS certificates,
            every PHP build, disk space, backups and config files. Nothing is changed until you press a fix.</p></div>
        <div class="doc-summary" id="doc-summary"></div>
        <button class="btn btn-blue doc-run" data-act="doc-run">↻ Check everything</button>
      </div>
      <div id="doc-results"></div>
    </div>`;
}

function renderDoctor() {
  const box = $("#doc-results");
  if (!box) return;
  const r = DOC.result;
  $("#doc-summary").innerHTML = DOC.running ? `<span class="chip chip-yellow">⏳ checking…</span>`
    : r ? `${r.counts.fail ? `<span class="chip chip-coral">${plural(r.counts.fail, "problem")}</span>` : ""}
           ${r.counts.warn ? `<span class="chip chip-yellow">${plural(r.counts.warn, "warning")}</span>` : ""}
           <span class="chip chip-mint">${r.counts.ok} OK</span>
           <span class="muted doc-when">checked ${esc(fmtWhen(r.checked_at))} in ${r.took} s</span>` : "";
  document.querySelector(".doc-run").disabled = DOC.running;
  if (!r) { box.innerHTML = DOC.running ? `<div class="empty"><p>Checking… this takes a few seconds.</p></div>` : ""; return; }
  const groups = [];
  for (const f of r.findings) {
    let g = groups.find((x) => x.name === f.group);
    if (!g) groups.push(g = { name: f.group, items: [] });
    g.items.push(f);
  }
  box.innerHTML = groups.map((g) => `<section class="doc-group">
      <h3 class="section-title">${esc(g.name)}</h3>
      ${g.items.map((f) => `<div class="doc-row doc-${esc(f.status)}">
          <span class="doc-icon" aria-label="${esc(f.status)}">${DOC_ICON[f.status]}</span>
          <div class="grow"><div class="doc-title">${esc(f.title)}</div>${f.detail ? `<div class="doc-detail">${esc(f.detail)}</div>` : ""}</div>
          ${doctorFixButton(f)}</div>`).join("")}
    </section>`).join("");
}

async function runDoctor() {
  if (DOC.running) return;
  DOC.running = true;
  renderDoctor();
  try { DOC.result = await api.post("/api/doctor/run"); }
  catch (err) { toast({ ok: false, title: "Doctor", message: err.message }); }
  DOC.running = false;
  renderDoctor();
}

async function doctorAction(path, body) {
  try {
    const r = await api.post(path, body);
    if (r && r.message) toast({ ok: true, title: "Doctor", message: r.message });
  } catch (err) { toast({ ok: false, title: "Doctor", message: err.message }); return; }
  setTimeout(runDoctor, path.startsWith("/api/doctor") ? 300 : 2500);  // background actions need a moment
}

// ---------------------------------------------------------------- PHP errors view
// Like the Mail view, it is built once and refreshed in place (filters and open stack traces stay).

const ERR = { severity: "", site: "", q: "", list: [], open: new Set() };
const SEV_LABEL = { error: "Errors", warning: "Warnings", notice: "Notices" };

function homeErrorsChip() {
  const e = S.php_errors || { unseen_errors: 0, unseen_warnings: 0 };
  if (e.unseen_errors) return `<button class="chip chip-coral" data-act="goto" data-view="errors">${plural(e.unseen_errors, "new error")}</button>`;
  if (e.unseen_warnings) return `<button class="chip chip-yellow" data-act="goto" data-view="errors">${plural(e.unseen_warnings, "new warning")}</button>`;
  return `<span class="chip chip-mint">none new</span>`;
}

function viewErrorsSkeleton() {
  const e = S.php_errors || { notify: "errors" };
  const sites = [["", "All sites"], ...S.sites.map((s) => [s.domain, s.domain]), ["-", "Not in a site"]];
  return `<div class="errors" id="errors">
      <div class="err-bar">
        <div class="seg" role="group" aria-label="Severity">${[["", "All"], ["error", "Errors"], ["warning", "Warnings"], ["notice", "Notices"]]
          .map(([v, l]) => `<button class="seg-btn" data-act="err-filter" data-ref="${v}" aria-pressed="${ERR.severity === v}">${l}</button>`).join("")}</div>
        <select class="select" id="err-site" aria-label="Site">${sites.map(([v, l]) => `<option value="${esc(v)}" ${ERR.site === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
        <input class="input err-search" id="err-q" value="${esc(ERR.q)}" placeholder="Search message or file" autocomplete="off" spellcheck="false" aria-label="Search errors">
        <span class="grow"></span>
        <label class="err-notify">Alert me for
          <select class="select" id="err-notify">${[["errors", "fatal errors"], ["warnings", "errors + warnings"], ["off", "nothing"]]
            .map(([v, l]) => `<option value="${v}" ${e.notify === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <button class="btn btn-sm btn-coral" data-act="err-clear">Clear all</button>
      </div>
      <div id="err-list"></div>
    </div>`;
}

function errRow(g) {
  const trace = g.detail && g.detail.includes("\n");
  const where = g.file ? `<span class="mono err-file" title="${esc(g.file)}">${esc(g.file_name)}:${g.line}</span>` : "";
  return `<article class="err-row sev-${esc(g.severity)}${g.seen ? "" : " unseen"}${g.muted ? " muted" : ""}">
      <div class="err-top">
        <span class="err-level">${esc(g.level)}</span>
        ${g.site ? `<span class="chip chip-blue">${esc(g.site)}</span>` : `<span class="chip">no site</span>`}
        ${where}
        ${g.count > 1 ? `<span class="chip chip-ink">×${g.count}</span>` : ""}
        <span class="grow"></span>
        <span class="muted err-time" title="first ${esc(fmtDate(g.first_seen))}">${esc(mailTime(new Date(g.last_seen * 1000).toISOString()))}</span>
      </div>
      <div class="err-msg">${esc(g.message)}</div>
      ${g.file ? `<div class="muted mono err-path">${esc(g.file)}</div>` : ""}
      ${trace ? `<details class="err-trace" data-id="${esc(g.id)}" ${ERR.open.has(g.id) ? "open" : ""}><summary>Stack trace</summary><pre>${esc(g.detail)}</pre></details>` : ""}
      <div class="row err-actions">
        ${g.file ? `<button class="btn btn-sm btn-blue" data-act="err-open" data-id="${esc(g.id)}">Open in VS Code</button>` : ""}
        <button class="btn btn-sm" data-act="${g.muted ? "err-unmute" : "err-mute"}" data-id="${esc(g.id)}" title="${g.muted ? "Alert for it again" : "No more alerts for this one"}">${g.muted ? "Unmute" : "Mute alerts"}</button>
        <button class="btn btn-sm btn-ghost" data-act="err-dismiss" data-id="${esc(g.id)}">Dismiss</button>
      </div>
    </article>`;
}

async function loadErrors() {
  const box = $("#err-list");
  if (!box) return;
  const site = ERR.site === "-" ? "" : ERR.site;
  try {
    let list = await api.get(`/api/php-errors?severity=${encodeURIComponent(ERR.severity)}&site=${encodeURIComponent(site)}&q=${encodeURIComponent(ERR.q)}`);
    if (ERR.site === "-") list = list.filter((g) => !g.site);
    ERR.list = list;
  } catch (err) {
    box.innerHTML = `<div class="empty"><h3>Couldn't load the errors</h3><p>${esc(err.message)}</p></div>`;
    return;
  }
  box.innerHTML = ERR.list.length ? ERR.list.map(errRow).join("")
    : `<div class="empty"><h3>${ERR.severity || ERR.site || ERR.q ? "Nothing matches" : "No PHP errors 🎉"}</h3>
        <p>varsha follows every PHP version's <code>logs\\php-*.log</code> live. When a site hits an error it shows up here at once,
        grouped and with the site, file and line${(S.php_errors || {}).notify === "off" ? "" : ", and you get an alert"}.</p></div>`;
  if ((S.php_errors || {}).unseen) {
    await api.post("/api/php-errors/seen").catch(() => {});
    S.php_errors = { ...S.php_errors, unseen: 0, unseen_errors: 0, unseen_warnings: 0 };
    renderShell();
  }
}

function errToast(g) {
  const el = document.createElement("div");
  el.className = `toast${g.severity === "error" ? " bad" : " toast-warn"}`;
  el.innerHTML = `<div class="toast-title">⚠ PHP ${esc(g.level)}${g.site ? ` · ${esc(g.site)}` : ""}</div>
    <div class="toast-msg">${esc(g.message.slice(0, 240))}${g.file ? `\n${esc(g.file_name)}:${g.line}` : ""}</div>
    <div class="row"><button class="btn btn-sm" data-act="goto" data-view="errors">View</button>
    ${g.file ? `<button class="btn btn-sm btn-blue" data-act="err-open" data-id="${esc(g.id)}">Open in VS Code</button>` : ""}
    <button class="btn btn-sm btn-ghost" data-act="dismiss">Dismiss</button></div>`;
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), g.severity === "error" ? 15000 : 8000);
}

function onPhpErrorEvent(data) {
  if (!S) return;
  S.php_errors = data.summary;
  if (view === "errors" && $("#errors")) loadErrors(); else renderShell();
  if (view === "stack" && $("#bento")) fillHome();
  for (const g of (data.new || []).slice(0, 3)) errToast(g);
}

async function errPost(path, body) {
  try { return await api.post(path, body); } catch (err) { toast({ ok: false, title: "PHP errors", message: err.message }); return null; }
}

// ---------------------------------------------------------------- tasks view (cron)

const TASK_TEMPLATES = [
  { key: "script", label: "PHP script", hint: "Any cron.php style script in a site",
    task: { name: "PHP script", command: "php cron.php", every: "hourly", where: "project" } },
  { key: "custom", label: "Custom command", hint: "Any command line (cmd.exe)",
    task: { name: "", command: "", every: "daily", where: "project" } },
];
const EVERY_OPTIONS = [["1m", "Every minute"], ["5m", "Every 5 minutes"], ["15m", "Every 15 minutes"], ["30m", "Every 30 minutes"],
  ["hourly", "Every hour"], ["6h", "Every 6 hours"], ["12h", "Every 12 hours"], ["daily", "Every day at\u2026"], ["weekly", "Every week on\u2026"]];
const task = (id) => (S.tasks || []).find((t) => t.id === id);
const fmtDuration = (s) => (s < 1 ? `${Math.round(s * 1000)} ms` : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);

function taskFolder(t) {
  if (t.where === "custom") return t.cwd;
  return `${t.site} \u00b7 ${t.where === "project" ? "project folder" : "www folder"}`;
}

function viewTasks() {
  const tasks = S.tasks || [];
  const head = `<div class="toolbar"><p class="muted">Run commands on a schedule, like cron on Linux: report scripts, cleanups, imports.
      Each task runs with its site's PHP, composer and mysql on PATH. Tasks run only while varsha runs.</p>
      <button class="btn btn-ink" data-act="task-new" data-ref="custom">+ New task</button></div>`;
  const errors = S.schedule_error ? `<div class="banner banner-coral"><p><b>schedule.json couldn't be read:</b> ${esc(S.schedule_error)}</p></div>` : "";
  const templates = `<div class="task-templates">${TASK_TEMPLATES.map((tpl) => `<button class="task-template" data-act="task-new" data-ref="${tpl.key}">
      <b>${esc(tpl.label)}</b><span>${esc(tpl.hint)}</span></button>`).join("")}</div>`;
  if (!tasks.length) {
    return head + errors + `<div class="empty"><h3>No tasks yet</h3><p>Start from a template:</p>${templates}</div>`;
  }
  return head + errors + `<div class="task-grid">${tasks.map(taskCard).join("")}</div>
    <h2 class="section-title">Add another</h2>${templates}`;
}

function taskCard(t) {
  let status;
  if (t.running) status = `<span class="chip chip-yellow">\u23f3 running\u2026</span>`;
  else if (!t.enabled) status = `<span class="chip">paused</span>`;
  else status = `<span class="muted">next ${esc(fmtWhen(t.next_due))}</span>`;
  const last = t.last_run ? `<div class="task-last ${t.last_ok ? "ok" : "bad"}">${t.last_ok ? "\u2714" : "\u2716"} ${esc(fmtWhen(t.last_run))}
      \u00b7 ${esc(t.message)} \u00b7 ${esc(fmtDuration(t.duration || 0))}</div>` : `<div class="task-last muted">Never ran yet</div>`;
  return `<article class="card task-card${t.enabled ? "" : " paused"}${t.last_ok === false && t.enabled ? " failed" : ""}">
      <div class="card-head"><h3 class="card-title">${esc(t.name)}</h3>
        <button class="switch" role="switch" aria-checked="${t.enabled}" aria-label="${t.enabled ? "Pause" : "Resume"} ${esc(t.name)}"
          data-act="task-toggle" data-id="${esc(t.id)}"></button></div>
      <div class="row"><span class="chip chip-blue">${esc(t.describe)}</span>${status}</div>
      <div class="conn"><code title="${esc(t.command)}">${esc(t.command)}</code></div>
      <div class="muted task-where">\ud83d\udcc2 ${esc(taskFolder(t))}${t.php ? ` \u00b7 PHP ${esc(t.php)}` : ""}</div>
      ${last}
      ${t.output ? `<pre class="task-output">${esc(t.output)}</pre>` : ""}
      <div class="row">
        <button class="btn btn-sm btn-mint" data-act="task-run" data-id="${esc(t.id)}" ${t.running ? "disabled" : ""}>\u25b6 Run now</button>
        <button class="btn btn-sm" data-act="task-edit" data-id="${esc(t.id)}">Edit\u2026</button>
        <button class="btn btn-sm" data-act="view-log" data-id="task-${esc(t.id)}">Log</button>
        <span class="grow"></span>
        <button class="btn btn-sm btn-coral" data-act="task-delete" data-id="${esc(t.id)}">Delete</button>
      </div>
    </article>`;
}

function taskBody(form) {
  const body = {
    name: form.get("name"), command: form.get("command"), site: form.get("site") || "",
    where: form.get("site") ? form.get("where") : "custom", cwd: form.get("cwd") || "",
    php: form.get("php") || "", every: form.get("every"), at: form.get("at") || "00:00",
    day: parseInt(form.get("day"), 10), timeout: parseInt(form.get("timeout"), 10) || 600, enabled: form.has("enabled"),
  };
  if (body.where === "custom" && !body.cwd) throw new Error("Choose a site, or type the full folder the command runs in.");
  return body;
}

function taskDialog(existing, templateKey) {
  const tpl = TASK_TEMPLATES.find((x) => x.key === templateKey) || TASK_TEMPLATES.find((x) => x.key === "custom");
  const t = existing || { ...tpl.task, site: S.sites[0]?.domain || "", cwd: "", php: "", at: "02:00", day: 6, timeout: 600, enabled: true };
  const opts = (pairs, current) => pairs.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(current) ? "selected" : ""}>${esc(l)}</option>`).join("");
  const sites = [["", "No site (custom folder)"], ...S.sites.map((s) => [s.domain, s.domain])];
  const php = [["", "The site's PHP (or the default)"], ...usablePhp().map((p) => [p.version, `PHP ${p.label}`])];
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => [i, d]);
  dialog(`<h2>${existing ? "Edit task" : `New task \u00b7 ${esc(tpl.label)}`}</h2>
    <label>Name<input class="input" name="name" value="${esc(t.name)}" maxlength="60" required placeholder="e.g. Banking nightly report"></label>
    <label>Command<input class="input mono" name="command" value="${esc(t.command)}" required spellcheck="false"
      placeholder="php cron.php"></label>
    <div class="form-row even">
      <label>Site<select class="input" name="site">${opts(sites, t.site)}</select></label>
      <label>Runs in<select class="input" name="where">${opts([["project", "Project folder (where composer.json is)"], ["site", "The site's www folder"]], t.where === "custom" ? "project" : t.where)}</select></label>
    </div>
    <label>\u2026or a custom folder (used when no site is chosen)<input class="input mono" name="cwd" value="${esc(t.cwd)}" spellcheck="false" placeholder="D:\\scripts"></label>
    <div class="form-row even">
      <label>How often<select class="input" name="every">${opts(EVERY_OPTIONS, t.every)}</select></label>
      <label>At (daily / weekly)<input class="input" type="time" name="at" value="${esc(t.at)}"></label>
      <label>Day (weekly)<select class="input" name="day">${opts(days, t.day)}</select></label>
    </div>
    <div class="form-row even">
      <label>PHP<select class="input" name="php">${opts(php, t.php)}</select></label>
      <label>Time limit (seconds)<input class="input" type="number" name="timeout" min="10" max="86400" value="${esc(t.timeout)}"></label>
    </div>
    <label class="check"><input type="checkbox" name="enabled" ${t.enabled ? "checked" : ""}> Active
      <small>A task never overlaps itself: if it's still running when it comes due again, that run is skipped.</small></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button>
      ${existing ? "" : `<button class="btn" value="save-run">Save &amp; run now</button>`}<button class="btn btn-ink" value="ok">Save</button></div>`,
  async (form, button) => {
    const body = taskBody(form);
    const saved = await api.post(existing ? `/api/tasks/${existing.id}` : "/api/tasks", body);
    if (button === "save-run") await api.post(`/api/tasks/${saved.id}/run`);
    toast({ ok: true, title: existing ? "Task saved" : "Task created", message: body.name });
  });
}

// ---------------------------------------------------------------- render

const RENDER = { tasks: viewTasks, sites: viewSites, databases: viewDatabases, share: viewShare, logs: viewLogs, system: viewSystem };

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
  if (view === "errors") {
    if (!$("#errors")) { host.innerHTML = viewErrorsSkeleton(); loadErrors(); }
    return;
  }
  if (view === "doctor") {
    if (!$("#doctor")) { host.innerHTML = viewDoctorSkeleton(); renderDoctor(); if (!DOC.result) runDoctor(); }
    return;
  }
  if (view === "mail") {
    if (!$("#mail")) { host.innerHTML = viewMailSkeleton(); MAIL.running = mailState().running; renderReader(); loadMail(); }
    else {
      $("#mail-banner").innerHTML = mailBanner();
      if (MAIL.running !== mailState().running) { MAIL.running = mailState().running; loadMail(); }
    }
    return;
  }
  const shareValue = $("#share-target")?.value;
  const focused = document.activeElement?.id;
  host.innerHTML = RENDER[view]();
  if (view === "sites") loadSiteGit();
  if (shareValue !== undefined && $("#share-target")) $("#share-target").value = shareValue;
  if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
}

async function refresh() {
  try {
    S = await api.get("/api/state");
    $("#offline").hidden = true;
    render();
  } catch (err) {
    const banner = $("#offline");
    // 401: varsha is running but this tab's login belongs to an earlier run (each run has a new one).
    banner.textContent = err.status === 401
      ? "This tab's login has expired because varsha was restarted. Open the control panel again from the varsha tray icon (right-click → Open control panel)."
      : "Lost contact with varsha. Is it still running? Reconnecting…";
    banner.hidden = false;
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
  // Cancel must close the dialog without the browser first demanding the required fields.
  form.querySelectorAll('button[value="cancel"]').forEach((b) => { b.formNoValidate = true; });
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
      <span class="grow mono">${esc(b.name)}${b.name.startsWith("auto_") ? ' <span class="chip chip-mint">auto</span>' : ""}</span><span class="muted">${fmtBytes(b.size)} \u00b7 ${esc(fmtDate(b.modified))}</span></label>`).join("");
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
    ["link", "Link a folder", "Use a project folder that lives elsewhere (a junction, like mklink /J). Nothing is copied or changed."],
    ["wordpress", "WordPress", "Latest release from wordpress.org with wp-config.php filled in."]];
  dialog(`<h2>Quick Create</h2>
    <div class="kinds">${kinds.map(([k, t, d], i) => `<label class="kind"><input type="radio" name="kind" value="${k}" ${i === 0 ? "checked" : ""}>
      <b>${t}</b><small>${esc(d)}</small></label>`).join("")}</div>
    <label class="link-only" hidden>Project folder
      <div class="pick-row"><input class="input mono" name="source" placeholder="D:\\Projects\\shop" spellcheck="false" autocomplete="off">
        <button class="btn" type="button" data-pick-folder>Browse\u2026</button></div>
      <small class="muted">Its <code>public\\</code> folder is used as the web root when it has one. Deleting the site later removes only the link.</small></label>
    <label>Folder name<input class="input" name="name" placeholder="shop" pattern="[a-z0-9][a-z0-9-]*" required spellcheck="false" autocomplete="off">
      <small class="muted">Becomes <b>name.${esc(S.tld)}</b> \u00b7 lowercase letters, digits and "-".</small></label>
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
    const kind = form.get("kind");
    await api.post("/api/create", { kind, name: form.get("name"), php: form.get("php"), db: form.get("db"),
      source: kind === "link" ? form.get("source") : "" });
    toast({ ok: true, title: "Creating\u2026", message: kind === "link" ? "The site appears in a second." : "Follow along in Logs \u2192 Quick Create." });
  });
  const form = $("#dialog-form");
  const linkRow = form.querySelector(".link-only");
  const source = form.elements.source;
  const name = form.elements.name;
  let autoName = true;  // fill the name from the folder until the user types one
  name.addEventListener("input", () => { autoName = name.value === ""; });
  const slug = (path) => (path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const fillName = () => { if (autoName && source.value) name.value = slug(source.value); };
  source.addEventListener("input", fillName);
  const sync = () => {
    const link = form.elements.kind.value === "link";
    linkRow.hidden = !link;
    source.required = link;
  };
  form.querySelectorAll('input[name="kind"]').forEach((r) => r.addEventListener("change", sync));
  form.querySelector("[data-pick-folder]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const picked = (await api.post("/api/pick-folder", { title: "Choose the project folder" })).path;
      if (picked) { source.value = picked; fillName(); }
    } catch (err) {
      form.querySelector(".form-error").textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });
}

function deleteSiteDialog(domain) {
  const site = S.sites.find((s) => s.domain === domain);
  const what = site && site.link
    ? `<p>This site is a <b>link</b> to <code>${esc(site.link)}</code>. Only the link in <code>www\\</code> is removed:
       the project folder and its files are <b>not touched</b>. Its database is <b>not</b> deleted.</p>`
    : `<p>The folder moves to the <b>Windows Recycle Bin</b>, so you can restore it from there. Its database is <b>not</b> deleted.</p>`;
  dialog(`<h2>Delete ${esc(domain)}?</h2>
    ${what}
    <label>Type <code>${esc(domain)}</code> to confirm<input class="input" name="confirm" autocomplete="off" spellcheck="false"></label>
    <div class="form-error"></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn btn-coral" value="ok">${site && site.link ? "Remove link" : "Move to Recycle Bin"}</button></div>`,
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
    case "autobackup": autoBackupDialog(svc(id)); return;
    case "import-wizard": importWizard(); return;
    case "task-new": taskDialog(null, ref); return;
    case "task-edit": taskDialog(task(id)); return;
    case "task-run": await act(`/api/tasks/${id}/run`); return;
    case "task-toggle": {
      const t = task(id);
      await act(`/api/tasks/${id}`, { ...t, enabled: !t.enabled });
      return;
    }
    case "task-delete": {
      const t = task(id);
      confirmDialog(`Delete the task “${t.name}”?`, "It stops running. Its log file is kept in logs\\tasks.", "Delete",
        async () => { await api.post(`/api/tasks/${id}/delete`); });
      return;
    }
    case "autobackup-run": await act(`/api/services/${id}/autobackup-run`); return;
    case "unshare": await act(`/api/tunnels/${id}/stop`); break;
    case "share-site": go("share"); $("#share-target").value = ref; $("#share-target").focus(); return;
    case "lan-share": {
      const domain = ref || $("#lan-site")?.value;
      if (domain) await act(`/api/lan/${encodeURIComponent(domain)}/share`);
      return;
    }
    case "lan-stop": await act(`/api/lan/${encodeURIComponent(ref)}/stop`); return;
    case "lan-qr": lanQrDialog(ref); return;
    case "lan-firewall-remove":
      confirmDialog("Remove the LAN firewall rule?", "Colleagues can't reach shared sites until you share one again (that asks for the rule again).",
        "Remove", async () => { await api.post("/api/lan/firewall", { enable: false }); });
      return;
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
    case "git-refresh": GIT.sites = {}; fillSiteGit(); loadSiteGit(true); return;
    case "doc-run": runDoctor(); return;
    case "doc-fix": doctorAction("/api/doctor/fix", { fix: ref }); return;
    case "doc-hosts": doctorAction("/api/hosts/update"); return;
    case "doc-trust": doctorAction("/api/https/trust"); return;
    case "err-filter": ERR.severity = ref || ""; document.querySelectorAll("#errors .seg-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.ref === ERR.severity))); loadErrors(); return;
    case "err-open": { const r = await errPost(`/api/php-errors/${id}/open`); if (r) toast({ ok: true, title: "PHP error", message: r.message }); return; }
    case "err-mute": case "err-unmute": if (await errPost(`/api/php-errors/${id}/${action.slice(4)}`)) loadErrors(); return;
    case "err-dismiss": if (await errPost(`/api/php-errors/${id}/dismiss`)) loadErrors(); return;
    case "err-clear":
      confirmDialog("Clear all PHP errors?", "The list is emptied. The log files themselves are kept in logs\\.", "Clear all",
        async () => { await api.post("/api/php-errors/clear"); loadErrors(); });
      return;
    case "sites-page": homeSitesPageNo = Math.max(0, homeSitesPageNo + Number(ref)); fillHome(); return;
    case "mail-open": openMail(id); return;
    case "mail-home-open": el.closest(".toast")?.remove(); if (view !== "mail") go("mail"); openMail(id); return;
    case "mail-refresh": loadMail(); return;
    case "mail-more": loadMail(true); return;
    case "mail-links": checkLinks(); return;
    case "mail-tab": MAIL.tab = el.dataset.tab; renderReader(); return;
    case "mail-unread":
      if (await mailPost("/api/mail/read", { ids: [id], read: false })) {
        const item = MAIL.list.find((x) => x.id === id);
        if (item) item.read = false;
        closeMail(); renderMailList();
      }
      return;
    case "mail-delete":
      if (await mailPost("/api/mail/delete", { ids: [id] })) {
        MAIL.list = MAIL.list.filter((x) => x.id !== id);
        closeMail(); loadMail();
      }
      return;
    case "mail-read-all": if (await mailPost("/api/mail/read", { ids: [], read: true })) loadMail(); return;
    case "mail-delete-all":
      confirmDialog("Delete all mail?", `Every email in Mailpit (${plural(MAIL.total || mailState().total, "email")}) is deleted. This can't be undone.`,
        "Delete all", async () => { await api.post("/api/mail/delete", { all: true }); MAIL.list = []; closeMail(); loadMail(); });
      return;
    default: return;
  }
  render();
});

document.addEventListener("submit", (event) => {
  const kind = event.target.dataset.form;
  if (kind === "mail-search") {
    event.preventDefault();
    MAIL.q = $("#mail-q").value.trim();
    loadMail();
    return;
  }
  if (kind !== "share") return;
  event.preventDefault();
  const target = event.target.target.value.trim();
  if (target) shareDialog(target);
});

document.addEventListener("change", (event) => {
  if (event.target.id === "log-follow") logsTail.follow = event.target.checked;
  if (event.target.id === "mail-remote") { MAIL.remote = event.target.checked; renderReader(); }
  if (event.target.id === "mail-follow") MAIL.follow = event.target.checked;
  if (event.target.id === "err-site") { ERR.site = event.target.value; loadErrors(); }
  if (event.target.id === "err-notify") {
    errPost("/api/php-errors/settings", { notify: event.target.value })
      .then((r) => r && toast({ ok: true, title: "PHP error alerts", message: { errors: "Fatal errors only", warnings: "Errors and warnings", off: "Off" }[event.target.value] }));
  }
  if (event.target.dataset.sitePhp) act(`/api/sites/${event.target.dataset.sitePhp}/php`, { php: event.target.value });
  if (event.target.name?.startsWith("ext:")) event.target.closest(".ext")?.classList.toggle("on", event.target.checked);
  if (event.target.id === "home-log-src") {
    homeTail.start(event.target.value);
    $("#home-log-full").dataset.id = event.target.value;
  }
});

let errSearchTimer = null;
document.addEventListener("input", (event) => {
  if (event.target.id !== "err-q") return;
  clearTimeout(errSearchTimer);
  errSearchTimer = setTimeout(() => { ERR.q = event.target.value.trim(); loadErrors(); }, 250);
});
// <details> "toggle" doesn't bubble: listen in the capture phase to remember open stack traces.
document.addEventListener("toggle", (event) => {
  const d = event.target;
  if (!d.classList || !d.classList.contains("err-trace")) return;
  if (d.open) ERR.open.add(d.dataset.id); else ERR.open.delete(d.dataset.id);
}, true);

document.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, dialog") || event.ctrlKey || event.metaKey || event.altKey) return;
  const index = "1234567890".indexOf(event.key);
  if (index >= 0) go(Object.keys(VIEWS)[index]);
  if (event.key === "Escape" && openMenu) { openMenu = null; render(); }
});

function connect() {
  const source = new EventSource("/api/events");
  let offlineTimer = null;
  source.addEventListener("hello", () => { clearTimeout(offlineTimer); $("#offline").hidden = true; refresh(); });
  source.addEventListener("state", scheduleRefresh);
  source.addEventListener("toast", (event) => { toast(JSON.parse(event.data)); scheduleRefresh(); });
  source.addEventListener("mail", (event) => onMailEvent(JSON.parse(event.data)));
  source.addEventListener("php_error", (event) => onPhpErrorEvent(JSON.parse(event.data)));
  source.onerror = () => { clearTimeout(offlineTimer); offlineTimer = setTimeout(() => { $("#offline").hidden = false; }, 2500); };
}

refresh().then(connect);
