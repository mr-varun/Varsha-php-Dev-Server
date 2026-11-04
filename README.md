# Build "VStack" — a Laragon-like local dev environment for Windows

You are a senior Python + Windows systems developer. Build **VStack**, a portable local web development environment for Windows 10/11, similar to Laragon. Work **phase by phase**. At the start, show me a short plan and the folder structure, then build Phase 1. **Stop after each phase**, summarize what works and how to test it, and wait for my OK before starting the next phase.

---

## Tech stack
- Python 3.12+, **PySide6** (GUI)
- `psutil` (processes, ports), `watchdog` (folder watching), `dnslib` (local DNS), `jinja2` (config templates)
- **PyInstaller** for `vstack.exe`, **Inno Setup** script for the installer
- Type hints everywhere, `logging` module (no print), `pytest` tests for non-GUI logic
- Windows only. Use `subprocess.CREATE_NO_WINDOW` for all child processes.

## Core principles
1. **Portable**: every path is relative to the VStack root folder. Copying the folder to another drive must still work. Never hardcode `C:\`.
2. **Config-driven**: all services are defined in `config/services.json`. Adding a new service (e.g. Redis) must need no code changes.
3. **Least privilege**: `vstack.exe` runs as a normal user. A separate small `vstack-helper.exe` (built with `--uac-admin`) does admin-only tasks: editing the hosts file, installing the mkcert CA, starting DNS on port 53. The GUI calls the helper only when needed.
4. **Never touch user data unsafely**: hosts file edits only inside `# vstack-start` / `# vstack-end` markers. Back up any file before rewriting it.
5. **Do not bundle or auto-download binaries in Phase 1–4.** Assume I place them in `bin\` manually. Detect versions by scanning folders. Show a clear message in the GUI if a binary is missing.

## Folder structure
```
VStack\
 ├─ vstack.exe
 ├─ vstack-helper.exe
 ├─ config\        services.json, settings.json, sites.json
 ├─ bin\
 │   ├─ apache\apache-2.4.x\
 │   ├─ php\php-7.4\  php-8.3\  php-8.4\
 │   ├─ mysql\mysql-5.7\  mysql-8.4\
 │   ├─ mariadb\mariadb-11.4\
 │   ├─ mailpit\mailpit.exe
 │   ├─ mkcert\mkcert.exe
 │   ├─ composer\composer.phar
 │   └─ phpmyadmin\
 ├─ etc\
 │   ├─ templates\    (jinja2: httpd.conf, vhost.conf, php.ini, my.ini, phpmyadmin config.inc.php)
 │   ├─ apache\sites-enabled\
 │   └─ php\
 ├─ data\
 │   ├─ mysql-5.7\  mariadb-11.4\  mysql-8.4\
 │   ├─ ssl\
 │   └─ mailpit\
 ├─ www\
 ├─ logs\
 └─ src\vstack\     (Python source)
