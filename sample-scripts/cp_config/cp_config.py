#!/usr/bin/env python3
# pylint: disable=too-many-lines
"""Get or set Crosswork Planning collector configuration.

``--get FILE`` writes the gateway envelope from
GET /cp/apiGateway/collector/v1/configurations (Application Note §6.8).

``--set FILE`` reads that envelope, or a subset of it, and creates only
objects that are not already on the controller (Application Note §§3.1,
7.1, 7.2, 8.2, 8.5). Lists omitted from the file are left untouched.
Networks, schedulers, SNMP groups, auth groups, node profiles, and agents
that already exist are not modified. There is no public API to create
SNMP groups, auth groups, node profiles, or traffic agents, and attaching
an existing traffic agent retargets the current poller, so those creates
are rejected before any write.

A new network is created from the collectors listed for that network, which
may be a subset of a full collection (for example only TOPO_IGP). A
scheduler is created only for a network created by the same ``--set`` run.
When the file sets ``active`` to false, the scheduler is paused after
create because the gateway still starts it active.

This file is standalone. Authentication and HTTP calls are implemented
here and do not import the archive scripts.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import ssl
import sys
from urllib.parse import quote

import requests
import urllib3
from urllib3.util.ssl_ import create_urllib3_context

DEFAULT_PORT = 30603
DEFAULT_TIMEOUT = 60
# Environment variable names. These strings are not secret values.
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"  # nosec B105

_MAX_TEXT_BYTES = 16 * 1024 * 1024
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_SECRET_KEY = re.compile(
    r"(password|secret|token|community|authorization|credential)",
    re.IGNORECASE,
)

CONFIG_KEYS = (
    "snmpGroupDataList",
    "authGroupDataList",
    "nodeProfileDataList",
    "agentDataList",
    "networkDataList",
    "schedulerConfigDataList",
)

READONLY_KINDS = {
    "snmpGroupDataList": "SNMP group",
    "authGroupDataList": "auth group",
    "nodeProfileDataList": "node profile",
    "agentDataList": "agent",
}

TASK_FIELDS = (
    "taskName",
    "collectorId",
    "collectorName",
    "collectorType",
    "collect",
    "aggreagate",
    "archive",
)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or an API call fails."""


class _TLSAdapter(requests.adapters.HTTPAdapter):
    """HTTPS adapter that applies a timeout and requires TLS 1.3."""

    def __init__(self, timeout: int, verify_ssl: bool = True):
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        if self._verify_ssl:
            context = create_urllib3_context()
        else:
            context = create_urllib3_context(cert_reqs=ssl.CERT_NONE)
            context.check_hostname = False
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(*args, **kwargs)


def _create_session(*, verify_ssl: bool, timeout: int) -> requests.Session:
    """Create an HTTPS session. Certificate checks stay on unless disabled.

    Ambient proxy and netrc settings are ignored so the password and bearer
    token are not sent through an environment the operator did not name.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise CrossworkAuthError("timeout must be an integer >= 1")
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    session.trust_env = False
    session.cw_timeout = timeout
    session.mount("https://", _TLSAdapter(timeout, verify_ssl))
    return session


def _redact_obj(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_obj(item)
        return redacted
    if isinstance(value, list):
        return [_redact_obj(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    """Hide secret-looking fields before an error is printed."""
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return json.dumps(_redact_obj(parsed))
    return re.sub(
        r"(?i)((?:password|secret|token|community|authorization|credential)"
        r"(?:\s*[:=]\s*))(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1***",
        text,
    )


def _error_detail(value) -> str:
    """Redact and shorten a server payload before it is printed."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value)
        except (TypeError, ValueError):
            value = str(value)
    text = _redact_text(value)
    if len(text) > 500:
        return text[:500] + "..."
    return text


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError when an HTTP response failed."""
    if resp.ok:
        return
    if resp.is_redirect or resp.is_permanent_redirect:
        raise CrossworkAuthError(
            f"{context} returned HTTP {resp.status_code} redirect; "
            "redirects are not followed"
        )
    body = _error_detail((resp.text or "").strip())
    if not body:
        body = "<empty response body>"
    reason = getattr(resp, "reason", "")
    status = f"HTTP {resp.status_code}"
    if reason:
        status = f"{status} {reason}"
    raise CrossworkAuthError(f"{context} returned {status}: {body}")


def _http(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Send one HTTPS request. Redirects are not followed."""
    if not url.startswith("https://"):
        raise CrossworkAuthError("refusing a non-HTTPS controller URL")
    kwargs.setdefault("timeout", session.cw_timeout)
    kwargs.setdefault("verify", session.verify)
    kwargs.setdefault("allow_redirects", False)
    return session.request(method, url, **kwargs)


