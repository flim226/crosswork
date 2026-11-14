import argparse
import base64
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import urllib3

# Crosswork Planning Startup Script to Retrieve Plan from CNC
# To be used in Crosswork Planning 7.2 Collector > Startup script > Script
# Connection settings - modify these values as needed

CROSSWORK_IP = "198.18.134.219"
CROSSWORK_USERNAME = "admin"
CROSSWORK_PASSWORD = "PASSWORD"
PLAN_VERSION = ""
TMP_PLANFILE = "planfile.pln"  # Created in a private per-run temp directory unless --tmpfile is given
INSECURE = False
GEOLOC = False
BASE_PORT = 30603
CONNECT_TIMEOUT = 20
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
ENV_LOG_LEVEL = "INFO"
SCRIPT_VERSION = "1.5"
JWT_EXPIRY_WARNING_SECONDS = 300

TRIM_FILES = {
    "trim_include.txt": "include node table",
    "trim_exclude.txt": "exclude node table",
    "trim_include_regex.txt": "include nodes regex",
    "trim_exclude_regex.txt": "exclude nodes regex",
}

# Plan inventory counter name -> plan table name
INVENTORY_TABLES = [
    ("nodes", "Nodes"),
    ("circuits", "Circuits"),
    ("interfaces", "Interfaces"),
    ("cs_sr_policies", "CircuitStyleSRPolicies"),
    ("demands", "Demands"),
    ("flex_algos", "FlexAlgorithms"),
    ("node_flex_algos", "NodeFlexAlgorithms"),
    ("srlgs", "SRLGs"),
]
INVENTORY_ORDER = [
    "nodes", "circuits", "interfaces", "sr_policies", "sr_policies_srv4", "sr_policies_srv6",
    "cs_sr_policies", "rsvp_lsps", "other_lsps", "lsps_total", "demands", "flex_algos",
    "node_flex_algos", "srlgs", "pln_format_version",
]
INVENTORY_DELTA_KEYS = ["nodes", "circuits", "interfaces", "sr_policies", "rsvp_lsps", "demands", "srlgs"]
SUMMARY_INVENTORY_KEYS = [
    "nodes", "circuits", "interfaces", "sr_policies", "rsvp_lsps", "demands", "flex_algos", "srlgs",
]

EXIT_CODES = {
    "unknown": 1,
    "config": 2,
    "auth": 3,
    "connect": 4,
    "timeout": 5,
    "http": 6,
    "parse": 7,
    "subprocess": 8,
    "io": 9,
}

RUN_ID = uuid.uuid4().hex[:8]

