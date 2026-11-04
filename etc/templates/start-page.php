<?php
/*
 * varsha start page (http://localhost/).
 *
 * Everything shown comes from varsha itself: sites from the generated Apache vhosts
 * (etc/apache/sites-enabled), services and ports from config/services.json, defaults from
 * config/settings.json. Works on every bundled PHP (5.4 to 8.5), so no ?? / fn / type hints.
 *
 * Privacy: requests that come through a tunnel (Cloudflare adds CF-Connecting-IP) or a proxy
 * reach Apache from 127.0.0.1 too, so they are treated as remote and see only a short notice.
 */

$ROOT = dirname(__DIR__);

function h($s) { return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8'); }

function server($key, $default = '') { return isset($_SERVER[$key]) ? $_SERVER[$key] : $default; }

function read_json($path)
{
    if (!is_file($path)) return array();
    $data = json_decode((string) @file_get_contents($path), true);
    return is_array($data) ? $data : array();
}

$isLocal = in_array(server('REMOTE_ADDR'), array('127.0.0.1', '::1'), true)
    && server('HTTP_CF_CONNECTING_IP') === '' && server('HTTP_X_FORWARDED_FOR') === '';

$settings = read_json($ROOT . '/config/settings.json');
$tld = isset($settings['tld']) ? $settings['tld'] : 'test';
$defaultPhp = isset($settings['default_php']) ? $settings['default_php'] : PHP_VERSION;

/* ---------- services ---------- */

function port_open($port, $timeout = 0.25)
{
    $fp = @fsockopen('127.0.0.1', (int) $port, $errno, $errstr, $timeout);
    if (!$fp) return false;
    fclose($fp);
    return true;
}

/** DNS answers on UDP only: send a real query and wait briefly for any reply with our id. */
function dns_up($tld)
{
    $fp = @fsockopen('udp://127.0.0.1', 53, $errno, $errstr, 0.25);
    if (!$fp) return false;
    $name = '';
    foreach (array('varsha', $tld) as $label) $name .= chr(strlen($label)) . $label;
    @fwrite($fp, pack('nnnnnn', 0x7661, 0x0100, 1, 0, 0, 0) . $name . "\0" . pack('nn', 1, 1));
    stream_set_timeout($fp, 0, 300000);
    $reply = @fread($fp, 512);
    fclose($fp);
    return is_string($reply) && strlen($reply) >= 12 && substr($reply, 0, 2) === "\x76\x61";
}

function load_services($root)
{
    $raw = read_json($root . '/config/services.json');
    $out = array();
    foreach (isset($raw['services']) ? $raw['services'] : array() as $s) {
        if (!is_array($s) || !isset($s['id']) || (isset($s['enabled']) && !$s['enabled'])) continue;
        $ports = isset($s['ports']) ? array_values($s['ports']) : array();
        $out[] = array(
            'id' => $s['id'],
            'name' => isset($s['name']) ? $s['name'] : $s['id'],
            'type' => isset($s['type']) ? $s['type'] : '',
            'ports' => $ports,
        );
    }
    return $out;
}

function service_states($services, $tld)
{
    $states = array();
    foreach ($services as $s) {
        if ($s['type'] === 'builtin' && in_array(53, $s['ports'])) $up = dns_up($tld);
        elseif ($s['ports']) $up = port_open($s['ports'][0]);
        else $up = false;
        $states[$s['id']] = $up;
    }
    return $states;
}

/* ---------- sites (from the vhosts varsha generated) ---------- */

function load_sites($root, $defaultPhp)
{
    $sites = array();
    foreach ((array) glob($root . '/etc/apache/sites-enabled/*.conf') as $file) {
        $text = (string) @file_get_contents($file);
        if (!preg_match('/ServerName\s+(\S+)/i', $text, $m)) continue;
        $name = $m[1];
        $docroot = preg_match('/DocumentRoot\s+"?([^"\r\n]+)"?/i', $text, $d) ? str_replace('\\', '/', $d[1]) : '';
        $php = $defaultPhp;
        if (preg_match('#FcgidWrapper\s+"?[^"\r\n]*/php/php-([0-9][0-9.]*)#i', $text, $p)) $php = rtrim($p[1], '.');
        $wwwPos = stripos($docroot, '/www/');
        $sites[$name] = array(
            'name' => $name,
            'folder' => $wwwPos === false ? $docroot : 'www' . substr($docroot, $wwwPos + 4),
            'https' => stripos($text, 'SSLEngine on') !== false,
            'php' => $php,
            'mtime' => (int) @filemtime($docroot),
        );
    }
    ksort($sites, SORT_NATURAL | SORT_FLAG_CASE);
    return array_values($sites);
}

function ago($ts)
{
    if (!$ts) return '';
    $d = time() - $ts;
    if ($d < 60) return 'just now';
    if ($d < 3600) return floor($d / 60) . ' min ago';
    if ($d < 86400) return floor($d / 3600) . ' h ago';
    return floor($d / 86400) . ' days ago';
}

/* ---------- routes ---------- */

$q = isset($_GET['q']) ? (string) $_GET['q'] : '';
if ($q !== '') {
    if (!$isLocal) {
        http_response_code(403);
        exit('Only available on this computer.');
    }
    if ($q === 'info') {
        phpinfo();
        exit;
    }
    if ($q === 'status') {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
        echo json_encode(service_states(load_services($ROOT), $tld));
        exit;
    }
    http_response_code(404);
    exit('Unknown page.');
}

if (!$isLocal) {
    http_response_code(200);
    ?><!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>varsha</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f0e6;color:#121212;font-family:"Segoe UI",system-ui,sans-serif}
div{border:3px solid #121212;border-radius:10px;background:#fff;box-shadow:6px 6px 0 #121212;padding:28px 32px;max-width:420px}h1{margin:0 0 8px;font-size:24px}p{margin:0;color:#55524b}</style></head>
<body><div><h1>varsha</h1><p>This is a local development server. There's nothing public here.</p></div></body></html><?php
    exit;
}

$services = load_services($ROOT);
$states = service_states($services, $tld);
$sites = load_sites($ROOT, $defaultPhp);
$upCount = count(array_filter($states));

$mailUi = null;
$databases = array();
foreach ($services as $s) {
    if ($s['type'] === 'mail' && count($s['ports']) > 1) $mailUi = 'http://127.0.0.1:' . (int) $s['ports'][1] . '/';
    if (in_array($s['type'], array('mysql', 'mariadb'), true) && $s['ports']) $databases[] = $s;
}

$extensions = array('pdo_mysql', 'mysqli', 'mbstring', 'openssl', 'curl', 'gd', 'intl', 'zip',
                    'fileinfo', 'exif', 'sodium', 'xml', 'Zend OPcache', 'xdebug');
$loaded = array_map('strtolower', get_loaded_extensions());
$extOn = 0;
foreach ($extensions as $e) if (in_array(strtolower($e), $loaded, true)) $extOn++;
$iniRows = array(
    'memory_limit' => ini_get('memory_limit'),
    'upload_max_filesize' => ini_get('upload_max_filesize'),
    'post_max_size' => ini_get('post_max_size'),
    'max_execution_time' => ini_get('max_execution_time') . ' s',
    'date.timezone' => ini_get('date.timezone') ? ini_get('date.timezone') : 'not set',
    'display_errors' => ini_get('display_errors') ? 'on' : 'off',
);
$https = server('HTTPS') !== '' && strtolower(server('HTTPS')) !== 'off';
$httpsSites = 0;
foreach ($sites as $s) if ($s['https']) $httpsSites++;

/* Lines typed into the status terminal: real states, not a canned demo. */
$termLines = array(array('$ varsha status', 'cmd'));
foreach ($services as $s) {
    $up = !empty($states[$s['id']]);
    $label = strtolower($s['name']);
    $dots = str_repeat('.', max(3, 26 - strlen($label)));
    $termLines[] = array($label . ' ' . $dots . ' ' . ($up ? 'running' : 'stopped'), $up ? 'ok' : 'off');
}
$termLines[] = array('php ' . PHP_VERSION . ' (' . PHP_SAPI . ') ' . str_repeat('.', max(3, 18 - strlen(PHP_VERSION))) . ' ready', 'ok');
$termLines[] = array('sites: ' . count($sites) . ' on .' . $tld . ', ' . $httpsSites . ' with https', '');
$termLines[] = array('extensions: ' . $extOn . '/' . count($extensions) . ' common ones loaded', '');
$termLines[] = array($upCount === count($services) ? 'all systems go.' : ($upCount . ' of ' . count($services) . ' services up'), $upCount === count($services) ? 'ok' : 'warn');
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>varsha · localhost</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='3' y='3' width='58' height='58' rx='12' fill='%232f4bff' stroke='%23121212' stroke-width='5'/%3E%3Cpath d='M18 20 L32 46 L46 20' fill='none' stroke='%23ffd23f' stroke-width='7' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{
  --paper:#f4f0e6; --card:#fff; --text:#121212; --muted:#55524b; --edge:#121212;
  --blue:#2f4bff; --sky:#c9d3ff; --yellow:#ffd23f; --mint:#3ddc97; --mint-soft:#c8f5df;
  --coral:#ff5c5c; --coral-soft:#ffd6d6; --lilac:#b9a6ff; --grey:#d8d2c2; --dots:rgba(18,18,18,.08);
  --term-bg:#0f1115; --term-text:#e7e7e7;
  --line:3px solid var(--edge); --thin:2px solid var(--edge);
  --shadow:6px 6px 0 var(--edge); --shadow-sm:4px 4px 0 var(--edge); --shadow-xs:2px 2px 0 var(--edge);
  --r:10px; --r-sm:7px; --wire:rgba(18,18,18,.22);
  --font:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Mono","Cascadia Code",Consolas,"Courier New",monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#16161a; --card:#202026; --text:#f2efe6; --muted:#b3ada0; --edge:#f2efe6;
    --sky:#2b3266; --mint-soft:#17402e; --coral-soft:#4a2020; --grey:#3a3a42; --dots:rgba(242,239,230,.07);
    --shadow:6px 6px 0 #000; --shadow-sm:4px 4px 0 #000; --shadow-xs:2px 2px 0 #000; --wire:rgba(242,239,230,.18);
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;min-height:100vh;background:var(--paper);background-image:radial-gradient(var(--dots) 1px,transparent 1px);
  background-size:18px 18px;color:var(--text);font-family:var(--font);font-size:15px;line-height:1.45;overflow-x:hidden;position:relative}
a{color:inherit}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px;position:relative;z-index:1}

/* ================= animated background ================= */
.bg{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.glow{position:absolute;inset:-10%;transition:transform .6s ease-out;will-change:transform}
.glow span{position:absolute;border-radius:50%;filter:blur(80px)}
.glow .g1{width:560px;height:560px;left:-8%;top:-14%;background:radial-gradient(circle,rgba(47,75,255,.20),transparent 70%);animation:driftA 26s ease-in-out infinite}
.glow .g2{width:480px;height:480px;right:-6%;top:18%;background:radial-gradient(circle,rgba(255,210,63,.30),transparent 70%);animation:driftB 32s ease-in-out infinite}
.glow .g3{width:440px;height:440px;left:28%;bottom:-16%;background:radial-gradient(circle,rgba(61,220,151,.20),transparent 70%);animation:driftC 38s ease-in-out infinite}
@keyframes driftA{50%{transform:translate(70px,40px) scale(1.12)}}
@keyframes driftB{50%{transform:translate(-60px,60px) scale(1.08)}}
@keyframes driftC{50%{transform:translate(40px,-60px) scale(1.15)}}

.wires{position:absolute;inset:0;width:100%;height:100%}
.wires path{fill:none;stroke:var(--wire);stroke-width:2;stroke-dasharray:1600;stroke-dashoffset:1600;
  animation:draw 4.5s ease-out forwards}
.wires path.w2{animation-delay:.4s}.wires path.w3{animation-delay:.8s}
.wires path.flow{stroke-width:3;stroke-linecap:round;stroke-dasharray:3 28;stroke-dashoffset:0;opacity:0;
  animation:flowIn .6s ease-out 4.6s forwards,flowRun 3.4s linear 5.2s infinite}
.wires path.flow.blue{stroke:var(--blue)}.wires path.flow.yellow{stroke:#e0a800}.wires path.flow.mint{stroke:#1fa56b}
.wires path.flow.rev{animation:flowIn .6s ease-out 5s forwards,flowRun 4.2s linear 5.6s infinite reverse}
.wires circle{fill:var(--paper);stroke:var(--edge);stroke-width:2;opacity:0;animation:dotIn .4s ease-out forwards}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes flowIn{to{opacity:.75}}
@keyframes flowRun{to{stroke-dashoffset:-310}}
@keyframes dotIn{to{opacity:.7}}

.scan{position:absolute;left:-10%;width:120%;height:200px;filter:blur(2px);
  background:linear-gradient(180deg,transparent,rgba(47,75,255,.07) 45%,rgba(47,75,255,.11) 50%,rgba(47,75,255,.07) 55%,transparent);
  animation:sweep 10s ease-in-out infinite}
@keyframes sweep{0%,100%{top:-18%}50%{top:96%}}

.sparks{position:absolute;inset:0}
.spark{position:absolute;width:5px;height:5px;margin:-2px;border-radius:50%;background:var(--blue);opacity:0;animation:twinkle 5s ease-in-out infinite}
.spark.live{background:#e0a800;animation:twinkleOnce 2.4s ease-out forwards}
@keyframes twinkle{0%,100%{opacity:0;transform:scale(.5)}50%{opacity:.6;transform:scale(1)}}
@keyframes twinkleOnce{0%{opacity:0;transform:scale(.4)}25%{opacity:.9;transform:scale(1.5)}100%{opacity:0;transform:scale(.6)}}

/* ================= header ================= */
.top{display:flex;flex-wrap:wrap;align-items:center;gap:16px;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:14px}
.logo{width:58px;height:58px;border:var(--line);border-radius:14px;background:var(--blue);box-shadow:var(--shadow-sm);
  display:grid;place-items:center;transform:rotate(-4deg);animation:wobble 6s ease-in-out infinite}
.logo svg{width:32px;height:32px}
.logo path{stroke-dasharray:60;stroke-dashoffset:60;animation:drawV 1.1s .3s ease-out forwards}
@keyframes wobble{0%,100%{transform:rotate(-4deg)}50%{transform:rotate(3deg) translateY(-2px)}}
@keyframes drawV{to{stroke-dashoffset:0}}
.brand h1{margin:0;font-size:34px;letter-spacing:-.5px;line-height:1}
.brand p{margin:4px 0 0;color:var(--muted);font-size:14px}
.tag{display:inline-block;background:var(--text);color:var(--yellow);font-family:var(--mono);font-size:12px;
  padding:2px 8px;border-radius:5px;vertical-align:middle;margin-left:6px}
.pills{display:flex;flex-wrap:wrap;gap:10px}
.pill{border:var(--thin);border-radius:999px;background:var(--card);padding:6px 14px;font-weight:600;font-size:14px;
  box-shadow:var(--shadow-xs);display:inline-flex;align-items:center;gap:9px;white-space:nowrap}
.pill.up{background:var(--mint-soft)}
.pill.warn{background:var(--coral-soft)}
#clock{font-family:var(--mono)}
.led{position:relative;width:10px;height:10px;border-radius:50%;background:var(--mint);border:2px solid var(--edge);flex:none}
.pill.warn .led{background:var(--coral)}
.led::after{content:"";position:absolute;inset:-2px;border-radius:50%;border:2px solid var(--mint);opacity:0}
.pill.warn .led::after{border-color:var(--coral)}
.led.ping::after{animation:ping 1s ease-out}
@keyframes ping{0%{opacity:.8;transform:scale(1)}100%{opacity:0;transform:scale(3.2)}}

/* seconds gauge */
.gauge{height:12px;border:var(--thin);border-radius:999px;background:var(--card);overflow:hidden;box-shadow:var(--shadow-xs);margin:0 0 6px}
.gauge i{display:block;height:100%;width:0;background:repeating-linear-gradient(-45deg,var(--yellow) 0 10px,#ffe27a 10px 20px);
  border-right:2px solid var(--edge);transition:width .2s linear;animation:stripes 1s linear infinite;background-size:28px 28px}
@keyframes stripes{to{background-position:28px 0}}
.gauge-label{font-family:var(--mono);font-size:11px;color:var(--muted);margin:0 0 20px;display:flex;justify-content:space-between}

/* actions */
.actions{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
.btn{display:inline-flex;align-items:center;gap:8px;border:var(--line);border-radius:var(--r-sm);background:var(--card);
  color:var(--text);padding:10px 16px;font:inherit;font-weight:700;text-decoration:none;box-shadow:var(--shadow-sm);
  cursor:pointer;transition:transform .08s,box-shadow .08s}
.btn:hover{transform:translate(-2px,-2px);box-shadow:6px 6px 0 var(--edge)}
.btn:active{transform:translate(2px,2px);box-shadow:var(--shadow-xs)}
.btn.blue{background:var(--blue);color:#fff}
.btn.yellow{background:var(--yellow);color:#121212}
.btn.mint{background:var(--mint);color:#121212}
.btn small{font-weight:500;opacity:.8}

/* ================= bento ================= */
.bento{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:20px}
.tile{grid-column:span 12;background:var(--card);border:var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:18px 20px;min-width:0;
  transition:transform .15s,box-shadow .15s}
.tile:hover{transform:translate(-3px,-3px);box-shadow:9px 9px 0 var(--edge)}
.tile h2{margin:0 0 14px;font-size:18px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.tile h2 .count{font-family:var(--mono);font-size:13px;font-weight:500;color:var(--muted)}
@media (min-width:900px){.span-8{grid-column:span 8}.span-4{grid-column:span 4}}
.tile.sky{background:var(--sky)}
/* entrance: tiles pop in one after another */
.pop{opacity:0;transform:translateY(18px) rotate(-1deg);animation:popIn .55s cubic-bezier(.2,.9,.3,1.3) forwards}
@keyframes popIn{to{opacity:1;transform:none}}

/* terminal */
.term{background:var(--term-bg);color:var(--term-text);border:var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.term-bar{display:flex;align-items:center;gap:7px;padding:9px 12px;border-bottom:var(--line);background:var(--text)}
.term-bar b{width:12px;height:12px;border-radius:50%;border:2px solid #000;display:block}
.term-bar b:nth-child(1){background:var(--coral)}.term-bar b:nth-child(2){background:var(--yellow)}.term-bar b:nth-child(3){background:var(--mint)}
.term-bar span{margin-left:8px;font-family:var(--mono);font-size:12px;color:var(--yellow)}
@media (prefers-color-scheme:dark){.term-bar{background:#000}}
.term-body{padding:14px 16px;font-family:var(--mono);font-size:13.5px;line-height:1.85;min-height:250px;white-space:pre-wrap;word-break:break-word}
.term-body .cmd{color:var(--yellow)}
.term-body .ok{color:var(--mint)}
.term-body .off{color:#8d8a83}
.term-body .warn{color:var(--coral)}
.caret{display:inline-block;width:8px;height:15px;background:var(--yellow);vertical-align:-2px;margin-left:2px;animation:blink 1s step-end infinite}
@keyframes blink{50%{opacity:0}}

/* now tile */
.now .big-clock{font-family:var(--mono);font-size:38px;font-weight:700;line-height:1;margin:0 0 4px;letter-spacing:-1px}
.now .date{color:var(--muted);margin:0 0 16px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{border:var(--thin);border-radius:var(--r-sm);padding:10px;background:var(--paper);text-align:center}
.stat b{display:block;font-size:30px;line-height:1.05}
.stat span{font-family:var(--mono);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat:nth-child(1){background:var(--mint-soft)}.stat:nth-child(2){background:var(--sky)}.stat:nth-child(3){background:#fff1b8}
@media (prefers-color-scheme:dark){.stat:nth-child(3){background:#4a3d12}}
.orbit{width:64px;height:64px;float:right;margin:-4px -4px 0 0}
.orbit circle{fill:none;stroke:var(--edge);stroke-width:2}
.orbit .hand{stroke:var(--blue);stroke-width:3;stroke-linecap:round;transform-origin:32px 32px}
.orbit .sec{stroke:var(--coral);stroke-width:2}

/* sites */
.search{width:100%;border:var(--thin);border-radius:var(--r-sm);background:var(--paper);color:var(--text);
  padding:9px 12px;font:inherit;margin-bottom:14px}
.search:focus{outline:3px solid var(--yellow);outline-offset:1px}
.sites{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.site{border:var(--thin);border-radius:var(--r-sm);padding:12px 14px;background:var(--paper);display:flex;flex-direction:column;gap:8px;
  transition:transform .1s,box-shadow .1s;opacity:0;animation:popIn .45s cubic-bezier(.2,.9,.3,1.3) forwards}
.site:hover{transform:translate(-2px,-2px) rotate(-.4deg);box-shadow:var(--shadow-sm)}
.site .name{font-weight:700;font-size:16px;word-break:break-all;text-decoration:none}
.site .name:hover{text-decoration:underline;text-decoration-thickness:2px}
.site .meta{font-family:var(--mono);font-size:12px;color:var(--muted);word-break:break-all}
.site .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:auto}
.chip{border:2px solid var(--edge);border-radius:6px;padding:1px 7px;font-family:var(--mono);font-size:12px;
  background:var(--card);text-decoration:none;white-space:nowrap;transition:transform .08s}
.chip:hover{transform:translateY(-1px)}
.chip.php{background:var(--lilac);color:#121212}
.chip.ssl{background:var(--mint);color:#121212}
a.chip:hover{background:var(--yellow);color:#121212}
button.chip{cursor:pointer;color:var(--text);font-family:var(--mono)}
button.chip:hover{background:var(--yellow);color:#121212}
.empty{border:2px dashed var(--edge);border-radius:var(--r-sm);padding:22px;text-align:center;color:var(--muted)}
.empty code{font-family:var(--mono);background:var(--yellow);color:#121212;padding:1px 6px;border-radius:4px}

/* services */
.svc{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:2px dashed var(--grey)}
.svc:last-child{border-bottom:0}
.dot{position:relative;width:14px;height:14px;border-radius:50%;border:2px solid var(--edge);background:var(--grey);flex:none;transition:background .3s}
.dot.on{background:var(--mint)}
.dot.on::after{content:"";position:absolute;inset:-2px;border-radius:50%;border:2px solid var(--mint);animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{opacity:.7;transform:scale(1)}70%,100%{opacity:0;transform:scale(2.2)}}
.svc.flash{animation:flash .6s ease-out}
@keyframes flash{0%{background:rgba(255,210,63,.55)}100%{background:transparent}}
.svc .nm{font-weight:600;flex:1;min-width:0}
.svc .pt{font-family:var(--mono);font-size:12px;color:var(--muted)}
.svc .st{font-family:var(--mono);font-size:11px;text-transform:uppercase;width:62px;text-align:right}

/* php */
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:14px;margin:0}
.kv dt{color:var(--muted);font-family:var(--mono);font-size:12.5px;padding-top:2px}
.kv dd{margin:0;font-weight:600;word-break:break-all}
.exts{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.ext{border:2px solid var(--edge);border-radius:6px;padding:2px 8px;font-family:var(--mono);font-size:12px;background:var(--grey);opacity:.6}
.ext.on{background:var(--mint-soft);opacity:1}
.big{font-size:40px;font-weight:800;line-height:1;margin:0 0 4px}

/* databases */
.db{border:var(--thin);border-radius:var(--r-sm);padding:10px 12px;margin-bottom:10px;background:var(--paper);transition:transform .1s}
.db:hover{transform:translateX(3px)}
.db:last-child{margin-bottom:0}
.db b{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.code{display:flex;gap:8px;align-items:center}
.code code{flex:1;min-width:0;font-family:var(--mono);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* steps */
.steps{margin:0;padding-left:22px}
.steps li{margin:6px 0}
.steps code{font-family:var(--mono);background:var(--yellow);color:#121212;padding:0 5px;border-radius:4px}

footer{margin-top:28px;display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;color:var(--muted);font-size:13px;font-family:var(--mono)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(120px);background:var(--text);color:var(--yellow);
  border:var(--thin);border-radius:8px;padding:8px 16px;font-weight:700;transition:transform .25s cubic-bezier(.2,.9,.3,1.3);z-index:5}
.toast.show{transform:translateX(-50%) translateY(0)}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;animation-delay:0s!important;transition-duration:.001ms!important}
  .bg .scan,.bg .sparks{display:none}
}
</style>
</head>
<body>

<div class="bg" aria-hidden="true">
  <div class="glow" id="glow"><span class="g1"></span><span class="g2"></span><span class="g3"></span></div>
  <svg class="wires" viewBox="0 0 1200 1600" preserveAspectRatio="xMidYMid slice">
    <path d="M -50,200 L 260,200 L 260,70 L 640,70 L 640,330 L 990,330 L 990,150 L 1260,150"/>
    <path class="w2" d="M -50,640 L 220,640 L 220,880 L 560,880 L 560,560 L 910,560 L 910,780 L 1260,780"/>
    <path class="w3" d="M -50,1200 L 340,1200 L 340,1000 L 700,1000 L 700,1300 L 1050,1300 L 1050,1100 L 1260,1100"/>
    <path class="flow blue" d="M -50,200 L 260,200 L 260,70 L 640,70 L 640,330 L 990,330 L 990,150 L 1260,150"/>
    <path class="flow yellow rev" d="M -50,640 L 220,640 L 220,880 L 560,880 L 560,560 L 910,560 L 910,780 L 1260,780"/>
    <path class="flow mint" d="M -50,1200 L 340,1200 L 340,1000 L 700,1000 L 700,1300 L 1050,1300 L 1050,1100 L 1260,1100"/>
    <circle cx="260" cy="200" r="6" style="animation-delay:1.2s"/><circle cx="640" cy="330" r="6" style="animation-delay:1.6s"/>
    <circle cx="990" cy="150" r="6" style="animation-delay:2s"/><circle cx="220" cy="880" r="6" style="animation-delay:1.4s"/>
    <circle cx="560" cy="560" r="6" style="animation-delay:1.8s"/><circle cx="910" cy="780" r="6" style="animation-delay:2.2s"/>
    <circle cx="340" cy="1000" r="6" style="animation-delay:1.6s"/><circle cx="700" cy="1300" r="6" style="animation-delay:2s"/>
    <circle cx="1050" cy="1100" r="6" style="animation-delay:2.4s"/>
  </svg>
  <div class="scan"></div>
  <div class="sparks" id="sparks"></div>
</div>

<div class="wrap">

  <header class="top">
    <div class="brand">
      <div class="logo" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M6 7 L16 26 L26 7" fill="none" stroke="#ffd23f" stroke-width="5" stroke-linejoin="round" stroke-linecap="round"/></svg></div>
      <div>
        <h1>varsha <span class="tag">localhost</span></h1>
        <p>Your local web dev server. Drop a folder into <b>www</b> and open <b>folder.<?= h($tld) ?></b></p>
      </div>
    </div>
    <div class="pills">
      <span class="pill <?= $upCount === count($services) ? 'up' : ($upCount === 0 ? 'warn' : '') ?>" id="upPill"><span class="led" id="led"></span><span><span id="upCount"><?= $upCount ?></span>/<?= count($services) ?> services up</span></span>
      <span class="pill">PHP <?= h(PHP_VERSION) ?></span>
      <span class="pill" id="clock">--:--:--</span>
    </div>
  </header>

  <div class="gauge" aria-hidden="true"><i id="gaugeFill"></i></div>
  <p class="gauge-label"><span id="gaugeLabel">T+00s</span><span>this minute</span></p>

  <nav class="actions">
    <a class="btn blue" href="/phpmyadmin/" target="_blank" rel="noopener">phpMyAdmin</a>
    <?php if ($mailUi): ?><a class="btn yellow" href="<?= h($mailUi) ?>" target="_blank" rel="noopener">Mail inbox <small>Mailpit</small></a><?php endif; ?>
    <a class="btn mint" href="/?q=info" target="_blank" rel="noopener">phpinfo()</a>
    <?php if (!$https): ?><a class="btn" href="https://localhost/">Switch to HTTPS</a><?php endif; ?>
  </nav>

  <main class="bento">

    <section class="tile term span-8 pop" style="animation-delay:.05s">
      <div class="term-bar"><b></b><b></b><b></b><span>varsha@localhost: ~</span></div>
      <div class="term-body" id="term"></div>
    </section>

    <section class="tile now span-4 pop" style="animation-delay:.12s">
      <svg class="orbit" viewBox="0 0 64 64" aria-hidden="true">
        <circle cx="32" cy="32" r="28"/><circle cx="32" cy="32" r="3" fill="var(--edge)"/>
        <line class="hand" id="handMin" x1="32" y1="32" x2="32" y2="12"/>
        <line class="hand sec" id="handSec" x1="32" y1="32" x2="32" y2="8"/>
      </svg>
      <h2>Now</h2>
      <p class="big-clock" id="bigClock">--:--</p>
      <p class="date" id="date">&nbsp;</p>
      <div class="stats">
        <div class="stat"><b data-count="<?= $upCount ?>">0</b><span>services</span></div>
        <div class="stat"><b data-count="<?= count($sites) ?>">0</b><span>sites</span></div>
        <div class="stat"><b data-count="<?= count($databases) ?>">0</b><span>databases</span></div>
      </div>
    </section>

    <section class="tile span-8 pop" style="animation-delay:.2s">
      <h2>Sites <span class="count"><?= count($sites) ?> found</span></h2>
      <?php if ($sites): ?>
        <?php if (count($sites) > 3): ?><input class="search" id="search" type="search" placeholder="Filter sites…" autocomplete="off"><?php endif; ?>
        <div class="sites" id="sites">
          <?php foreach ($sites as $i => $s):
            $scheme = $s['https'] ? 'https' : 'http'; ?>
            <div class="site" style="animation-delay:<?= number_format(0.35 + $i * 0.05, 2) ?>s" data-name="<?= h(strtolower($s['name'] . ' ' . $s['folder'])) ?>">
              <a class="name" href="<?= h($scheme . '://' . $s['name'] . '/') ?>" target="_blank" rel="noopener"><?= h($s['name']) ?></a>
              <div class="meta"><?= h($s['folder']) ?><?= $s['mtime'] ? ' · ' . h(ago($s['mtime'])) : '' ?></div>
              <div class="row">
                <span class="chip php">PHP <?= h($s['php']) ?></span>
                <?php if ($s['https']): ?><a class="chip ssl" href="<?= h('https://' . $s['name'] . '/') ?>" target="_blank" rel="noopener">https</a><?php endif; ?>
                <a class="chip" href="<?= h('http://' . $s['name'] . '/') ?>" target="_blank" rel="noopener">http</a>
                <button class="chip" type="button" data-copy="<?= h($scheme . '://' . $s['name'] . '/') ?>">copy</button>
              </div>
            </div>
          <?php endforeach; ?>
        </div>
        <p class="empty" id="noMatch" hidden>No site matches that filter.</p>
      <?php else: ?>
        <div class="empty">No sites yet. Create a folder like <code>www\myapp</code> and it shows up here as
          <code>myapp.<?= h($tld) ?></code>. Or use <b>Quick Create</b> in the varsha panel.</div>
      <?php endif; ?>
    </section>

    <section class="tile span-4 pop" style="animation-delay:.28s">
      <h2>Services <span class="count" id="svcAge">live</span></h2>
      <?php foreach ($services as $s): $up = !empty($states[$s['id']]); ?>
        <div class="svc" data-id="<?= h($s['id']) ?>">
          <span class="dot<?= $up ? ' on' : '' ?>"></span>
          <span class="nm"><?= h($s['name']) ?></span>
          <span class="pt"><?= $s['ports'] ? ':' . h(implode(' :', $s['ports'])) : '' ?></span>
          <span class="st"><?= $up ? 'running' : 'stopped' ?></span>
        </div>
      <?php endforeach; ?>
    </section>

    <section class="tile span-4 sky pop" style="animation-delay:.36s">
      <h2>PHP</h2>
      <p class="big"><?= h(PHP_VERSION) ?></p>
      <p style="margin:0 0 14px;color:var(--muted)"><?= h(PHP_SAPI) ?> · default for sites: <?= h($defaultPhp) ?></p>
      <dl class="kv">
        <?php foreach ($iniRows as $k => $v): ?><dt><?= h($k) ?></dt><dd><?= h($v) ?></dd><?php endforeach; ?>
      </dl>
      <div class="exts">
        <?php foreach ($extensions as $e): ?>
          <span class="ext<?= in_array(strtolower($e), $loaded, true) ? ' on' : '' ?>"><?= h($e === 'Zend OPcache' ? 'opcache' : $e) ?></span>
        <?php endforeach; ?>
      </div>
    </section>

    <section class="tile span-4 pop" style="animation-delay:.44s">
      <h2>Databases</h2>
      <?php if ($databases): foreach ($databases as $db): $port = (int) $db['ports'][0]; $up = !empty($states[$db['id']]); ?>
        <div class="db">
          <b><span class="dot<?= $up ? ' on' : '' ?>"></span><?= h($db['name']) ?></b>
          <div class="code">
            <code>mysql:host=127.0.0.1;port=<?= $port ?></code>
            <button class="chip" type="button" data-copy="<?= h('mysql:host=127.0.0.1;port=' . $port . ';charset=utf8mb4') ?>">copy</button>
          </div>
        </div>
      <?php endforeach; ?>
        <p style="margin:12px 0 0;color:var(--muted);font-size:13px">User <b>root</b>. Set or change its password in the varsha panel.</p>
      <?php else: ?><div class="empty">No database services are enabled.</div><?php endif; ?>
    </section>

    <section class="tile span-4 pop" style="animation-delay:.52s">
      <h2>Quick start</h2>
      <ol class="steps">
        <li>Make a folder in <code>www</code>, e.g. <code>www\shop</code></li>
        <li>Open <b>http://shop.<?= h($tld) ?></b>. If <code>public\</code> exists, it becomes the web root</li>
        <li>For HTTPS, click <b>Trust HTTPS</b> once in the varsha panel</li>
        <li>Pick a PHP version per site in the panel's <b>Sites</b> view</li>
        <li>Mail sent from PHP lands in <b>Mailpit</b>, never on the internet</li>
      </ol>
    </section>

  </main>

  <footer>
    <span><?= h(str_replace('\\', '/', $ROOT)) ?></span>
    <span>varsha · Apache + PHP <?= h(PHP_VERSION) ?> · <a href="/?q=info" target="_blank" rel="noopener">phpinfo</a></span>
  </footer>
</div>
<div class="toast" id="toast">Copied</div>

<script>
(function () {
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function $(id) { return document.getElementById(id); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* ---------- clocks + seconds gauge ---------- */
  var days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  function tick() {
    var d = new Date();
    $('clock').textContent = d.toLocaleTimeString();
    $('bigClock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    $('date').textContent = days[d.getDay()] + ', ' + d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
    var secs = d.getSeconds() + d.getMilliseconds() / 1000;
    $('gaugeFill').style.width = (secs / 60 * 100) + '%';
    $('gaugeLabel').textContent = 'T+' + pad(d.getSeconds()) + 's';
    $('handSec').style.transform = 'rotate(' + (secs * 6) + 'deg)';
    $('handMin').style.transform = 'rotate(' + ((d.getMinutes() + secs / 60) * 6) + 'deg)';
  }
  tick(); setInterval(tick, 200);

  /* ---------- LED ping once a second ---------- */
  var led = $('led');
  if (!reduced) setInterval(function () { led.classList.remove('ping'); void led.offsetWidth; led.classList.add('ping'); }, 1000);

  /* ---------- background: sparks on the dot grid + one live spark per second ---------- */
  var sparks = $('sparks');
  function spot(el) {
    var grid = 18, h = Math.max(document.body.scrollHeight, window.innerHeight);
    el.style.left = Math.round(Math.random() * window.innerWidth / grid) * grid + 'px';
    el.style.top = Math.round(Math.random() * h / grid) * grid + 'px';
  }
  if (!reduced) {
    for (var i = 0; i < 24; i++) {
      var s = document.createElement('div');
      s.className = 'spark'; spot(s);
      s.style.animationDelay = (Math.random() * 6) + 's';
      s.style.animationDuration = (4 + Math.random() * 4) + 's';
      sparks.appendChild(s);
    }
    setInterval(function () {
      var s = document.createElement('div');
      s.className = 'spark live'; spot(s); sparks.appendChild(s);
      setTimeout(function () { s.remove(); }, 2500);
    }, 1000);
  }

  /* ---------- parallax glow (mouse only) ---------- */
  var glow = $('glow');
  if (!reduced && window.matchMedia('(pointer:fine)').matches) {
    window.addEventListener('mousemove', function (e) {
      var x = (e.clientX / window.innerWidth - 0.5) * 36, y = (e.clientY / window.innerHeight - 0.5) * 36;
      glow.style.transform = 'translate(' + x + 'px,' + y + 'px)';
    });
  }

  /* ---------- count-up numbers ---------- */
  document.querySelectorAll('[data-count]').forEach(function (el) {
    var target = parseInt(el.getAttribute('data-count'), 10) || 0;
    if (reduced || target === 0) { el.textContent = target; return; }
    var start = null, dur = 900 + target * 40;
    function step(t) {
      if (start === null) start = t;
      var p = Math.min(1, (t - start) / dur);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    }
    setTimeout(function () { requestAnimationFrame(step); }, 400);
  });

  /* ---------- status terminal types the real state ---------- */
  var lines = <?= json_encode($termLines, defined('JSON_HEX_TAG') ? JSON_HEX_TAG | JSON_HEX_AMP : 0) ?>;
  var term = $('term'), caret = document.createElement('span'), li = 0;
  caret.className = 'caret';
  function typeLine() {
    if (li >= lines.length) { term.appendChild(document.createTextNode('$ ')); term.appendChild(caret); return; }
    var text = lines[li][0], cls = lines[li][1], line = document.createElement('div');
    if (cls) line.className = cls;
    term.appendChild(line); term.appendChild(caret);
    if (reduced) { line.textContent = text; li++; typeLine(); return; }
    var i = 0, iv = setInterval(function () {
      i += 2; line.textContent = text.slice(0, i);
      if (i >= text.length) { clearInterval(iv); li++; setTimeout(typeLine, cls === 'cmd' ? 380 : 120); }
    }, 16);
  }
  setTimeout(typeLine, 500);

  /* ---------- copy buttons ---------- */
  var toast = $('toast'), toastTimer;
  function showToast(text) {
    toast.textContent = text; toast.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 1400);
  }
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { showToast('Copied'); });
      return;
    }
    var area = document.createElement('textarea');  // http://*.test is not a secure context
    area.value = text; document.body.appendChild(area); area.select();
    try { document.execCommand('copy'); showToast('Copied'); } catch (e) { showToast(text); }
    area.remove();
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy]');
    if (btn) { e.preventDefault(); copy(btn.getAttribute('data-copy')); }
  });

  /* ---------- site filter ---------- */
  var search = $('search');
  if (search) {
    search.addEventListener('input', function () {
      var term = search.value.trim().toLowerCase(), shown = 0;
      document.querySelectorAll('#sites .site').forEach(function (el) {
        var hit = el.getAttribute('data-name').indexOf(term) !== -1;
        el.hidden = !hit; if (hit) shown++;
      });
      $('noMatch').hidden = shown !== 0;
    });
  }

  /* ---------- live service lights every 10 s ---------- */
  var total = document.querySelectorAll('.svc').length;
  function refresh() {
    fetch('/?q=status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (states) {
      var up = 0;
      document.querySelectorAll('.svc').forEach(function (row) {
        var on = !!states[row.getAttribute('data-id')], dot = row.querySelector('.dot');
        if (on) up++;
        if (dot.classList.contains('on') !== on) { row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash'); }
        dot.classList.toggle('on', on);
        row.querySelector('.st').textContent = on ? 'running' : 'stopped';
      });
      $('upCount').textContent = up;
      var pill = $('upPill');
      pill.classList.toggle('up', up === total);
      pill.classList.toggle('warn', up === 0);
      $('svcAge').textContent = 'checked ' + new Date().toLocaleTimeString();
    }).catch(function () {});
  }
  setInterval(refresh, 10000);
})();
</script>
</body>
</html>