def get_ticket(session: requests.Session, base_url: str, username: str, password: str) -> str:
    """POST credentials to Crosswork SSO and return a ticket-granting ticket."""
    url = f"{base_url}/crosswork/sso/v1/tickets"
    resp = _http(
        session,
        "POST",
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check_response(resp, "get_ticket")
    location = resp.headers.get("Location", "")
    if location:
        ticket = location.rstrip("/").split("/")[-1]
    else:
        try:
            ticket = resp.json().get("ticket", "")
        except ValueError:
            ticket = resp.text.strip()
    if not ticket:
        detail = _error_detail(resp.text[:300])
        raise CrossworkAuthError(f"Could not extract ticket from response: {detail}")
    return ticket


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token."""
    url = f"{base_url}/crosswork/sso/v2/tickets/jwt"
    resp = _http(
        session,
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"tgt": ticket, "service": f"{base_url}/app-dashboard"},
    )
    check_response(resp, "get_token")
    try:
        token = resp.json().get("token", "")
    except ValueError:
        token = resp.text.strip()
    if not token:
        detail = _error_detail(resp.text[:300])
        raise CrossworkAuthError(f"Could not extract token from response: {detail}")
    return token


def get_jwt(  # pylint: disable=too-many-arguments
    ip: str,
    username: str,
    password: str,
    *,
    port: int = DEFAULT_PORT,
    verify_ssl: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Authenticate to Crosswork and return the JWT string."""
    session = _create_session(verify_ssl=verify_ssl, timeout=timeout)
    base_url = _origin(ip, port)
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def _resolve_credentials(args) -> tuple:
    """Resolve username and password from args, the environment, or a prompt.

    Priority is the CLI flag, then the environment variable, then a prompt.
    The password is read with getpass and is not printed.
    """
    username = args.username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")
    password = args.password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")
    if not username or not password:
        raise CrossworkAuthError("username and password are required")
    return username, password


def get_auth_headers(token: str) -> dict:
    """Return the bearer headers for a gateway call."""
    if not token or not isinstance(token, str):
        raise CrossworkAuthError("JWT is empty")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _host_for_url(ip: str) -> str:
    """Return a host safe to place in an https URL."""
    if not isinstance(ip, str):
        raise CrossworkAuthError("controller address must be a string")
    if not ip or ip != ip.strip() or any(char in ip for char in " /?#@\\"):
        raise CrossworkAuthError(f"invalid controller address: {ip!r}")
    host = ip
    bracketed = host.startswith("[") and host.endswith("]")
    if bracketed:
        host = host[1:-1]
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if bracketed and not isinstance(parsed, ipaddress.IPv6Address):
        raise CrossworkAuthError(f"invalid controller address: {ip!r}")
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{parsed.compressed}]"
    if parsed is not None:
        return str(parsed)
    labels = host.split(".")
    if (
        len(host) > 253
        or not labels
        or any(_HOST_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise CrossworkAuthError(f"invalid controller address: {ip!r}")
    return host


def _origin(ip: str, port: int) -> str:
    """Return https://host:port after checking the host and port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise CrossworkAuthError("port must be an integer from 1 to 65535")
    return f"https://{_host_for_url(ip)}:{port}"


def _read_text_file(path: str) -> str:
    with open(path, "rb") as handle:
        blob = handle.read(_MAX_TEXT_BYTES + 1)
    if len(blob) > _MAX_TEXT_BYTES:
        raise CrossworkAuthError(f"file exceeds {_MAX_TEXT_BYTES} bytes: {path}")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossworkAuthError(f"file is not UTF-8 text: {path}") from exc


def _warn_if_shared(path: str) -> None:
    mode = os.stat(path).st_mode
    if mode & 0o077:
        shown = oct(mode & 0o777)
        print(
            f"Warning: {path} is accessible to other users (mode {shown}). "
            "Restrict it with chmod 600.",
            file=sys.stderr,
        )


def _default_jwt_path(ip: str) -> str:
    """Return the JWT path cw_get_jwt.py writes for this host."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def _read_jwt_file(jwt_path: str) -> str:
    if not os.path.isfile(jwt_path):
        raise CrossworkAuthError(f"JWT file not found: {jwt_path}")
    _warn_if_shared(jwt_path)
    token = _read_text_file(jwt_path).strip()
    if not token:
        raise CrossworkAuthError(f"JWT file is empty: {jwt_path}")
    print(f"Using JWT from {jwt_path}")
    return token


def _token_from_args(args, verify_ssl: bool) -> tuple[str, str | None]:
    """Return the bearer token and the JWT file it came from, if any."""
    if args.jwt:
        jwt_path = os.path.expanduser(args.jwt)
        return _read_jwt_file(jwt_path), jwt_path
    if args.username is None and args.password is None:
        default_jwt = _default_jwt_path(args.ip)
        if os.path.isfile(default_jwt):
            return _read_jwt_file(default_jwt), default_jwt
    if args.password is not None:
        print(
            "Warning: --password is visible in the process list. "
            f"Prefer {ENV_PASSWORD} or the interactive prompt.",
            file=sys.stderr,
        )
    username, password = _resolve_credentials(args)
    print(f"Authenticating to {args.ip}...")
    token = get_jwt(
        args.ip,
        username,
        password,
        port=args.port,
        verify_ssl=verify_ssl,
        timeout=args.timeout,
    )
    return token, None


def gateway_url(ip: str, port: int) -> str:
    """Return the collector API gateway URL for this controller."""
    return f"{_origin(ip, port)}/cp/apiGateway"


def request_json(  # pylint: disable=too-many-arguments
    session: requests.Session,
    method: str,
    url: str,
    token: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
):
    """Send one API call and return the unwrapped ``data`` value."""
    headers = get_auth_headers(token)
    kwargs = {"headers": headers, "params": params}
    if body is not None:
        headers = {**headers, "Content-Type": "application/json"}
        kwargs["headers"] = headers
        kwargs["json"] = body
    resp = _http(session, method, url, **kwargs)
    check_response(resp, f"{method} {url}")
    if not resp.content or not resp.content.strip():
        return None
    try:
        payload = resp.json()
    except ValueError as exc:
        raise CrossworkAuthError(f"{method} {url} returned non-JSON content") from exc
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        if errors:
            raise CrossworkAuthError(f"{method} {url} returned errors: {errors}")
        if "data" in payload:
            return payload["data"]
    return payload


def load_config_document(path: str) -> dict:
    """Load a full configuration envelope or a subset of its lists."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise CrossworkAuthError(f"configuration file not found: {path}")
    try:
        payload = json.loads(_read_text_file(expanded))
    except json.JSONDecodeError as exc:
        raise CrossworkAuthError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CrossworkAuthError("configuration file must be a JSON object")

    body = payload
    data = payload.get("data")
    if isinstance(data, dict) and any(key in data for key in CONFIG_KEYS):
        body = data

    unknown = [key for key in body if key not in CONFIG_KEYS]
    if unknown:
        raise CrossworkAuthError(
            "unknown configuration keys: " + ", ".join(sorted(unknown))
        )

    selected = {}
    for key in CONFIG_KEYS:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, list):
            raise CrossworkAuthError(f"{key} must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise CrossworkAuthError(f"{key} entries must be JSON objects")
        selected[key] = value
    if not selected or not any(selected.values()):
        raise CrossworkAuthError("configuration file has no objects to apply")
    return selected


def _index_by_name(items: list) -> dict:
    indexed = {}
    for item in items or []:
        name = item.get("name")
        if isinstance(name, str) and name:
            indexed[name] = item
    return indexed


def _require_name(item: dict, kind: str) -> str:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CrossworkAuthError(f"{kind} is missing a name")
    return name


def _sanitize_params(params):
    """Return collector params as a JSON string, without demand-step ids."""
    if params is None:
        raise CrossworkAuthError("collector params are required")
    if isinstance(params, dict):
        obj = params
    elif isinstance(params, str):
        text = params.strip()
        if not text:
            raise CrossworkAuthError("collector params are empty")
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return params
    else:
        raise CrossworkAuthError("collector params must be a JSON string or object")
    if isinstance(obj, dict):
        for step in obj.get("demandSteps") or []:
            if isinstance(step, dict):
                step.pop("id", None)
        return json.dumps(obj)
    return params if isinstance(params, str) else json.dumps(obj)


def _collector_body(collector: dict) -> dict:
    name = _require_name(collector, "collector")
    collector_type = collector.get("type")
    if not isinstance(collector_type, str) or not collector_type:
        raise CrossworkAuthError(f"collector {name} is missing type")
    if collector.get("agents"):
        raise CrossworkAuthError(
            f"collector {name} references a traffic agent. "
            "Creating or sharing an agent retargets the current traffic poller, "
            "and the public API cannot create a new agent. "
            "Omit agents from the test configuration."
        )
    body = {
        "name": name,
        "type": collector_type,
        "consolidationType": collector.get("consolidationType") or "DARE",
        "configPhase": collector.get("configPhase"),
        "params": _sanitize_params(collector.get("params")),
        "timeout": collector.get("timeout", 60),
        "agents": [],
        "sourceCollector": None,
    }
    if not body["configPhase"]:
        raise CrossworkAuthError(f"collector {name} is missing configPhase")
    source = collector.get("sourceCollector")
    if isinstance(source, dict) and source.get("name"):
        source_type = source.get("type")
        if not source_type:
            raise CrossworkAuthError(f"collector {name} sourceCollector is missing type")
        body["sourceCollector"] = {"name": source["name"], "type": source_type}
    return body


def _profile_body(spec: dict, profiles_by_name: dict) -> dict:
    profile = spec.get("nodeProfileData") or {}
    if not isinstance(profile, dict):
        raise CrossworkAuthError(f"network {spec.get('name')} nodeProfileData must be an object")
    name = profile.get("name")
    if not isinstance(name, str) or not name:
        raise CrossworkAuthError(f"network {spec.get('name')} is missing nodeProfileData.name")
    current = profiles_by_name.get(name)
    if current is None:
        raise CrossworkAuthError(
            f"node profile {name!r} is not on the controller. "
            "The public API cannot create node profiles; reference an existing profile."
        )
    use_filter = profile.get("useNodeListAsIncludeFilter")
    if use_filter is None:
        use_filter = current.get("useNodeListAsIncludeFilter", False)
    return {
        "id": current["id"],
        "name": current["name"],
        "useNodeListAsIncludeFilter": bool(use_filter),
    }


def _task_body(task: dict, name_to_id: dict) -> dict:
    if not isinstance(task, dict):
        raise CrossworkAuthError("scheduler task must be an object")
    prepared = dict(task)
    if "aggreagate" not in prepared and "aggregate" in prepared:
        prepared["aggreagate"] = prepared["aggregate"]
    body = {field: prepared.get(field) for field in TASK_FIELDS}
    if not body.get("taskName"):
        raise CrossworkAuthError("scheduler task is missing taskName")
    collector_name = body.get("collectorName")
    if collector_name:
        collector_id = name_to_id.get(collector_name)
        if collector_id is None:
            raise CrossworkAuthError(
                f"scheduler task {body['taskName']} references unknown collector {collector_name}"
            )
        body["collectorId"] = collector_id
    else:
        body["collectorId"] = None
        body["collectorName"] = None
    for flag in ("collect", "aggreagate", "archive"):
        if body.get(flag) is None:
            body[flag] = False
    return body


def _needs_archive(scheduler: dict) -> bool:
    for task in scheduler.get("taskConfigDataList") or []:
        if isinstance(task, dict) and task.get("archive"):
            return True
    return False


def _plan_readonly(current: dict, requested: dict) -> tuple[list, list]:
    unchanged = []
    unsupported = []
    for key, kind in READONLY_KINDS.items():
        if key not in requested:
            continue
        existing = _index_by_name(current.get(key) or [])
        seen = set()
        for item in requested[key]:
            name = _require_name(item, kind)
            if name in seen:
                raise CrossworkAuthError(
                    f"duplicate {kind} name {name!r} in the configuration file"
                )
            seen.add(name)
            entry = {"kind": kind, "name": name}
            if name in existing:
                entry["reason"] = "already exists"
                unchanged.append(entry)
            else:
                entry["reason"] = "no public create API"
                unsupported.append(entry)
    return unchanged, unsupported


def _plan_networks(current: dict, requested: dict, unchanged: list) -> tuple[list, dict]:
    current_networks = _index_by_name(current.get("networkDataList") or [])
    profiles = _index_by_name(current.get("nodeProfileDataList") or [])
    networks = []
    seen_networks = set()
    for spec in requested.get("networkDataList") or []:
        name = _require_name(spec, "network")
        if name in seen_networks:
            raise CrossworkAuthError(
                f"duplicate network name {name!r} in the configuration file"
            )
        seen_networks.add(name)
        if name in current_networks:
            unchanged.append({
                "kind": "network",
                "name": name,
                "reason": "already exists",
            })
            continue
        collectors = spec.get("collectors") or []
        if not isinstance(collectors, list) or not collectors:
            raise CrossworkAuthError(f"network {name} has no collectors to create")
        networks.append({
            "name": name,
            "profile": _profile_body(spec, profiles),
            "collectors": [_collector_body(collector) for collector in collectors],
            "archive": False,
        })
    return networks, current_networks


def _plan_schedulers(
    current: dict,
    requested: dict,
    unchanged: list,
    networks: list,
    current_networks: dict,
) -> list:
    current_schedulers = {
        (item.get("networkName"), item.get("name"))
        for item in (current.get("schedulerConfigDataList") or [])
        if isinstance(item, dict)
    }
    planned_names = {item["name"] for item in networks}
    schedulers = []
    seen_schedulers = set()
    for spec in requested.get("schedulerConfigDataList") or []:
        name = _require_name(spec, "scheduler")
        network_name = spec.get("networkName")
        if not isinstance(network_name, str) or not network_name:
            raise CrossworkAuthError(f"scheduler {name} is missing networkName")
        key = (network_name, name)
        if key in seen_schedulers:
            raise CrossworkAuthError(
                f"duplicate scheduler {name!r} for network {network_name!r}"
            )
        seen_schedulers.add(key)
        if network_name not in planned_names:
            if network_name in current_networks or key in current_schedulers:
                unchanged.append({
                    "kind": "scheduler",
                    "name": name,
                    "networkName": network_name,
                    "reason": "network already exists",
                })
                continue
            raise CrossworkAuthError(
                f"scheduler {name} references network {network_name}, "
                "which is neither on the controller nor created by this file"
            )
        tasks = spec.get("taskConfigDataList") or []
        if not isinstance(tasks, list) or not tasks:
            raise CrossworkAuthError(f"scheduler {name} has no tasks")
        if not spec.get("runNow") and not spec.get("cronExpression"):
            raise CrossworkAuthError(f"scheduler {name} is missing cronExpression")
        for planned in networks:
            if planned["name"] == network_name and _needs_archive(spec):
                planned["archive"] = True
        schedulers.append({
            "name": name,
            "networkName": network_name,
            "cronExpression": spec.get("cronExpression"),
            "runNow": bool(spec.get("runNow", False)),
            "active": bool(spec.get("active", False)),
            "tasks": tasks,
        })
    return schedulers


def plan_changes(current: dict, requested: dict) -> dict:
    """Decide creates vs leaves-unchanged. Does not call the network.

    Existing names are never updates. New readonly objects are rejected
    here so ``--set`` fails before it writes.
    """
    if not isinstance(current, dict):
        raise CrossworkAuthError("controller configuration payload is not an object")
    unchanged, unsupported = _plan_readonly(current, requested)
    networks, current_networks = _plan_networks(current, requested, unchanged)
    schedulers = _plan_schedulers(
        current, requested, unchanged, networks, current_networks
    )
    if unsupported:
        names = ", ".join(f"{item['kind']} {item['name']}" for item in unsupported)
        raise CrossworkAuthError(
            "these objects are not on the controller and have no public create API: "
            + names
        )
    return {"unchanged": unchanged, "networks": networks, "schedulers": schedulers}


def _archive_names(session, gateway: str, token: str) -> set[str]:
    payload = request_json(session, "GET", f"{gateway}/v1/archives", token)
    archives = []
    if isinstance(payload, dict):
        archives = payload.get("archives") or []
    elif isinstance(payload, list):
        archives = payload
    return {item.get("name") for item in archives if isinstance(item, dict)}


def _create_archive(session, gateway: str, token: str, name: str) -> None:
    request_json(
        session,
        "PUT",
        f"{gateway}/v1/archives",
        token,
        body={"name": name},
    )


def _create_network(session, gateway: str, token: str, planned: dict) -> dict:
    name = planned["name"]
    draft = request_json(
        session,
        "POST",
        f"{gateway}/collector/v1/networks",
        token,
        body={
            "name": name,
            "draft": True,
            "nodeProfileData": planned["profile"],
            "draftConfig": "[]",
        },
    )
    if not isinstance(draft, dict) or draft.get("id") is None:
        raise CrossworkAuthError(f"create network {name} did not return an id")
    network_id = draft["id"]
    try:
        finalized = request_json(
            session,
            "PUT",
            f"{gateway}/collector/v1/networks/{quote(str(network_id), safe='')}",
            token,
            body={
                "id": network_id,
                "name": name,
                "draft": False,
                "nodeProfileData": planned["profile"],
                "collectors": planned["collectors"],
            },
        )
    except CrossworkAuthError:
        _delete_quiet(session, gateway, token, network_id)
        raise
    details = (finalized or {}).get("responseDetails") or []
    failed = [item for item in details if not item.get("status")]
    if failed:
        _delete_quiet(session, gateway, token, network_id)
        raise CrossworkAuthError(
            f"network {name} collector create failed: {_error_detail(failed)}"
        )
    try:
        collectors = request_json(
            session,
            "GET",
            f"{gateway}/collector/v1/networks/{quote(str(network_id), safe='')}/collectors",
            token,
        )
    except CrossworkAuthError:
        _delete_quiet(session, gateway, token, network_id)
        raise
    if not isinstance(collectors, list):
        _delete_quiet(session, gateway, token, network_id)
        raise CrossworkAuthError(f"network {name} collector list was not returned")
    return {
        "id": network_id,
        "name": name,
        "collectors": {item["name"]: item["id"] for item in collectors if item.get("name")},
    }


def _delete_network(session, gateway: str, token: str, network_id) -> None:
    url = f"{gateway}/collector/v1/networks/{quote(str(network_id), safe='')}"
    resp = _http(session, "DELETE", url, headers=get_auth_headers(token))
    if resp.status_code not in (200, 202, 204):
        check_response(resp, f"DELETE {url}")


def _delete_quiet(session, gateway: str, token: str, network_id) -> None:
    """Best-effort removal of a network created during a failed --set."""
    try:
        _delete_network(session, gateway, token, network_id)
    except (CrossworkAuthError, requests.RequestException, OSError, ValueError) as exc:
        print(
            f"Warning: failed to remove incomplete network {network_id}: {exc}",
            file=sys.stderr,
        )


def _create_scheduler(session, gateway: str, token: str, planned: dict, network: dict) -> dict:
    tasks = [_task_body(task, network["collectors"]) for task in planned["tasks"]]
    created = request_json(
        session,
        "POST",
        f"{gateway}/collector/v1/networks/{quote(str(network['id']), safe='')}/schedulers",
        token,
        body={
            "networkId": network["id"],
            "name": planned["name"],
            "runNow": planned["runNow"],
            "active": planned["active"],
            "cronExpression": planned["cronExpression"],
            "taskConfigDataList": tasks,
        },
    )
    if not isinstance(created, dict) or created.get("id") is None:
        raise CrossworkAuthError(f"scheduler {planned['name']} did not return an id")
    if created.get("status") is False:
        detail = created.get("errorDetails") or created
        raise CrossworkAuthError(
            f"scheduler {planned['name']} was rejected: {_error_detail(detail)}"
        )
    scheduler_id = created["id"]
    active = planned["active"]
    if not active:
        request_json(
            session,
            "PUT",
            (
                f"{gateway}/collector/v1/networks/{quote(str(network['id']), safe='')}"
                f"/schedulers/{quote(str(scheduler_id), safe='')}/actions"
            ),
            token,
            params={"actionName": "PAUSE_JOB"},
        )
        listed = request_json(
            session,
            "GET",
            f"{gateway}/collector/v1/networks/{quote(str(network['id']), safe='')}/schedulers",
            token,
        )
        match = next(
            (
                item for item in (listed or [])
                if str(item.get("id")) == str(scheduler_id)
            ),
            None,
        )
        if match is None:
            raise CrossworkAuthError(f"scheduler {planned['name']} was not listed after create")
        active = bool(match.get("active"))
        if active:
            raise CrossworkAuthError(
                f"scheduler {planned['name']} is still active after PAUSE_JOB"
            )
    return {
        "id": scheduler_id,
        "name": planned["name"],
        "networkName": planned["networkName"],
        "active": active,
    }


def apply_plan(session, gateway: str, token: str, planned: dict) -> list[dict]:
    """Create the planned networks, archives, and schedulers."""
    created = []
    archives = _archive_names(session, gateway, token)
    networks_by_name = {}
    for network in planned["networks"]:
        if network["archive"] and network["name"] not in archives:
            _create_archive(session, gateway, token, network["name"])
            archives.add(network["name"])
            created.append({"kind": "archive", "name": network["name"]})
        result = _create_network(session, gateway, token, network)
        networks_by_name[result["name"]] = result
        created.append({
            "kind": "network",
            "name": result["name"],
            "id": result["id"],
            "collectors": result["collectors"],
        })
    for scheduler in planned["schedulers"]:
        network = networks_by_name[scheduler["networkName"]]
        try:
            result = _create_scheduler(session, gateway, token, scheduler, network)
        except CrossworkAuthError as exc:
            _delete_quiet(session, gateway, token, network["id"])
            raise CrossworkAuthError(
                f"{exc}. Removed incomplete network {network['name']} "
                f"(id {network['id']})."
            ) from exc
        created.append({
            "kind": "scheduler",
            "name": result["name"],
            "id": result["id"],
            "networkName": result["networkName"],
            "active": result["active"],
        })
    return created


def write_config_file(path: str, payload: dict) -> None:
    """Write configuration JSON with owner-only permissions.

    The payload includes encrypted SNMP and login fields from the controller.
    """
    expanded = os.path.expanduser(path)
    parent = os.path.dirname(expanded) or "."
    if not os.path.isdir(parent):
        raise CrossworkAuthError(f"directory not found: {parent}")
    text = json.dumps(payload, indent=2) + "\n"
    temporary = expanded + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise CrossworkAuthError(
            f"could not create a private file at {temporary}: {exc.strerror}"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, expanded)
    os.chmod(expanded, 0o600)


def get_configurations(session, gateway: str, token: str) -> dict:
    """Return the collector configuration envelope from the gateway."""
    url = f"{gateway}/collector/v1/configurations"
    headers = get_auth_headers(token)
    resp = _http(session, "GET", url, headers=headers)
    check_response(resp, "GET configurations")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise CrossworkAuthError("GET configurations returned non-JSON content") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise CrossworkAuthError("GET configurations returned an unexpected payload")
    return payload


def run_get(session, gateway: str, token: str, path: str) -> None:
    """Save the controller configuration to an owner-only JSON file."""
    payload = get_configurations(session, gateway, token)
    write_config_file(path, payload)
    data = payload["data"]
    print(f"Wrote {os.path.expanduser(path)}")
    for key in CONFIG_KEYS:
        items = data.get(key) or []
        print(f"  {key}: {len(items)}")
    print(
        "The file contains encrypted SNMP and login fields returned by the controller. "
        "It is readable only by this user."
    )


def run_set(session, gateway: str, token: str, path: str) -> None:
    """Create objects from a configuration file that are not already present."""
    requested = load_config_document(path)
    current = get_configurations(session, gateway, token)["data"]
    planned = plan_changes(current, requested)
    created = apply_plan(session, gateway, token, planned)
    summary = {"unchanged": planned["unchanged"], "created": created}
    print(json.dumps(summary, indent=2))
    print(
        f"Unchanged {len(planned['unchanged'])}; created {len(created)}. "
        "Existing networks and schedulers were not modified."
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="cp_config.py",
        description=(
            "Get or set Crosswork Planning collector configuration. "
            "Pass the connection options first, then exactly one of --get or --set."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credentials: ~/.crosswork/<ip>.jwt when --username, --password, and\n"
            "--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,\n"
            f"{ENV_USERNAME}/{ENV_PASSWORD}, or an interactive prompt.\n"
            "A password passed with --password is visible in the process list.\n"
            "\n"
            "TLS certificate verification is on unless -k is passed.\n"
            "Connections offer TLS 1.3 only.\n"
            "\n"
            "--get writes GET /collector/v1/configurations (Application Note §6.8).\n"
            "--set accepts that file or a subset of its lists. Omitted lists are not\n"
            "changed. Objects that already exist are left unchanged. A new network is\n"
            "created from the collectors in the file, so the file can describe a\n"
            "subset such as a single TOPO_IGP collector. Schedulers are created only\n"
            "for networks created by the same --set, and are paused when active is\n"
            "false.\n"
            "\n"
            "Examples:\n"
            "  %(prog)s --ip 198.18.134.229 -k --get cp-configurations.json\n"
            "  %(prog)s --ip 198.18.134.229 -k --set cpconfig-test-subset.json"
        ),
    )
    connection = parser.add_argument_group("connection")
    connection.add_argument("--ip", required=True, help="Crosswork controller IP or hostname")
    connection.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTPS port (default: {DEFAULT_PORT})",
    )
    connection.add_argument(
        "--username", "-u",
        default=None,
        help=f"Username (or set {ENV_USERNAME})",
    )
    connection.add_argument(
        "--password", "-p",
        default=None,
        help=f"Password (or set {ENV_PASSWORD}; prompted if omitted)",
    )
    connection.add_argument(
        "--jwt",
        help="Path to a JWT file (default when no credentials are given: ~/.crosswork/<ip>.jwt)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--get",
        metavar="FILE",
        help="Write the collector configuration to this JSON file",
    )
    mode.add_argument(
        "--set",
        metavar="FILE",
        help="Apply a configuration JSON file. A subset of the configuration lists is enough",
    )
    parser.add_argument(
        "-k", "--insecure",
        action="store_true",
        help="Disable TLS certificate verification",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Get or set collector configuration."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if isinstance(args.timeout, bool) or args.timeout < 1:
        parser.error("--timeout must be >= 1")
    if isinstance(args.port, bool) or not 1 <= args.port <= 65535:
        parser.error("--port must be an integer from 1 to 65535")
    try:
        _host_for_url(args.ip)
    except CrossworkAuthError as exc:
        parser.error(str(exc))
    verify_ssl = not args.insecure
    session = _create_session(verify_ssl=verify_ssl, timeout=args.timeout)
    jwt_path = None
    try:
        token, jwt_path = _token_from_args(args, verify_ssl)
        gateway = gateway_url(args.ip, args.port)
        if args.get:
            run_get(session, gateway, token, args.get)
        else:
            run_set(session, gateway, token, args.set)
    except (CrossworkAuthError, requests.RequestException, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if jwt_path and "HTTP 401" in str(exc):
            print(
                f"The JWT in {jwt_path} was rejected. Refresh it, or pass "
                "--username to authenticate with a password instead.",
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