logger = logging.getLogger(__name__)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""
    def __init__(self, message: str, status: Optional[int] = None, context: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.context = context


class ConfigError(ValueError):
    """Raised when script arguments or constants are inconsistent."""


class CommandError(RuntimeError):
    """Raised when an external tool (trim_nodes, mate_convert) fails."""


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID
        return True


class _UTCFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"


def setup_logging(debug: bool) -> None:
    """Log UTC timestamps, level, and run ID on every line."""
    level_name = "DEBUG" if debug else os.environ.get(ENV_LOG_LEVEL, "INFO").upper()
    level = getattr(logging, level_name, None)
    invalid_level = not isinstance(level, int)
    if invalid_level:
        level = logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(_UTCFormatter("%(asctime)s %(levelname)-5s run=%(run_id)s %(message)s"))
    handler.addFilter(_RunIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    if invalid_level:
        logger.warning(kv(phase="start", invalid_log_level=level_name, using="INFO"))


def kv(**fields: object) -> str:
    """Format fields as key=value pairs; None becomes n/a, values with spaces are quoted."""
    parts = []
    for key, value in fields.items():
        if value is None:
            text = "n/a"
        elif isinstance(value, bool):
            text = str(value).lower()
        else:
            text = str(value)
        if text == "" or any(c in text for c in ' \t\n"'):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


class RunStats:
    """Durations and fields collected from each phase, used for the summary line."""
    def __init__(self) -> None:
        self.durations: Dict[str, int] = {}
        self.fields: Dict[str, Dict[str, object]] = {}


def classify_error(exc: BaseException) -> str:
    """Map an exception to an error_class used in logs and exit codes."""
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, CrossworkAuthError):
        if exc.status in (401, 403) or exc.context in ("get_ticket", "get_token", "jwt"):
            return "auth"
        return "http"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connect"
    if isinstance(exc, ValueError):
        return "parse"
    if isinstance(exc, requests.RequestException):
        return "http"
    if isinstance(exc, CommandError):
        return "subprocess"
    if isinstance(exc, OSError):
        return "io"
    return "unknown"


@contextmanager
def phase(stats: RunStats, name: str, **fields: object) -> Iterator[Dict[str, object]]:
    """Time a phase and log one status line; the yielded dict adds fields to that line."""
    key = f"{name}_{fields['stage']}" if "stage" in fields else name
    extra: Dict[str, object] = dict(fields)
    stats.fields[key] = extra
    logger.debug(kv(phase=name, status="start", **fields))
    start = time.monotonic()
    try:
        yield extra
    except Exception as exc:
        stats.durations[key] = int((time.monotonic() - start) * 1000)
        line = {"phase": name, "status": "failed", "error_class": classify_error(exc),
                "duration_ms": stats.durations[key]}
        line.update({k: v for k, v in extra.items() if k not in line})
        if isinstance(exc, CrossworkAuthError) and exc.status is not None:
            line["http"] = exc.status
        line["error"] = str(exc)
        logger.error(kv(**line))
        try:
            exc._logged = True  # type: ignore[attr-defined]
        except AttributeError:
            pass
        raise
    stats.durations[key] = int((time.monotonic() - start) * 1000)
    line = {"phase": name, "status": "ok", "duration_ms": stats.durations[key]}
    line.update({k: v for k, v in extra.items() if k not in line})
    logger.info(kv(**line))


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""
    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = CONNECT_TIMEOUT
        return super().send(*args, **kwargs)


def _http_info(resp: requests.Response) -> Dict[str, object]:
    """Return status, server time, and size of an HTTP response for log lines."""
    return {
        "http": resp.status_code,
        "http_ms": int(resp.elapsed.total_seconds() * 1000),
        "resp_bytes": len(resp.content),
    }


def _create_session(verify_ssl: bool = True) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.last_http = {}  # type: ignore[attr-defined]

    def record_http(resp: requests.Response, *args, **kwargs) -> None:
        info = _http_info(resp)
        session.last_http = info  # type: ignore[attr-defined]
        logger.debug(kv(phase="http", method=resp.request.method, path=urlparse(resp.url).path, **info))

    session.hooks["response"].append(record_http)
    return session


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError with useful context when an HTTP response fails."""
    if resp.ok:
        return
    body = (resp.text or "").strip()
    if len(body) > 500:
        body = body[:500] + "..."
    if not body:
        body = "<empty response body>"
    reason = getattr(resp, "reason", "")
    status = f"HTTP {resp.status_code}"
    if reason:
        status = f"{status} {reason}"
    path = urlparse(resp.url).path if resp.url else ""
    raise CrossworkAuthError(f"{context} {path} returned {status}: {body}",
                             status=resp.status_code, context=context)


def get_ticket(session: requests.Session, base_url: str, username: str, password: str) -> str:
    """POST credentials to Crosswork SSO and return a ticket-granting ticket."""
    url = f"{base_url}/crosswork/sso/v1/tickets"
    resp = session.post(
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
        raise CrossworkAuthError(f"Could not extract ticket from response: {resp.text[:300]}",
                                 status=resp.status_code, context="get_ticket")
    return ticket


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token via SSO v2."""
    url = f"{base_url}/crosswork/sso/v2/tickets/jwt"
    resp = session.post(
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
        raise CrossworkAuthError(f"Could not extract token from response: {resp.text[:300]}",
                                 status=resp.status_code, context="get_token")
    return token


def _resolve_credentials(username=None, password=None) -> Tuple[str, str, str, str]:
    """Resolve credentials from args, environment, or script defaults.

    Returns (username, password, username_source, password_source).
    """
    def pick(cli_value: Optional[str], env_name: str, constant: str) -> Tuple[str, str]:
        if cli_value:
            return cli_value, "cli"
        if os.environ.get(env_name):
            return os.environ[env_name], "env"
        return constant, "script_constant"

    username, user_source = pick(username, ENV_USERNAME, CROSSWORK_USERNAME)
    password, password_source = pick(password, ENV_PASSWORD, CROSSWORK_PASSWORD)
    return username, password, user_source, password_source


def load_token_from_file(path: str) -> str:
    """Read a JWT from an explicit file path. An empty file is an error."""
    with open(path, "r", encoding="utf-8") as jwt_file:
        token = jwt_file.read().strip()
    if not token:
        raise CrossworkAuthError(f"JWT file is empty: {path}", context="jwt")
    return token


def read_stored_jwt(path: str) -> Optional[str]:
    """Return the token at *path* when the file exists and is non-empty."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as jwt_file:
        token = jwt_file.read().strip()
    return token or None


def default_jwt_path(ip: str) -> str:
    """Return the default JWT path created by cw_get_jwt.py for *ip*."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def jwt_claims(token: str) -> Dict[str, object]:
    """Decode the JWT payload without verifying it; returns {} if it is not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def acquire_token(session: requests.Session, base_url: str, args: argparse.Namespace,
                  ph: Dict[str, object]) -> str:
    """Return a bearer token and record where it came from in *ph*."""
    if args.jwt:
        ph.update(source="jwt_file", path=args.jwt)
        return load_token_from_file(args.jwt)

    if not args.username and not args.password:
        stored_jwt_path = default_jwt_path(args.ip)
        token = read_stored_jwt(stored_jwt_path)
        if token:
            ph.update(source="stored_jwt", path=stored_jwt_path)
            return token
        ph["stored_jwt"] = "missing_or_empty"

    username, password, user_source, password_source = _resolve_credentials(args.username, args.password)
    ph.update(source="credentials", user=username, user_source=user_source, password_source=password_source)
    if password_source == "script_constant" and password == "PASSWORD":
        logger.warning(kv(phase="auth", warning="CROSSWORK_PASSWORD is still the placeholder value"))
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def log_token_claims(token: str, ph: Dict[str, object]) -> None:
    """Add JWT subject and expiry to *ph*, warning when the token is expired or about to expire."""
    claims = jwt_claims(token)
    exp = claims.get("exp")
    if claims.get("sub") is not None:
        ph["jwt_sub"] = claims["sub"]
    if not isinstance(exp, (int, float)):
        return
    ttl = int(exp - time.time())
    ph["jwt_exp"] = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ph["jwt_ttl_s"] = ttl
    if ttl <= 0:
        logger.warning(kv(phase="auth", warning="JWT is expired", jwt_exp=ph["jwt_exp"], source=ph.get("source")))
    elif ttl < JWT_EXPIRY_WARNING_SECONDS:
        logger.warning(kv(phase="auth", warning="JWT expires soon", jwt_ttl_s=ttl, source=ph.get("source")))


def get_plan(session: requests.Session, base_url: str, token: str, plan_format: str, version: str) -> bytes:
    """Retrieve the plan file from Crosswork."""
    plan_url = f"{base_url}/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/yang-data+json",
        "Content-Type": "application/yang-data+json",
    }
    payload = {
        "input": {
            "version": version,
            "format": plan_format,
        }
    }

    response = session.post(plan_url, headers=headers, json=payload)
    check_response(response, "get_plan")

    data = response.json()

    # Extract plan file content from response
    output = data.get("cisco-crosswork-optimization-engine-operations:output", data.get("output", {}))
    if "planfile-content" in output:
        content = base64.b64decode(output["planfile-content"])
    elif "plan-file-content" in output:
        content = base64.b64decode(output["plan-file-content"])
    else:
        logger.warning(kv(phase="get_plan", warning="no planfile-content in response; using raw body",
                          response_keys=",".join(data.keys()) if isinstance(data, dict) else type(data).__name__,
                          output_keys=",".join(output.keys()) if isinstance(output, dict) else None))
        content = response.content
    if not content:
        raise ValueError("get_plan returned an empty plan")
    return content


def get_dlm_node_coordinates(
    session: requests.Session, base_url: str, token: str
) -> Tuple[Dict[str, Tuple[object, object]], int]:
    """Return (DLM Inventory coordinates keyed by case-insensitive hostname, DLM node count)."""
    inventory_url = f"{base_url}/crosswork/inventory/v1/nodes/query"
    response = session.post(
        inventory_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"limit": 0, "filter": {}},
    )
    check_response(response, "get_dlm_node_coordinates")

    try:
        nodes = response.json().get("data", [])
    except ValueError as exc:
        raise CrossworkAuthError("get_dlm_node_coordinates returned invalid JSON",
                                 context="get_dlm_node_coordinates") from exc
    if not isinstance(nodes, list):
        raise CrossworkAuthError("get_dlm_node_coordinates returned an unexpected response format",
                                 context="get_dlm_node_coordinates")

    coordinates: Dict[str, Tuple[object, object]] = {}
    for node in nodes:
        hostname = node.get("host_name")
        geo = node.get("geo_info", {}).get("coordinates", {})
        latitude = geo.get("latitude", {}).get("value")
        longitude = geo.get("longitude", {}).get("value")
        if isinstance(hostname, str) and latitude is not None and longitude is not None:
            coordinates[hostname.casefold()] = (longitude, latitude)
    return coordinates, len(nodes)


def update_plan_node_coordinates(
    plan_content: bytes, coordinates: Dict[str, Tuple[object, object]]
) -> Tuple[bytes, int, int, List[str]]:
    """Update Longitude and Latitude columns in a text plan's ``<Nodes>`` table.

    Returns (plan, updated count, plan node count, unmatched node names).
    """
    try:
        plan_text = plan_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("--geoloc requires a UTF-8 text plan (.txt), not a binary plan") from exc

    lines = plan_text.splitlines(keepends=True)
    in_nodes_table = False
    header_indexes: Optional[Dict[str, int]] = None
    updated = 0
    plan_node_count = 0
    unmatched: List[str] = []

    for index, line in enumerate(lines):
        line_body = line.rstrip("\r\n")
        if line_body.strip() == "<Nodes>":
            in_nodes_table = True
            header_indexes = None
            continue
        if in_nodes_table and line_body.startswith("<"):
            break
        if not in_nodes_table:
            continue

        columns = line_body.split("\t")
        if header_indexes is None:
            if "Name" in columns and "Longitude" in columns and "Latitude" in columns:
                header_indexes = {
                    "name": columns.index("Name"),
                    "longitude": columns.index("Longitude"),
                    "latitude": columns.index("Latitude"),
                }
            continue
        if not line_body:
            continue

        plan_node_count += 1
        name_index = header_indexes["name"]
        if len(columns) <= name_index:
            continue
        node_coordinates = coordinates.get(columns[name_index].casefold())
        if node_coordinates is None:
            unmatched.append(columns[name_index])
            continue

        required_columns = max(header_indexes.values()) + 1
        columns.extend([""] * (required_columns - len(columns)))
        longitude, latitude = node_coordinates
        columns[header_indexes["longitude"]] = str(longitude)
        columns[header_indexes["latitude"]] = str(latitude)
        line_ending = line[len(line_body):]
        lines[index] = "\t".join(columns) + line_ending
        updated += 1

    if not in_nodes_table or header_indexes is None:
        raise ValueError("The text plan does not contain a <Nodes> table with Longitude and Latitude columns")
    return "".join(lines).encode("utf-8"), updated, plan_node_count, unmatched


def _count_text_plan(path: str) -> Tuple[Dict[str, int], Optional[Dict[str, int]]]:
    """Return (rows per table, LSP rows per Type) for a tab-separated text plan."""
    counts: Dict[str, int] = {}
    lsp_types: Optional[Dict[str, int]] = None
    table: Optional[str] = None
    header: Optional[List[str]] = None
    type_index: Optional[int] = None

    with open(path, "r", encoding="utf-8", errors="replace") as plan_file:
        for line in plan_file:
            body = line.rstrip("\r\n")
            stripped = body.strip()
            if stripped.startswith("<") and stripped.endswith(">") and "\t" not in body:
                table = stripped[1:-1]
                header = None
                type_index = None
                counts.setdefault(table, 0)
                continue
            if table is None or not stripped:
                continue
            if header is None:
                header = body.split("\t")
                if table == "LSPs" and "Type" in header:
                    type_index = header.index("Type")
                    lsp_types = lsp_types or {}
                continue
            counts[table] += 1
            if type_index is not None and lsp_types is not None:
                columns = body.split("\t")
                lsp_type = columns[type_index] if len(columns) > type_index else ""
                lsp_types[lsp_type] = lsp_types.get(lsp_type, 0) + 1
    return counts, lsp_types


def _count_pln_plan(path: str) -> Tuple[Dict[str, int], Optional[Dict[str, int]], Optional[str]]:
    """Return (rows per table, LSP rows per Type, version.dat) for a .pln (zip with SQLite network.db)."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        db_member = "network.db" if "network.db" in names else next((n for n in names if n.endswith(".db")), None)
        if db_member is None:
            raise ValueError(f".pln archive has no SQLite database (members: {names})")
        pln_version = None
        if "version.dat" in names:
            pln_version = archive.read("version.dat").decode("utf-8", errors="replace").strip() or None

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = archive.extract(db_member, tmp_dir)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                wanted = {table for _, table in INVENTORY_TABLES} | {"LSPs"}
                counts = {
                    table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in wanted & tables
                }
                lsp_types = None
                if "LSPs" in tables:
                    columns = [row[1] for row in conn.execute('PRAGMA table_info("LSPs")')]
                    if "Type" in columns:
                        lsp_types = {
                            (lsp_type or ""): count
                            for lsp_type, count in conn.execute('SELECT Type, COUNT(*) FROM "LSPs" GROUP BY Type')
                        }
            finally:
                conn.close()
    return counts, lsp_types, pln_version


def plan_inventory(path: str, plan_format: str) -> Dict[str, object]:
    """Count nodes, circuits, interfaces, SR policies, LSPs, demands, flex-algos, and SRLGs in a plan.

    Tables missing from the plan are reported as None (logged as n/a).
    """
    pln_version = None
    if plan_format == "pln":
        counts, lsp_types, pln_version = _count_pln_plan(path)
    else:
        counts, lsp_types = _count_text_plan(path)

    inventory: Dict[str, object] = {name: counts.get(table) for name, table in INVENTORY_TABLES}
    lsps_total = counts.get("LSPs")
    inventory["lsps_total"] = lsps_total
    if lsp_types is not None and lsps_total is not None:
        srv4 = lsp_types.get("SRv4", 0)
        srv6 = lsp_types.get("SRv6", 0)
        rsvp = lsp_types.get("RSVP", 0)
        inventory.update(sr_policies=srv4 + srv6, sr_policies_srv4=srv4, sr_policies_srv6=srv6, rsvp_lsps=rsvp)
        other = lsps_total - srv4 - srv6 - rsvp
        if other:
            inventory["other_lsps"] = other
    else:
        inventory.update(sr_policies=None, sr_policies_srv4=None, sr_policies_srv6=None, rsvp_lsps=None)
    if pln_version is not None:
        inventory["pln_format_version"] = pln_version
    return {key: inventory[key] for key in INVENTORY_ORDER if key in inventory}


def log_plan_inventory(stats: RunStats, path: str, plan_format: str, stage: str,
                       baseline: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Log plan object counts for *stage*; failures are logged and never abort the collection."""
    with phase(stats, "plan_inventory", stage=stage) as ph:
        try:
            inventory = plan_inventory(path, plan_format)
        except Exception as exc:  # inventory is diagnostic only
            ph["inventory"] = "unavailable"
            logger.warning(kv(phase="plan_inventory", stage=stage, warning="could not count plan objects",
                              error_class="parse", error=str(exc)))
            return {}
        ph.update(inventory)
        if baseline:
            for key in INVENTORY_DELTA_KEYS:
                before, after = baseline.get(key), inventory.get(key)
                if isinstance(before, int) and isinstance(after, int):
                    ph[f"delta_{key}"] = after - before

    nodes, interfaces = inventory.get("nodes"), inventory.get("interfaces")
    if stage == "trimmed" and nodes == 0:
        logger.warning(kv(phase="plan_inventory", stage=stage, warning="trimming removed every node"))
    elif nodes == 0:
        logger.warning(kv(phase="plan_inventory", stage=stage, warning="plan has no nodes; CNC model may be empty"))
    elif isinstance(nodes, int) and interfaces == 0:
        logger.warning(kv(phase="plan_inventory", stage=stage,
                          warning="plan has nodes but no interfaces; CNC model may be partial"))
    return inventory


def run_command(cmd: List[str], description: str) -> None:
    """Run a subprocess command, log output, and raise on failure."""
    tool = cmd[0]
    logger.info(kv(phase="subprocess", tool=tool, description=description, cmd=" ".join(cmd)))
    start = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CommandError(f"{tool} not found on PATH") from exc
    duration_ms = int((time.monotonic() - start) * 1000)
    for line in (result.stdout or "").splitlines():
        if line.strip():
            logger.info("%s %s", kv(phase="subprocess", tool=tool, stream="stdout"), line)
    stderr_level = logging.WARNING if result.returncode != 0 else logging.INFO
    for line in (result.stderr or "").splitlines():
        if line.strip():
            logger.log(stderr_level, "%s %s", kv(phase="subprocess", tool=tool, stream="stderr"), line)
    level = logging.ERROR if result.returncode != 0 else logging.INFO
    logger.log(level, kv(phase="subprocess", tool=tool, rc=result.returncode, duration_ms=duration_ms))
    if result.returncode != 0:
        raise CommandError(f"{description} failed with return code {result.returncode}")


def find_trim_file(
    filename: str, search_dirs: List[str], dir_labels: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Search for a trim file in the given directories, return (path, label) or (None, None)."""
    for d in search_dirs:
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            return path, dir_labels[d]
    return None, None


def find_and_apply_trim(
    tmpfile: str, search_dirs: List[str], dir_labels: Dict[str, str]
) -> str:
    """Discover trim config files, run trim_nodes if needed, and return the input path for conversion."""
    found_trim_files: Dict[str, str] = {}
    found_labels: List[str] = []
    not_found: List[str] = []
    for fname, desc in TRIM_FILES.items():
        path, label = find_trim_file(fname, search_dirs, dir_labels)
        if path:
            logger.debug(kv(phase="trim_config", file=fname, description=desc, location=label, path=path))
            found_trim_files[fname] = path
            found_labels.append(f"{fname}@{label}")
        else:
            not_found.append(fname)

    trim_args: List[str] = []
    regexes: Dict[str, str] = {}
    if "trim_include.txt" in found_trim_files:
        trim_args.extend(["-node-table", found_trim_files["trim_include.txt"], "-exclude-node-table", "false"])
    if "trim_exclude.txt" in found_trim_files:
        trim_args.extend(["-node-table", found_trim_files["trim_exclude.txt"], "-exclude-node-table", "true"])
    for fname, flag in [("trim_include_regex.txt", "-include-nodes-regex"),
                        ("trim_exclude_regex.txt", "-exclude-nodes-regex")]:
        if fname in found_trim_files:
            with open(found_trim_files[fname], "r") as rf:
                regex = rf.read().strip()
            trim_args.extend([flag, regex])
            regexes[flag.lstrip("-").replace("-", "_")] = regex

    logger.info(kv(phase="trim_config", searched=",".join(dir_labels.values()),
                   found=",".join(found_labels) or None, not_found=",".join(not_found) or None, **regexes))

    if not trim_args:
        return tmpfile

    base, _ = os.path.splitext(tmpfile)
    trim_output = base + ".trim" + os.path.splitext(tmpfile)[1]
    trim_cmd = ["trim_nodes", "-plan-file", tmpfile, "-out-file", trim_output] + trim_args
    run_command(trim_cmd, "Trimming nodes")
    return trim_output


def run_collection(args: argparse.Namespace, stats: RunStats, temp_files: List[str]) -> None:
    """Authenticate, retrieve, optionally geolocate and trim, then convert the plan."""
    base_url = f"https://{args.ip}:{BASE_PORT}"
    logger.info(kv(
        phase="start", script="get_plan_cp_cw.py", script_version=SCRIPT_VERSION,
        python=platform.python_version(), requests=requests.__version__, host=socket.gethostname(),
        pid=os.getpid(), cwd=os.getcwd(), base_url=base_url, out=args.planfile, tmpfile=args.tmpfile or "auto",
        home_dir=args.home_dir, user_upload_dir=args.user_upload_dir, plan_version=args.version or "default",
        geoloc=args.geoloc, insecure=args.insecure, timeout_s=CONNECT_TIMEOUT,
    ))

    if args.tmpfile is None:
        name = TMP_PLANFILE
        # DLM coordinates are written into the text plan before it is converted to .db.
        if args.geoloc:
            name = os.path.splitext(name)[0] + ".txt"
        # mkdtemp creates a unique 0700 directory, so the plan (network topology) is not
        # readable by other local users and cannot be pre-planted or symlinked.
        private_dir = tempfile.mkdtemp(prefix="get_plan_cp_cw-")
        temp_files.append(private_dir)
        args.tmpfile = os.path.join(private_dir, name)

    # Deduce format from temporary planfile extension
    ext = os.path.splitext(args.tmpfile)[1].lower()
    if ext == ".txt":
        file_format = "txt"
    elif ext == ".pln":
        file_format = "pln"
    else:
        raise ConfigError(f"Temporary planfile must have .txt or .pln, got '{ext}'")
    if args.geoloc and file_format != "txt":
        raise ConfigError("--geoloc requires a .txt temporary plan file; use --tmpfile planfile.txt")

    if args.insecure:
        logger.warning(kv(phase="start", warning="SSL verification disabled"))
    session = _create_session(verify_ssl=not args.insecure)

    with phase(stats, "auth") as ph:
        token = acquire_token(session, base_url, args, ph)
        log_token_claims(token, ph)

    with phase(stats, "get_plan", format=file_format, plan_version=args.version or "default") as ph:
        plan_content = get_plan(session, base_url, token, file_format, args.version)
        ph.update(session.last_http)
        ph["bytes"] = len(plan_content)

    if args.geoloc:
        with phase(stats, "geoloc") as ph:
            coordinates, dlm_nodes = get_dlm_node_coordinates(session, base_url, token)
            ph.update(session.last_http)
            plan_content, updated_nodes, plan_nodes, unmatched = update_plan_node_coordinates(
                plan_content, coordinates)
            ph.update(dlm_nodes=dlm_nodes, dlm_with_coords=len(coordinates), plan_nodes=plan_nodes,
                      matched=updated_nodes, unmatched=len(unmatched))
        if unmatched:
            logger.debug(kv(phase="geoloc", unmatched_sample=",".join(unmatched[:20])))
        if plan_nodes and not updated_nodes:
            logger.warning(kv(phase="geoloc", warning="no plan node matched a DLM host_name with coordinates"))

    with phase(stats, "write_tmp", path=args.tmpfile) as ph:
        with open(args.tmpfile, "wb") as f:
            f.write(plan_content)
        temp_files.append(args.tmpfile)
        ph["bytes"] = len(plan_content)

    received = log_plan_inventory(stats, args.tmpfile, file_format, "received")

    search_dirs = [args.home_dir, args.user_upload_dir]
    dir_labels = {args.home_dir: "home_dir", args.user_upload_dir: "user_upload_dir"}

    with phase(stats, "trim") as ph:
        convert_input = find_and_apply_trim(args.tmpfile, search_dirs, dir_labels)
        ph["applied"] = convert_input != args.tmpfile
    if convert_input != args.tmpfile:
        temp_files.append(convert_input)
        log_plan_inventory(stats, convert_input, file_format, "trimmed", baseline=received)

    # Convert .pln to .db format as required by Crosswork Planning
    with phase(stats, "convert", input=convert_input, out=args.planfile) as ph:
        run_command(["mate_convert", "-plan-file", convert_input, "-out-file", args.planfile], "Converting plan")
        if not os.path.isfile(args.planfile) or os.path.getsize(args.planfile) == 0:
            raise CommandError(f"mate_convert exited 0 but {args.planfile} is missing or empty")
        ph["out_bytes"] = os.path.getsize(args.planfile)


ERROR_HINTS = {
    "config": "check --tmpfile extension and --geoloc settings",
    "connect": "check that the CNC address and port 30603 are reachable from the Crosswork Planning collector",
    "timeout": "CNC did not respond within CONNECT_TIMEOUT; check CNC load or raise CONNECT_TIMEOUT",
    "subprocess": "see the subprocess stdout/stderr lines above",
    "parse": "CNC returned an unexpected response or plan format",
    "io": "check file paths and permissions",
}


def error_hint(exc: BaseException, error_class: str, stats: RunStats) -> Optional[str]:
    """Return a short operator hint for a failure."""
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate verification failed; install the CNC CA certificate or use -k / INSECURE"
    if error_class == "auth":
        source = stats.fields.get("auth", {}).get("source")
        if source in ("jwt_file", "stored_jwt"):
            return "JWT may be expired or invalid; refresh it or remove it to fall back to credentials"
        return "check CNC credentials and AAA session limits (Admin > AAA Settings)"
    return ERROR_HINTS.get(error_class)


def log_summary(stats: RunStats, status: str, exit_code: int, error_class: Optional[str],
                total_ms: int) -> None:
    fields: Dict[str, object] = {"phase": "summary", "status": status, "exit": exit_code}
    if error_class:
        fields["error_class"] = error_class
    fields["total_ms"] = total_ms
    for name in ("auth", "get_plan", "geoloc", "write_tmp", "trim", "convert"):
        if name in stats.durations:
            fields[f"{name}_ms"] = stats.durations[name]
    fields["plan_bytes"] = stats.fields.get("get_plan", {}).get("bytes")
    inventory = stats.fields.get("plan_inventory_received", {})
    for key in SUMMARY_INVENTORY_KEYS:
        fields[key] = inventory.get(key)
    fields["trim_applied"] = stats.fields.get("trim", {}).get("applied")
    fields["out_bytes"] = stats.fields.get("convert", {}).get("out_bytes")
    level = logging.INFO if status == "ok" else logging.ERROR
    logger.log(level, kv(**fields))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve a plan file from Crosswork Network Controller",
        epilog=(
            "When --username, --password, and --jwt are omitted, a non-empty "
            "~/.crosswork/<ip>.jwt is used. If that file is missing or empty, or if "
            "--username or --password is set, credentials are resolved in order: "
            f"CLI flags > environment variables ({ENV_USERNAME}, {ENV_PASSWORD}) > "
            "the CROSSWORK_USERNAME and CROSSWORK_PASSWORD constants in this script.\n"
            f"Log verbosity: --debug, or set {ENV_LOG_LEVEL} (DEBUG, INFO, WARNING, ERROR)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dummy", help="First parameter, base planfile (ignored)")
    parser.add_argument("planfile", help="Output plan file name")
    parser.add_argument("device_auth_file", help="Device access authentication file")
    parser.add_argument("network_access_config", help="Global network access configuration file")
    parser.add_argument("home_dir", help="Home directory")
    parser.add_argument("user_upload_dir", help="Path where user uploaded external files are available")
    parser.add_argument("archive_root_dir", help="Path to access archive root directory")
    parser.add_argument("--tmpfile", default=None,
                        help=f"Intermediate plan file; extension (.pln/.txt) sets download format "
                             f"(default: {TMP_PLANFILE} in a private per-run temp directory)")
    parser.add_argument("--ip", default=CROSSWORK_IP, help=f"Crosswork controller IP address (default: {CROSSWORK_IP})")
    parser.add_argument("--username", "-u", default=None,
                        help=f"Username (or set {ENV_USERNAME}; otherwise the script constant)")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; otherwise the script constant)")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument("-k", "--insecure", action="store_true", default=INSECURE,
                        help=f"Disable SSL certificate verification (not recommended) (default: {INSECURE})")
    parser.add_argument("--version", "-v", default=PLAN_VERSION,
                        help=f"Planfile version (default: {PLAN_VERSION or 'empty'})")
    parser.add_argument("--geoloc", action="store_true", default=GEOLOC,
                        help="Populate <Nodes> Longitude and Latitude from DLM Inventory "
                             f"(uses a text temporary plan) (default: {GEOLOC})")
    parser.add_argument("--debug", action="store_true",
                        help=f"Enable DEBUG logging (overrides {ENV_LOG_LEVEL})")

    args = parser.parse_args()
    setup_logging(args.debug)

    stats = RunStats()
    temp_files: List[str] = []
    status = "interrupted"
    exit_code = 130
    error_class: Optional[str] = None
    start = time.monotonic()
    try:
        run_collection(args, stats, temp_files)
        status, exit_code = "ok", 0
    except Exception as exc:
        error_class = classify_error(exc)
        status, exit_code = "failed", EXIT_CODES[error_class]
        fields: Dict[str, object] = {"phase": "error", "error_class": error_class}
        if not getattr(exc, "_logged", False):
            fields["error"] = str(exc)
        hint = error_hint(exc, error_class, stats)
        if hint:
            fields["hint"] = hint
        logger.error(kv(**fields))
        logger.debug("Traceback for run %s", RUN_ID, exc_info=True)
    finally:
        for tmp in reversed(temp_files):
            try:
                if os.path.isdir(tmp):
                    shutil.rmtree(tmp)
                else:
                    os.remove(tmp)
                logger.info(kv(phase="cleanup", removed=tmp))
            except OSError as exc:
                logger.debug(kv(phase="cleanup", path=tmp, error=str(exc)))
        log_summary(stats, status, exit_code, error_class, int((time.monotonic() - start) * 1000))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