```

## Services (default `services.json`)
| Service | Binary | Port(s) |
|---|---|---|
| Apache 2.4 | `bin\apache\...\bin\httpd.exe` | 80, 443 |
| MySQL 5.7 | `bin\mysql\mysql-5.7\bin\mysqld.exe` | 3306 |
| MariaDB 11.4 | `bin\mariadb\mariadb-11.4\bin\mariadbd.exe` | 3307 |
| MySQL 8.4 | `bin\mysql\mysql-8.4\bin\mysqld.exe` | 3308 |
| Mailpit | `bin\mailpit\mailpit.exe` | SMTP 1025, UI 8025 |
| DNS (.test resolver) | built-in Python | 53 (127.0.0.1 only) |

Each service entry: `id, name, type, exe, args, ports, config_template, data_dir, init_command, autostart, enabled`.

---

## Phase 1 — Service Manager
- Main window: one row per service with status light (green running / grey stopped / red error), name, version, port(s), Start, Stop, Restart, Logs buttons.
- Start All / Stop All.
- **Port check** before start; if a port is busy, show which process (via psutil) is using it.
- Track processes by PID, capture stdout/stderr to `logs\<service>.log`, live log viewer window.
- Graceful stop: `httpd.exe -k stop` for Apache, `mysqladmin shutdown` for MySQL/MariaDB, terminate for Mailpit. Kill after timeout.
- Auto-restart on unexpected crash (max 3 tries, then show error).
- On app exit: ask to stop all services.

## Phase 2 — Config Engine + Databases + Tools
- Generate all configs from jinja2 templates into `etc\` with correct absolute paths at runtime.
- **Each DB instance** gets its own `my.ini` with unique `port`, `basedir`, `datadir`, `socket` (named pipe), `pid-file`, log paths.
- **First-run init**:
  - MySQL 5.7 / 8.4: `mysqld --defaults-file=<ini> --initialize-insecure`
  - MariaDB: `mysql_install_db.exe --datadir=<dir> --port=3307`
- Root password reset action per DB.
- **phpMyAdmin**: auto-generate `config.inc.php` with 3 servers (host `127.0.0.1`, ports 3306/3307/3308, `auth_type=cookie`, `verbose` names), random `blowfish_secret`. Serve via Apache at `http://localhost/phpmyadmin`. "Open phpMyAdmin" button per DB opens `index.php?server=N`.
- **Mailpit**: args `--smtp 127.0.0.1:1025 --listen 127.0.0.1:8025 --database data\mailpit\mailpit.db`. Every generated `php.ini` sets `SMTP=127.0.0.1`, `smtp_port=1025`. "Open Mail Inbox" button.
- Database backup/restore (mysqldump / mysql client) per DB to `data\backups\`.

## Phase 3 — Auto VHost, SSL, DNS
- Watch `www\` with watchdog. Each folder `name` → `name.test`.
- Docroot: `public\` if it exists, else the folder. Allow override in `sites.json`.
- Generate `etc\apache\sites-enabled\name.test.conf` with HTTP (80) and HTTPS (443) vhosts, `ServerAlias *.name.test`. `httpd.conf` uses `IncludeOptional` for that folder. Remove configs for deleted folders.
- **SSL**: on first run, helper runs `mkcert -install`. For each site: `mkcert -cert-file data\ssl\name.test.pem -key-file data\ssl\name.test-key.pem name.test "*.name.test"`.
- **Hosts file** (via helper): write all site domains inside the markers.
- **Local DNS resolver** (optional, setting toggle, via helper): answers any `*.test` query with `127.0.0.1` on `127.0.0.1:53`, forwards everything else to the system's upstream DNS. Explain in the GUI how to set it as the Windows DNS for the loopback, or document the limitation clearly. When DNS is enabled, wildcard subdomains work without hosts edits.
- Reload Apache gracefully after changes (`httpd.exe -k restart` equivalent on Windows, or stop/start).
- Never use `.dev` TLD. Default TLD `.test`, configurable.

## Phase 4 — PHP + Developer Tools
- Detect PHP versions in `bin\php\`. One-click default PHP switch.
- **Per-site PHP version** using Apache `mod_fcgid` with `php-cgi.exe` of the chosen version (set in `sites.json`, selectable in GUI).
- php.ini editor + extension toggle list (checkboxes) per PHP version.
- **Built-in terminal** button: opens Windows Terminal / cmd with PATH set to the default PHP, Composer, MySQL client, and `www\`.
- **Quick Create**: Laravel (`composer create-project laravel/laravel`), WordPress (download latest + create DB + wp-config), blank PHP. Auto-creates DB and vhost.
- Site list tab: domain, docroot, PHP version, buttons: Open http, Open https, Folder, Terminal, Delete.
- System tray icon with menu: Start All, Stop All, sites submenu, phpMyAdmin, Mail, Quit.

## Phase 5 — Version Manager
- Download PHP / MySQL / MariaDB / Apache builds from **official sources only** (windows.php.net, dev.mysql.com, mariadb.org, apachelounge.com), verify checksums where available, extract into `bin\`.
- Add/remove versions in the GUI.

## Phase 6 — Polish & Release
- Settings page: TLD, ports, autostart with Windows, start minimized to tray, dark/light theme.
- Auto-updater that checks GitHub releases.
- PyInstaller spec for both exes + Inno Setup installer script.
- README with screenshots placeholders, setup guide, and troubleshooting (port 80 in use by IIS/Skype, Firefox certificate trust, antivirus).

## Bonus (only after Phase 6, ask me first)
- Share a site publicly via Cloudflare Tunnel.
- Per-project `.vstack.json` (PHP version, DB, env).
- Built-in MCP server so AI assistants can start/stop services and create sites.

---

## Quality rules
- Clean modules: `services/`, `config_engine/`, `vhosts/`, `ssl/`, `dns/`, `php/`, `gui/`, `helper/`.
- GUI never blocks: run long tasks in `QThread`/worker pool with progress feedback.
- All errors shown in the GUI with a clear message and a "View log" link.
- Write pytest tests for config generation, hosts-file marker editing, port checks, vhost generation, and services.json loading.
- Explain any Windows-specific gotcha you hit in code comments.
- Before each phase ends: run the tests, list the manual test steps, and list known limitations.

Start now: show the plan + folder tree, then build Phase 1.