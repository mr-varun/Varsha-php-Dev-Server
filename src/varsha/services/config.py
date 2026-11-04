"""Load and validate config/services.json.

Adding a service (Redis, another MySQL version, ...) is a JSON edit only: every behaviour the
manager needs (exe, args, ports, stop method) is described here.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from varsha.errors import ConfigError
from varsha.services.models import Link, ReloadSpec, ServiceDef, StopMethod, StopSpec
from varsha.services.resolve import PLACEHOLDERS, template_fields

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
KNOWN_KEYS = frozenset(
    {"id", "name", "type", "exe", "args", "ports", "config_template", "config_file",
     "data_dir", "init_command", "init_stdin", "log_file", "tools", "password_reset_sql",
     "links", "module", "reload", "autostart", "enabled", "stop"}
)


def load_services(path: Path) -> list[ServiceDef]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Services file not found: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    return parse_services(raw, source=path.name)


def parse_services(raw: Any, source: str = "services.json") -> list[ServiceDef]:
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), list):
        raise ConfigError(f'{source}: expected an object with a "services" list')

    services: list[ServiceDef] = []
    seen_ids: set[str] = set()
    port_users: dict[int, str] = {}
    for index, entry in enumerate(raw["services"]):
        defn = _parse_entry(entry, f"{source}, service #{index + 1}")
        if defn.id in seen_ids:
            raise ConfigError(f"{source}: duplicate service id '{defn.id}'")
        seen_ids.add(defn.id)
        if defn.enabled:
            for port in defn.ports:
                if port in port_users:
                    raise ConfigError(
                        f"{source}: port {port} is used by both '{port_users[port]}' and '{defn.id}'"
                    )
                port_users[port] = defn.id
        services.append(defn)
    return services


def _parse_entry(entry: Any, where: str) -> ServiceDef:
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: expected an object")

    sid = _req_str(entry, "id", where)
    if not ID_RE.match(sid):
        raise ConfigError(f"{where}: id '{sid}' must be lowercase letters, digits, '.', '_' or '-'")
    where = f"{where} ('{sid}')"

    for key in entry:
        if key not in KNOWN_KEYS and not key.startswith("_"):
            logger.warning("%s: unknown key '%s' ignored", where, key)

    stype = _req_str(entry, "type", where).lower()
    exe = _opt_str(entry, "exe", where)
    if exe is None and stype != "builtin":
        raise ConfigError(f'{where}: "exe" is required (only type "builtin" may omit it)')
    if exe is not None:
        _check_relative(exe, "exe", where)

    config_file = _opt_str(entry, "config_file", where)
    data_dir = _opt_str(entry, "data_dir", where)
    log_file = _opt_str(entry, "log_file", where)
    for key, value in (("config_file", config_file), ("data_dir", data_dir), ("log_file", log_file)):
        if value is not None:
            _check_relative(value, key, where)

    defn = ServiceDef(
        id=sid,
        name=_req_str(entry, "name", where),
        type=stype,
        exe=exe,
        args=_str_list(entry, "args", where),
        ports=_ports(entry, where),
        config_template=_opt_str(entry, "config_template", where),
        config_file=config_file,
        data_dir=data_dir,
        init_command=_str_list(entry, "init_command", where),
        init_stdin=_str_list(entry, "init_stdin", where),
        log_file=log_file,
        tools=_tools(entry, where),
        password_reset_sql=_str_list(entry, "password_reset_sql", where),
        links=_links(entry, where),
        module=_module(entry, stype, where),
        reload=_reload(entry, where),
        autostart=_bool(entry, "autostart", where, default=False),
        enabled=_bool(entry, "enabled", where, default=True),
        stop=_stop(entry, where),
    )
    _check_placeholders(defn, where)
    return defn


def _check_relative(value: str, key: str, where: str) -> None:
    path = PureWindowsPath(value)
    if path.drive or path.root:
        raise ConfigError(
            f'{where}: "{key}" must be relative to the varsha folder (got "{value}"); '
            "absolute paths break portability"
        )
    if ".." in path.parts:
        raise ConfigError(f'{where}: "{key}" must stay inside the varsha folder (got "{value}")')


def _check_placeholders(defn: ServiceDef, where: str) -> None:
    available = set(PLACEHOLDERS)
    if defn.config_file is None:
        available.discard("config")
    if defn.data_dir is None:
        available.discard("data_dir")
    if defn.init_stdin and not defn.init_command:
        raise ConfigError(f'{where}: "init_stdin" needs an "init_command" to feed')
    checks: list[tuple[str, tuple[str, ...], set[str]]] = [
        ("args", defn.args, available),
        ("init_command", defn.init_command, available),
        ("init_stdin", tuple(t for t in defn.init_stdin if not t.startswith("sql:")), available),
        ("stop.command", defn.stop.command, available | {"client_cnf"}),
        ("tools", tuple(defn.tools.values()), available),
        ("links", tuple(link.url for link in defn.links), available),
        ("reload.test_command", defn.reload.test_command, available),
        ("password_reset_sql", defn.password_reset_sql, available | {"password"}),
    ]
    for key, templates, allowed in checks:
        for template in templates:
            try:
                unknown = template_fields(template) - allowed
            except ValueError as exc:
                raise ConfigError(f'{where}: bad braces in {key} "{template}": {exc}') from exc
            if unknown:
                names = ", ".join("{" + name + "}" for name in sorted(unknown))
                hint = ""
                if "config" in unknown:
                    hint = ' ({config} needs "config_file")'
                elif "data_dir" in unknown:
                    hint = ' ({data_dir} needs "data_dir")'
                raise ConfigError(f'{where}: unknown placeholder {names} in {key}{hint}')


def _req_str(entry: dict[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{where}: "{key}" must be a non-empty string')
    return value.strip()


def _opt_str(entry: dict[str, Any], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{where}: "{key}" must be a non-empty string or null')
    return value.strip()


def _str_list(entry: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = entry.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f'{where}: "{key}" must be a list of strings')
    return tuple(value)


def _bool(entry: dict[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{where}: "{key}" must be true or false')
    return value


def _ports(entry: dict[str, Any], where: str) -> tuple[int, ...]:
    value = entry.get("ports", [])
    if not isinstance(value, list):
        raise ConfigError(f'{where}: "ports" must be a list of numbers')
    ports: list[int] = []
    for port in value:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f'{where}: invalid port {port!r} (must be 1-65535)')
        if port in ports:
            raise ConfigError(f"{where}: port {port} is listed twice")
        ports.append(port)
    return tuple(ports)


MODULE_RE = re.compile(r"^varsha(\.[a-z_][a-z0-9_]*)+$")


def _module(entry: dict[str, Any], stype: str, where: str) -> str | None:
    module = _opt_str(entry, "module", where)
    if module is None:
        return None
    if stype != "builtin":
        raise ConfigError(f'{where}: "module" is only for type "builtin"')
    if not MODULE_RE.match(module):
        raise ConfigError(f'{where}: "module" must be a varsha module, e.g. "varsha.dns.server"')
    return module


def _reload(entry: dict[str, Any], where: str) -> ReloadSpec:
    value = entry.get("reload")
    if value is None:
        return ReloadSpec()
    if not isinstance(value, dict):
        raise ConfigError(f'{where}: "reload" must be an object like {{"method": "restart"}}')
    method = value.get("method", "restart")
    if method not in ("restart", "apache_signal"):
        raise ConfigError(f'{where}: reload.method must be "restart" or "apache_signal"')
    return ReloadSpec(method=method, test_command=_str_list(value, "test_command", where))


def _tools(entry: dict[str, Any], where: str) -> dict[str, str]:
    value = entry.get("tools", {})
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in value.items()):
        raise ConfigError(f'{where}: "tools" must map names to command paths, e.g. {{"client": "..."}}')
    return dict(value)


def _links(entry: dict[str, Any], where: str) -> tuple[Link, ...]:
    value = entry.get("links", [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f'{where}: "links" must be a list of {{"label": ..., "url": ...}}')
    links = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError(f'{where}: each link must be an object with "label" and "url"')
        links.append(Link(_req_str(item, "label", where), _req_str(item, "url", where)))
    return tuple(links)


def _stop(entry: dict[str, Any], where: str) -> StopSpec:
    value = entry.get("stop")
    if value is None:
        return StopSpec()
    if not isinstance(value, dict):
        raise ConfigError(f'{where}: "stop" must be an object like {{"method": "terminate"}}')
    method_name = value.get("method", StopMethod.TERMINATE.value)
    try:
        method = StopMethod(method_name)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in StopMethod)
        raise ConfigError(f'{where}: stop.method "{method_name}" is not one of: {allowed}') from exc
    command = _str_list(value, "command", where)
    if method is StopMethod.COMMAND and not command:
        raise ConfigError(f'{where}: stop.method "command" needs a non-empty stop.command')
    also_kill = tuple(name.lower() for name in _str_list(value, "also_kill", where))
    return StopSpec(method=method, command=command, also_kill=also_kill)
