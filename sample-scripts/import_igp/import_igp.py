#!/usr/bin/env python3
"""
Submit native ``parse_igp`` as a Crosswork Planning CLI_JOB over REST.

Builds a plan file from an IGP link-state database (the output of
``show isis database verbose``, ``show ip ospf database router``, or the
Juniper ``extensive`` equivalents) by staging the database file in Design
userspace, submitting an immediate ``CLI_JOB`` (``script=parse_igp``),
polling until completion, and extracting the plan from the result tar.

Tool options match ``parse_igp -help``. The engine injects ``-plan-file``
from ``inPlanFileId`` and ``-out-file`` from ``jobConfig.outFile``. CLI_JOB
requires a staged plan, so when ``-plan-file`` is omitted an empty base plan
is staged in its place. ``-database-file`` and the remaining flags go in
``jobConfig.options``.

Credentials are never hardcoded. When ``--username``, ``--password``, and
``--jwt`` are all omitted, the script uses ``~/.crosswork/<ip>.jwt`` if that
file exists (the path ``cw_get_jwt.py`` writes). Otherwise it uses ``--jwt``,
CLI flags, ``CW_USERNAME`` / ``CW_PASSWORD``, or an interactive prompt.

HTTPS only. Certificate verification stays on unless ``--insecure`` is set,
and the TLS handshake requires TLS 1.2 or newer. Redirects are not followed.
Plan names sent to the engine are restricted to a short safe character set,
and upload, JSON, and result-tar sizes are capped.
"""

from __future__ import annotations

import argparse
import getpass
import io
import ipaddress
import json
import math
import os
import re
import ssl
import stat
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import requests
import urllib3

BOOLEAN_STRINGS = ("true", "false")
IGP_PROTOCOLS = ("ospf", "ospfv3", "isis", "isisv6")
ISIS_PROTOCOLS = frozenset({"isis", "isisv6"})
ISIS_LEVELS = ("1", "2", "both")
LIC_PRODUCTS = ("Collector", "Design")
JOB_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "ABORTED", "CANCELLED")
FILE_CATEGORIES = frozenset({"PLANFILE", "OTHERS"})
OPTION_FLAGS = frozenset(
    {
        "-igp-protocol",
        "-database-file",
        "-isis-level",
        "-lic-product",
        "-use-dns",
        "-area",
        "-find-ip-manage",
        "-create-subnet-links",
        "-get-segments",
        "-add-node-tag",
        "-simple-txt-out-file",
        "-verbosity",
        "-log-file",
        "-suppress-progress",
        "-options-file",
        "-no-global-options",
    }
)
REDACT_RESPONSE_BODY = frozenset({"get_ticket", "get_token"})

DEFAULT_PORT = 30603
DEFAULT_TIMEOUT = 30
ENV_USERNAME = "CW_USERNAME"
# Name of the password environment variable. This is not a password value.
ENV_PASSWORD = "CW_PASSWORD"  # noqa: S105  # nosec B105
ENV_HOST = "CW_HOST"
DEFAULT_PROFILE = "4000m vCPU, 16Gi Mem"
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_JOB_TIMEOUT = 300
MAX_HTTP_TIMEOUT = 86400
MAX_JOB_TIMEOUT = 86400
MAX_POLL_INTERVAL = 3600
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_TRANSFER_BYTES = 1024 * 1024 * 1024
MAX_TAR_MEMBERS = 4096
MAX_TOKEN_CHARS = 16384
MAX_NAME_LEN = 255
SPOOL_MEMORY_BYTES = 8 * 1024 * 1024

PROFILE_RE = re.compile(r"^\d+m vCPU,\s*\d+Gi Mem$")
JOB_ID_RE = re.compile(r"Job Id:\s*(\d+)", re.IGNORECASE)
SAFE_NAME_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_NAME_LEN - 1}}}$")
AREA_RE = re.compile(r"^(?:all|[0-9]{1,10}|[0-9]{1,3}(?:\.[0-9]{1,3}){3})$")
NODE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_NODE_TAGS = 32
# CLI_JOB fails with "Plan file does not exist" unless inPlanFileId names a
# staged plan, so an empty plan is staged when -plan-file is omitted. No
# Version row, so the same file works across releases.
EMPTY_BASE_PLAN = "<Network>\nProperty\tValue\nTitle\t\n"
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or an API call fails."""


class ImportIgpError(RuntimeError):
    """Raised when parse_igp REST job staging, submit, or export fails."""


def _one_line(text: str, limit: int = 300) -> str:
    """Collapse untrusted text onto one printable line."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned


def _positive_number(name: str, value: float, *, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if (
        isinstance(value, bool)
        or not math.isfinite(number)
        or number <= 0
        or number > upper
    ):
        raise ValueError(f"{name} must be greater than 0 and at most {upper:g}")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ImportIgpError(f"invalid {label}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ImportIgpError(f"invalid {label}") from exc
    if number <= 0 or number > 2**63 - 1:
        raise ImportIgpError(f"invalid {label}")
    return number


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be an integer from 1 to 65535, got: {port!r}")
    return port


def _validate_host(value: str) -> str:
    """Return a hostname or IP safe to place in an https URL."""
    host = _as_str(value)
    if (
        not host
        or len(host) > 253
        or any(ch.isspace() or not ch.isprintable() for ch in host)
    ):
        raise ValueError("controller host must be a hostname or IP address")
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        labels = host.split(".")
        if not HOSTNAME_RE.fullmatch(host) or all(label.isdigit() for label in labels):
            raise ValueError(
                "controller host must be a hostname or IP address"
            ) from None
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    return str(address)


def _tls_context(*, verify: bool) -> ssl.SSLContext:
    """Client context: default trust store, TLS 1.2 minimum, compression off.

    TLS 1.2 remains allowed so the client can reach Crosswork controllers that
    have not moved to TLS 1.3 only. ``verify=False`` is the ``--insecure`` path.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout and a fixed TLS context."""

    def __init__(self, timeout: float, ssl_context: ssl.SSLContext) -> None:
        self._timeout = timeout
        self._ssl_context = ssl_context
        super().__init__()

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        pool_kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return super().send(request, **kwargs)


class _RejectCleartext(requests.adapters.HTTPAdapter):
    """Fail closed if a request is ever sent over cleartext HTTP."""

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        raise ImportIgpError("refusing non-HTTPS URL")


class _CrossworkSession(requests.Session):
    """Session that refuses cleartext URLs and HTTP redirects."""

    def __init__(self, *, verify_ssl: bool, timeout: float) -> None:
        super().__init__()
        self.verify = verify_ssl
        self._timeout = timeout
        adapter = _TimeoutAdapter(timeout, _tls_context(verify=verify_ssl))
        self.mount("https://", adapter)
        self.mount("http://", _RejectCleartext())

    def request(
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ImportIgpError("refusing non-HTTPS URL")
        kwargs["timeout"] = self._timeout
        kwargs["allow_redirects"] = False
        kwargs["stream"] = True
        return super().request(method, url, *args, **kwargs)


def _create_session(*, verify_ssl: bool, timeout: float) -> _CrossworkSession:
    """Create an HTTP session with a default timeout and TLS verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return _CrossworkSession(verify_ssl=verify_ssl, timeout=timeout)


@contextmanager
def _exchange(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs: Any,
) -> Any:
    """Perform one HTTPS call and always close the response."""
    resp = session.request(method, url, **kwargs)
    try:
        yield resp
    finally:
        resp.close()


def _content_length(resp: requests.Response, limit: int, context: str) -> None:
    declared = resp.headers.get("Content-Length")
    if declared is None:
        return
    try:
        size = int(declared)
    except ValueError as exc:
        raise ImportIgpError(f"{context} returned an invalid Content-Length") from exc
    if size < 0 or size > limit:
        raise ImportIgpError(f"{context} response exceeds {limit} bytes")


def _iter_body(resp: requests.Response, limit: int, context: str):
    _content_length(resp, limit, context)
    total = 0
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ImportIgpError(f"{context} response exceeds {limit} bytes")
        yield chunk


def _read_capped(resp: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        if total >= limit:
            break
        room = limit - total
        piece = chunk[:room]
        chunks.append(piece)
        total += len(piece)
        if len(chunk) > room:
            break
    return b"".join(chunks)


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError when an HTTP response is not successful."""
    if resp.ok:
        return
    status = f"HTTP {resp.status_code}"
    reason = getattr(resp, "reason", "") or ""
    if reason:
        status = f"{status} {_one_line(str(reason), 80)}"
    if context in REDACT_RESPONSE_BODY:
        raise CrossworkAuthError(f"{context} returned {status}")
    body = _one_line(_error_body(resp), 500) or "<empty response body>"
    raise CrossworkAuthError(f"{context} returned {status}: {body}")


def _error_body(resp: requests.Response) -> str:
    """Decode a capped slice of an error body. Auth callers omit this text."""
    raw = _read_capped(resp, 4096)
    return raw.decode("utf-8", errors="replace")


def _response_json(resp: requests.Response, context: str) -> dict[str, Any]:
    check_response(resp, context)
    raw = b"".join(_iter_body(resp, MAX_JSON_BYTES, context))
    if not raw:
        raise ImportIgpError(f"{context} returned an empty body")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportIgpError(f"{context} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ImportIgpError(f"{context} returned a JSON value that is not an object")
    return data


def _token_from_body(raw: bytes, field: str) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return str(parsed.get(field) or "").strip()
    return ""


def _validate_token(token: str, label: str) -> str:
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise CrossworkAuthError(f"Could not extract {label} from the SSO response")
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        raise CrossworkAuthError(f"Could not extract {label} from the SSO response")
    return token


def get_ticket(
    session: requests.Session, base_url: str, username: str, password: str
) -> str:
    """POST credentials to Crosswork SSO and return a ticket-granting ticket."""
    url = f"{base_url}/crosswork/sso/v1/tickets"
    with _exchange(
        session,
        "POST",
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        check_response(resp, "get_ticket")
        location = resp.headers.get("Location", "")
        if location:
            ticket = location.rstrip("/").split("/")[-1]
        else:
            ticket = _token_from_body(_read_capped(resp, MAX_JSON_BYTES), "ticket")
    return _validate_token(ticket.strip(), "ticket")


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token."""
    url = f"{base_url}/crosswork/sso/v2/tickets/jwt"
    with _exchange(
        session,
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"tgt": ticket, "service": f"{base_url}/app-dashboard"},
    ) as resp:
        check_response(resp, "get_token")
        token = _token_from_body(_read_capped(resp, MAX_JSON_BYTES), "token")
    return _validate_token(token, "token")


def get_jwt(
    ip: str,
    username: str,
    password: str,
    *,
    port: int = DEFAULT_PORT,
    verify_ssl: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> str:
    """Authenticate to Crosswork and return the JWT token string."""
    owns_session = session is None
    active = session or _create_session(verify_ssl=verify_ssl, timeout=timeout)
    try:
        base_url = f"https://{_validate_host(ip)}:{_validate_port(port)}"
        ticket = get_ticket(active, base_url, username, password)
        return get_token(active, base_url, ticket)
    finally:
        if owns_session:
            active.close()


def _resolve_credentials(username: str | None, password: str | None) -> tuple[str, str]:
    """Resolve username and password from CLI, environment, or a prompt.

    Priority: CLI argument, then environment variable, then interactive prompt.
    """
    resolved_user = (username or os.environ.get(ENV_USERNAME) or "").strip()
    if not resolved_user:
        resolved_user = input("Username: ").strip()
    if (
        not resolved_user
        or len(resolved_user) > 256
        or any(not ch.isprintable() for ch in resolved_user)
    ):
        raise CrossworkAuthError("username is empty or invalid")

    resolved_pass = password or os.environ.get(ENV_PASSWORD)
    if not resolved_pass:
        resolved_pass = getpass.getpass("Password: ")
    if (
        not resolved_pass
        or len(resolved_pass) > 1024
        or any(not ch.isprintable() for ch in resolved_pass)
    ):
        raise CrossworkAuthError("password is empty or invalid")
    return resolved_user, resolved_pass


def get_auth_headers(token: str) -> dict[str, str]:
    """Return bearer authorization headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _as_str(value: str) -> str:
    return str(value).strip()


def _validate_bool(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in BOOLEAN_STRINGS:
        raise ValueError(f"Expected one of {BOOLEAN_STRINGS}, got: {value!r}")
    return lowered


def _validate_igp_protocol(value: str) -> str:
    text = _as_str(value).lower()
    if text not in IGP_PROTOCOLS:
        raise ValueError(f"igp_protocol must be one of {IGP_PROTOCOLS}, got: {value!r}")
    return text


def _validate_isis_level(value: str) -> str:
    text = _as_str(value).lower()
    if text not in ISIS_LEVELS:
        raise ValueError(f"isis_level must be one of {ISIS_LEVELS}, got: {value!r}")
    return text


def _validate_lic_product(value: str) -> str:
    text = _as_str(value).capitalize()
    if text not in LIC_PRODUCTS:
        raise ValueError(f"lic_product must be one of {LIC_PRODUCTS}, got: {value!r}")
    return text


def _validate_area(value: str) -> str:
    """Allow 'all', a numeric area id, or a dotted-quad OSPF area id."""
    text = _as_str(value).lower()
    if not AREA_RE.fullmatch(text):
        raise ValueError(
            f"area must be 'all', a number, or a dotted-quad area id, got: {value!r}"
        )
    return text


def _validate_node_tags(value: str) -> str:
    """Comma-separated tags, each a short name from the safe character set."""
    tags = [tag.strip() for tag in _as_str(value).split(",")]
    if not tags or len(tags) > MAX_NODE_TAGS:
        raise ValueError(f"add_node_tag must list 1-{MAX_NODE_TAGS} tags")
    for tag in tags:
        if not NODE_TAG_RE.fullmatch(tag):
            shown = _one_line(tag, 80) or "<empty>"
            raise ValueError(
                "each node tag must be 1-64 characters of letters, digits, "
                f"'.', '_' or '-', starting with a letter or digit: {shown}"
            )
    return ",".join(tags)


def _validate_profile(value: str) -> str:
    text = _as_str(value)
    if not PROFILE_RE.fullmatch(text):
        raise ValueError(
            f"requestedProfile must match '<N>m vCPU, <N>Gi Mem', got: {value!r}"
        )
    return text


def _safe_basename(path: str) -> str:
    """Last path component, restricted to a shell-safe allowlist.

    Names that start with ``-`` are rejected so they cannot be read as flags
    when the engine places them after ``-plan-file`` or ``-out-file``.
    """
    name = Path(_as_str(path)).name
    if not SAFE_NAME_RE.fullmatch(name):
        shown = _one_line(path, 120) or "<empty>"
        raise ValueError(
            "file name must be 1-255 characters of letters, digits, '.', '_' or '-', "
            f"must start with a letter or digit, and must not include a path: {shown}"
        )
    return name


def _require_safe_name(name: str, label: str) -> str:
    text = str(name).strip()
    if not SAFE_NAME_RE.fullmatch(text):
        raise ImportIgpError(f"invalid {label}")
    return text


def _add_bool(args: list[str], flag: str, value: bool | None) -> None:
    if value is not None:
        args.extend([flag, "true" if value else "false"])


def _option_value_ok(flag: str, value: str) -> bool:
    if flag == "-add-node-tag":
        return all(NODE_TAG_RE.fullmatch(tag) for tag in value.split(","))
    if flag == "-area":
        return AREA_RE.fullmatch(value) is not None
    return SAFE_NAME_RE.fullmatch(value) is not None


def _checked_options(args: list[str]) -> list[str]:
    if len(args) % 2:
        raise ValueError("parse_igp options must be flag/value pairs")
    for flag, value in zip(args[0::2], args[1::2], strict=True):
        if flag not in OPTION_FLAGS or not _option_value_ok(flag, value):
            raise ValueError("refusing an unsafe parse_igp option")
    option_str = " ".join(args)
    if len(option_str) > 4000:
        raise ValueError("parse_igp options are too long")
    return args


def build_parse_igp_options(
    *,
    igp_protocol: str,
    database_file: str,
    isis_level: str | None = None,
    lic_product: str | None = None,
    use_dns: bool | None = None,
    area: str | None = None,
    find_ip_manage: bool | None = None,
    create_subnet_links: bool | None = None,
    get_segments: bool | None = None,
    add_node_tag: str | None = None,
    simple_txt_out_file: bool | None = None,
    verbosity: int | None = None,
    log_file: str | None = None,
    suppress_progress: bool | None = None,
    options_file: str | None = None,
    no_global_options: bool | None = None,
) -> list[str]:
    """
    Build ``jobConfig.options`` flags for native parse_igp.

    Does not include ``-plan-file`` / ``-out-file``; the engine injects those
    from ``inPlanFileId`` and ``jobConfig.outFile``. ``database_file`` is the
    staged basename, not a local path.
    """
    protocol = _validate_igp_protocol(igp_protocol)
    if isis_level is not None and protocol not in ISIS_PROTOCOLS:
        raise ValueError("isis_level is only valid with an ISIS igp_protocol")
    if get_segments and protocol not in ISIS_PROTOCOLS:
        raise ValueError("get_segments is only valid with an ISIS igp_protocol")

    args: list[str] = [
        "-igp-protocol",
        protocol,
        "-database-file",
        _safe_basename(database_file),
    ]
    if isis_level is not None:
        args.extend(["-isis-level", _validate_isis_level(isis_level)])
    if lic_product is not None:
        args.extend(["-lic-product", _validate_lic_product(lic_product)])
    _add_bool(args, "-use-dns", use_dns)
    if area is not None:
        args.extend(["-area", _validate_area(area)])
    _add_bool(args, "-find-ip-manage", find_ip_manage)
    _add_bool(args, "-create-subnet-links", create_subnet_links)
    _add_bool(args, "-get-segments", get_segments)
    if add_node_tag is not None and str(add_node_tag).strip():
        args.extend(["-add-node-tag", _validate_node_tags(add_node_tag)])
    _add_bool(args, "-simple-txt-out-file", simple_txt_out_file)
    if verbosity is not None:
        if (
            isinstance(verbosity, bool)
            or not isinstance(verbosity, int)
            or not 1 <= verbosity <= 60
        ):
            raise ValueError("verbosity must be an integer between 1 and 60")
        args.extend(["-verbosity", str(verbosity)])
    if log_file is not None and str(log_file).strip():
        args.extend(["-log-file", _safe_basename(log_file)])
    _add_bool(args, "-suppress-progress", suppress_progress)
    if options_file is not None and str(options_file).strip():
        args.extend(["-options-file", _safe_basename(options_file)])
    _add_bool(args, "-no-global-options", no_global_options)
    return _checked_options(args)


def build_job_config(
    *,
    out_file: str,
    options: list[str] | None = None,
) -> str:
    """Return the CLI_JOB ``jobConfig`` JSON string for native parse_igp."""
    checked = _checked_options(list(options or []))
    return json.dumps(
        {
            "scriptType": "CLI",
            "script": "parse_igp",
            "outFile": _safe_basename(out_file),
            "options": " ".join(checked),
        }
    )


def engine_out_file_name(out_file: str, database_file: str) -> str:
    """Basename written on the engine; derived from ``-out-file`` or the database."""
    dest = _as_str(out_file)
    if dest == "-":
        stem = Path(_safe_basename(database_file)).stem or "igp"
        return _safe_basename(f"{stem}_igp.txt")
    return _safe_basename(dest)


def _job_name(name: str) -> str:
    cleaned = _one_line(name, 200)
    if not cleaned:
        raise ValueError("job name is empty")
    return cleaned


def parse_job_id(resp: dict[str, Any]) -> int:
    """Extract numeric job id from a POST /jobs envelope."""
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("jobId", "id"):
            if data.get(key) is not None:
                return _positive_int(data[key], "jobId")
        match = JOB_ID_RE.search(str(data.get("message") or ""))
        if match:
            return _positive_int(match.group(1), "jobId")
    raise ImportIgpError("could not parse job id from the jobs response")


def parse_fs_id(resp: dict[str, Any]) -> str:
    """Read ``data.fsId`` from GET /jobs/input. Empty fsId is a failure."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    fs_id = str((data or {}).get("fsId") or "").strip()
    if (
        not fs_id
        or len(fs_id) > 1024
        or any(ch.isspace() or not ch.isprintable() for ch in fs_id)
    ):
        raise ImportIgpError("jobs/input returned an invalid fsId")
    return fs_id


def _id_from_file_record(
    source: dict[str, Any], fallback_name: str
) -> tuple[int, str] | None:
    if source.get("fileId") is None:
        return None
    stored = str(source.get("fileName") or "").strip() or fallback_name
    return _positive_int(source["fileId"], "fileId"), _require_safe_name(
        stored, "fileName"
    )


def parse_import_file_info(resp: dict[str, Any], fallback_name: str) -> tuple[int, str]:
    """Parse userspace fileId and stored name from a file/import envelope."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    extra: Any = (data or {}).get("additionalInfo")
    if isinstance(extra, str) and extra.strip():
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError as exc:
            raise ImportIgpError(
                "import response additionalInfo is not valid JSON"
            ) from exc
    if isinstance(extra, dict):
        parsed = _id_from_file_record(extra, fallback_name)
        if parsed is not None:
            return parsed
    if isinstance(data, dict):
        parsed = _id_from_file_record(data, fallback_name)
        if parsed is not None:
            return parsed
    raise ImportIgpError("import response missing fileId")


def _tar_basename(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return parts[-1]


def _read_tar_member(tar: tarfile.TarFile, wanted: str) -> bytes:
    for index, member in enumerate(tar, start=1):
        if index > MAX_TAR_MEMBERS:
            raise ImportIgpError("result tar has too many members")
        if _tar_basename(member.name) != wanted:
            continue
        if not member.isreg():
            raise ImportIgpError(f"{wanted!r} is not a regular file in the result tar")
        if member.size <= 0 or member.size > MAX_TRANSFER_BYTES:
            raise ImportIgpError(f"{wanted!r} in the result tar exceeds the size limit")
        handle = tar.extractfile(member)
        if handle is None:
            break
        chunks: list[bytes] = []
        remaining = member.size
        with handle:
            while remaining:
                part = handle.read(min(1024 * 1024, remaining))
                if not part:
                    break
                if len(part) > remaining:
                    raise ImportIgpError(
                        f"{wanted!r} in the result tar exceeds the size limit"
                    )
                chunks.append(part)
                remaining -= len(part)
        if remaining:
            raise ImportIgpError(f"{wanted!r} in the result tar was truncated")
        return b"".join(chunks)
    raise ImportIgpError(f"{wanted!r} not found in job result tar")


def extract_member_from_tar(tar_file: BinaryIO, member_name: str) -> bytes:
    """Return one regular-file member matched by basename.

    The archive is not extracted to disk. Links, absolute paths, and ``..``
    components are ignored. The member is read only up to its declared size,
    which must be within ``MAX_TRANSFER_BYTES``.
    """
    wanted = _safe_basename(member_name)
    try:
        with tarfile.open(fileobj=tar_file, mode="r:*") as tar:
            return _read_tar_member(tar, wanted)
    except tarfile.TarError as exc:
        raise ImportIgpError("job result is not a readable tar archive") from exc


def _local_file_size(path: Path) -> int:
    if not path.is_file():
        raise ImportIgpError(f"file not found: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ImportIgpError(f"refusing to upload an empty file: {path.name}")
    if size > MAX_TRANSFER_BYTES:
        raise ImportIgpError(f"file exceeds {MAX_TRANSFER_BYTES} bytes: {path.name}")
    return size


def import_to_userspace(
    session: requests.Session,
    base_url: str,
    token: str,
    file_path: str,
    *,
    file_name: str | None = None,
    category: str = "PLANFILE",
    tags: str | None = None,
) -> dict[str, Any]:
    """Import a file into Design userspace."""
    if category not in FILE_CATEGORIES:
        raise ImportIgpError("invalid userspace file category")
    name = _safe_basename(file_name or Path(file_path).name)
    path = Path(file_path)
    _local_file_size(path)
    data = {"fileName": name, "category": category}
    if tags:
        tag_text = _one_line(tags, 256)
        if tag_text:
            data["tags"] = tag_text

    url = f"{base_url}/cp/apiGateway/design/v1/file/import"
    with (
        path.open("rb") as handle,
        _exchange(
            session,
            "POST",
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, handle)},
            data=data,
        ) as resp,
    ):
        return _response_json(resp, "import_to_userspace")


def _copy_to_path(source: BinaryIO, output_path: str) -> None:
    dest = Path(output_path)
    if not _as_str(output_path) or "\x00" in output_path:
        raise ImportIgpError("invalid output path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def search_userspace_file(
    session: requests.Session,
    base_url: str,
    token: str,
    file_name: str,
    *,
    category: str = "PLANFILE",
) -> dict[str, Any] | None:
    """Find a userspace file by exact fileName and category."""
    if category not in FILE_CATEGORIES:
        raise ImportIgpError("invalid userspace file category")
    safe_name = _safe_basename(file_name)
    url = f"{base_url}/cp/apiGateway/design/v1/file/search"
    with _exchange(
        session,
        "GET",
        url,
        headers=get_auth_headers(token),
        params={
            "page": 0,
            "size": 100,
            "fileCategories": category,
            "fileName": safe_name,
        },
    ) as resp:
        body = _response_json(resp, "search_userspace_file")
    for entry in body.get("data", {}).get("content", []):
        if isinstance(entry, dict) and entry.get("fileName") == safe_name:
            return entry
    return None


def resolve_userspace_file(
    session: requests.Session,
    base_url: str,
    token: str,
    path: str,
    *,
    category: str,
    label: str,
) -> tuple[int, str]:
    """Import a local file, or locate an existing userspace file by basename."""
    local = Path(path)
    basename = _safe_basename(path)
    if local.is_file():
        print(f"Importing {label} {basename} to userspace...", file=sys.stderr)
        resp = import_to_userspace(
            session,
            base_url,
            token,
            str(local),
            file_name=basename,
            category=category,
        )
        return parse_import_file_info(resp, basename)

    found = search_userspace_file(session, base_url, token, basename, category=category)
    if found and found.get("fileId") is not None:
        return _positive_int(found["fileId"], "fileId"), _require_safe_name(
            str(found.get("fileName") or basename),
            "fileName",
        )
    raise ImportIgpError(
        f"{label} file not found locally or in userspace: {_one_line(path, 200)}"
    )


def resolve_optional_local_file(
    session: requests.Session,
    base_url: str,
    token: str,
    path: str | None,
    *,
    category: str = "OTHERS",
) -> tuple[int, str] | None:
    """Import ``path`` when it is a local file so it can be staged with the job."""
    if path is None or not str(path).strip():
        return None
    local = Path(path)
    if not local.is_file():
        return None
    basename = _safe_basename(path)
    print(f"Importing {basename} to userspace...", file=sys.stderr)
    resp = import_to_userspace(
        session,
        base_url,
        token,
        str(local),
        file_name=basename,
        category=category,
    )
    return parse_import_file_info(resp, basename)


def stage_job_input(
    session: requests.Session,
    base_url: str,
    token: str,
    file_ids: list[int],
) -> str:
    """GET /design/v1/jobs/input?fileId=... and return a non-empty fsId."""
    if not file_ids:
        raise ImportIgpError("no file ids to stage")
    url = f"{base_url}/cp/apiGateway/design/v1/jobs/input"
    params = [("fileId", str(_positive_int(fid, "fileId"))) for fid in file_ids]
    with _exchange(
        session, "GET", url, headers=get_auth_headers(token), params=params
    ) as resp:
        return parse_fs_id(_response_json(resp, "stage_job_input"))


def submit_cli_job(
    session: requests.Session,
    base_url: str,
    token: str,
    *,
    job_name: str,
    fs_id: str,
    job_config: str,
    in_plan_file_id: str,
    requested_profile: str,
    job_priority: str,
) -> int:
    """POST /design/v1/jobs and return the new job id."""
    url = f"{base_url}/cp/apiGateway/design/v1/jobs"
    payload = {
        "jobName": _job_name(job_name),
        "jobType": "CLI_JOB",
        "fsId": fs_id,
        "jobConfig": job_config,
        "inPlanFileId": _require_safe_name(in_plan_file_id, "inPlanFileId"),
        "requestedProfile": _validate_profile(requested_profile),
        "jobPriority": job_priority,
    }
    with _exchange(
        session, "POST", url, headers=get_auth_headers(token), json=payload
    ) as resp:
        return parse_job_id(_response_json(resp, "submit_cli_job"))


def poll_job(
    session: requests.Session,
    base_url: str,
    token: str,
    job_id: int,
    *,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    """POST /design/v1/jobs/search until a terminal status or timeout."""
    url = f"{base_url}/cp/apiGateway/design/v1/jobs/search"
    # "conjuction" matches the Design API field name.
    body = {
        "filters": [
            {
                "condition": {
                    "column": "jobId",
                    "operator": "EQUALS",
                    "value": str(job_id),
                },
                "conjuction": "AND",
            }
        ]
    }
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        try:
            with _exchange(
                session,
                "POST",
                url,
                headers=get_auth_headers(token),
                params={"page": 0, "size": 1},
                json=body,
            ) as resp:
                content = (
                    _response_json(resp, "poll_job").get("data", {}).get("content")
                    or []
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(
                f"Job {job_id} poll failed ({type(exc).__name__}); retrying...",
                file=sys.stderr,
            )
            time.sleep(poll_interval)
            continue
        job = content[0] if content and isinstance(content[0], dict) else None
        seen = job.get("jobId") if job else None
        if (
            job is not None
            and seen is not None
            and _positive_int(seen, "jobId") == job_id
        ):
            status = _one_line(str(job.get("jobStatus") or ""), 40)
            if status != last_status:
                print(f"Job {job_id} status: {status}", file=sys.stderr)
                last_status = status
            if status in TERMINAL_STATUSES:
                return job
        time.sleep(poll_interval)
    detail = f" (last status {last_status})" if last_status else ""
    raise ImportIgpError(
        f"timed out waiting for job {job_id} after {timeout:.0f}s{detail}"
    )


def _write_spool(
    resp: requests.Response,
    spool: tempfile.SpooledTemporaryFile[bytes],
    *,
    limit: int,
    context: str,
) -> None:
    _content_length(resp, limit, context)
    total = 0
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ImportIgpError(f"{context} response exceeds {limit} bytes")
        spool.write(chunk)
    spool.seek(0)


def export_result_member(
    session: requests.Session,
    base_url: str,
    token: str,
    tar_file_id: int,
    member_name: str,
    keep_tar: str | None,
) -> bytes:
    """Download the result tar, optionally save it, and return one member."""
    url = f"{base_url}/cp/apiGateway/design/v1/file/export"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
    }
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES) as spool:
        with _exchange(
            session,
            "GET",
            url,
            headers=headers,
            params={"fileId": _positive_int(tar_file_id, "fileId")},
        ) as resp:
            check_response(resp, "export_userspace_file")
            _write_spool(
                resp,
                spool,
                limit=MAX_TRANSFER_BYTES,
                context="export_userspace_file",
            )
        if keep_tar:
            _copy_to_path(spool, keep_tar)
            print(f"Saved result tar to {keep_tar}", file=sys.stderr)
            spool.seek(0)
        return extract_member_from_tar(spool, member_name)


def _default_jwt_path(ip: str) -> Path:
    """Return the JWT path cw_get_jwt.py writes for this host.

    The filename keeps the host text the operator typed, after it has passed
    host validation, so it matches ``cw_get_jwt.py``.
    """
    _validate_host(ip)
    filename = _as_str(ip)
    if filename.startswith("[") and filename.endswith("]"):
        filename = filename[1:-1]
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError("controller host must be a hostname or IP address")
    return Path.home() / ".crosswork" / f"{filename}.jwt"


def _read_jwt_file(jwt_path: str) -> str:
    path = Path(jwt_path).expanduser()
    if not path.is_file():
        raise CrossworkAuthError(f"JWT file not found: {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"WARNING: JWT file {path} is readable by group or others; "
            f"chmod 600 {path}",
            file=sys.stderr,
        )
    if path.stat().st_size > MAX_TOKEN_CHARS:
        raise CrossworkAuthError(f"JWT file is too large: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise CrossworkAuthError(f"JWT file is empty: {path}")
    print(f"Using JWT from {path}", file=sys.stderr)
    return _validate_token(token, "JWT")


def _token_from_args(
    session: requests.Session,
    *,
    ip: str,
    port: int,
    username: str | None,
    password: str | None,
    jwt_path: str | None,
    verify_ssl: bool,
    timeout: int,
) -> str:
    """Resolve a bearer token the same way ``cp_archive.py`` does.

    ``--jwt`` is used when given. When both ``--username`` and ``--password``
    are omitted, ``~/.crosswork/<ip>.jwt`` is used if that file exists.
    Otherwise username and password come from CLI flags, environment
    variables, or an interactive prompt.
    """
    if jwt_path:
        return _read_jwt_file(str(Path(jwt_path).expanduser()))
    if username is None and password is None:
        default_jwt = _default_jwt_path(ip)
        if default_jwt.is_file():
            return _read_jwt_file(str(default_jwt))
    resolved_user, resolved_pass = _resolve_credentials(username, password)
    print(f"Authenticating to {_validate_host(ip)}...", file=sys.stderr)
    return get_jwt(
        ip,
        resolved_user,
        resolved_pass,
        port=port,
        verify_ssl=verify_ssl,
        timeout=timeout,
        session=session,
    )


def stage_empty_base_plan(
    session: requests.Session,
    base_url: str,
    token: str,
    file_name: str,
) -> tuple[int, str]:
    """Import an empty PLANFILE for parse_igp to build into."""
    name = _safe_basename(file_name)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_text(EMPTY_BASE_PLAN, encoding="utf-8")
        print(f"Importing empty base plan {name} to userspace...", file=sys.stderr)
        resp = import_to_userspace(
            session, base_url, token, str(path), file_name=name, category="PLANFILE"
        )
    return parse_import_file_info(resp, name)


def write_plan_output(data: bytes, out_file: str) -> None:
    """Write the extracted plan to ``-out-file`` or stdout when ``-``."""
    if _as_str(out_file) == "-":
        sys.stdout.buffer.write(data)
        return
    if not data:
        raise ImportIgpError("generated plan is empty")
    _copy_to_path(io.BytesIO(data), out_file)
    print(
        f"Wrote plan to {out_file} ({len(data)} bytes)",
        file=sys.stderr,
    )


def run_import_igp_job(
    *,
    ip: str,
    igp_protocol: str,
    database_file: str,
    out_file: str,
    plan_file: str | None = None,
    port: int = DEFAULT_PORT,
    username: str | None = None,
    password: str | None = None,
    jwt_path: str | None = None,
    verify_ssl: bool = True,
    http_timeout: int = DEFAULT_TIMEOUT,
    job_name: str | None = None,
    requested_profile: str = DEFAULT_PROFILE,
    job_priority: str = "HIGH",
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_JOB_TIMEOUT,
    keep_tar: str | None = None,
    isis_level: str | None = None,
    lic_product: str | None = None,
    use_dns: bool | None = None,
    area: str | None = None,
    find_ip_manage: bool | None = None,
    create_subnet_links: bool | None = None,
    get_segments: bool | None = None,
    add_node_tag: str | None = None,
    simple_txt_out_file: bool | None = None,
    verbosity: int | None = None,
    log_file: str | None = None,
    suppress_progress: bool | None = None,
    options_file: str | None = None,
    no_global_options: bool | None = None,
) -> dict[str, Any]:
    """Authenticate, submit parse_igp CLI_JOB, and write the generated plan."""
    if _as_str(database_file) == "-":
        raise ImportIgpError(
            "-database-file '-' (stdin) is not supported; "
            "provide a local path or userspace filename"
        )
    if plan_file is not None and _as_str(plan_file) == "-":
        raise ImportIgpError(
            "-plan-file '-' (stdin) is not supported; "
            "provide a local path or userspace filename"
        )
    if _as_str(out_file) != "-" and (not _as_str(out_file) or "\x00" in out_file):
        raise ValueError("invalid out_file")
    if keep_tar is not None and (not _as_str(keep_tar) or "\x00" in keep_tar):
        raise ValueError("invalid keep_tar path")

    host = _validate_host(ip)
    checked_port = _validate_port(port)
    http_limit = _positive_number("http_timeout", http_timeout, upper=MAX_HTTP_TIMEOUT)
    profile = _validate_profile(requested_profile)
    priority = _as_str(job_priority).upper()
    if priority not in JOB_PRIORITIES:
        raise ValueError(
            f"job_priority must be one of {JOB_PRIORITIES}, got: {job_priority!r}"
        )
    interval = _positive_number("poll_interval", poll_interval, upper=MAX_POLL_INTERVAL)
    job_timeout = _positive_number("timeout", timeout, upper=MAX_JOB_TIMEOUT)

    db_name = _safe_basename(database_file)
    engine_out = engine_out_file_name(out_file, database_file)
    base_name = (
        _safe_basename(plan_file)
        if plan_file is not None
        else _safe_basename(f"{Path(db_name).stem}_igp_base.txt")
    )
    if base_name == engine_out:
        raise ValueError("-plan-file and -out-file must have different basenames")
    if db_name == engine_out:
        raise ValueError("-database-file and -out-file must have different basenames")
    # Validate every option before touching the network.
    option_kwargs: dict[str, Any] = {
        "igp_protocol": igp_protocol,
        "isis_level": isis_level,
        "lic_product": lic_product,
        "use_dns": use_dns,
        "area": area,
        "find_ip_manage": find_ip_manage,
        "create_subnet_links": create_subnet_links,
        "get_segments": get_segments,
        "add_node_tag": add_node_tag,
        "simple_txt_out_file": simple_txt_out_file,
        "verbosity": verbosity,
        "log_file": log_file,
        "suppress_progress": suppress_progress,
        "options_file": options_file,
        "no_global_options": no_global_options,
    }
    build_parse_igp_options(database_file=db_name, **option_kwargs)
    protocol = _validate_igp_protocol(igp_protocol)
    name = job_name or f"CLI: parse_igp {protocol} {db_name} -> {engine_out}"

    base_url = f"https://{host}:{checked_port}"
    session = _create_session(verify_ssl=verify_ssl, timeout=http_limit)
    try:
        token = _token_from_args(
            session,
            ip=host,
            port=checked_port,
            username=username,
            password=password,
            jwt_path=jwt_path,
            verify_ssl=verify_ssl,
            timeout=int(http_limit),
        )
        db_id, db_stored = resolve_userspace_file(
            session,
            base_url,
            token,
            database_file,
            category="OTHERS",
            label="database",
        )
        stage_ids = [db_id]

        if plan_file is not None:
            plan_id, plan_stored = resolve_userspace_file(
                session,
                base_url,
                token,
                plan_file,
                category="PLANFILE",
                label="plan",
            )
        else:
            plan_id, plan_stored = stage_empty_base_plan(
                session, base_url, token, base_name
            )
        stage_ids.append(plan_id)

        extra = resolve_optional_local_file(
            session, base_url, token, options_file, category="OTHERS"
        )
        if extra:
            stage_ids.append(extra[0])

        options = build_parse_igp_options(database_file=db_stored, **option_kwargs)
        job_config = build_job_config(out_file=engine_out, options=options)

        shown_ids = ",".join(str(fid) for fid in stage_ids)
        print(f"Staging job input (fileId={shown_ids})...", file=sys.stderr)
        fs_id = stage_job_input(session, base_url, token, stage_ids)

        print(
            f"Submitting CLI_JOB parse_igp (fsId={_one_line(fs_id, 80)})...",
            file=sys.stderr,
        )
        submitted = submit_cli_job(
            session,
            base_url,
            token,
            job_name=name,
            fs_id=fs_id,
            job_config=job_config,
            in_plan_file_id=plan_stored,
            requested_profile=profile,
            job_priority=priority,
        )
        print(f"Submitted job {submitted}", file=sys.stderr)

        job = poll_job(
            session,
            base_url,
            token,
            submitted,
            poll_interval=interval,
            timeout=job_timeout,
        )
        status = _one_line(str(job.get("jobStatus") or ""), 40)
        try:
            tar_id = int(job.get("outputTarFileId") or 0)
        except (TypeError, ValueError):
            tar_id = 0
        if status != "COMPLETED" or tar_id <= 0:
            result = _one_line(str(job.get("jobResult") or ""), 300)
            detail = f" jobResult={result}" if result else ""
            raise ImportIgpError(
                f"job {submitted} ended with status={status} "
                f"outputTarFileId={tar_id}{detail}"
            )

        print(f"Exporting result tar fileId={tar_id}...", file=sys.stderr)
        plan_bytes = export_result_member(
            session,
            base_url,
            token,
            tar_id,
            engine_out,
            keep_tar,
        )
        write_plan_output(plan_bytes, out_file)
        return job
    finally:
        session.close()


def main() -> int:
    """Parse CLI arguments, run the parse_igp job, and return an exit status."""
    parser = argparse.ArgumentParser(
        description=(
            "Submit native parse_igp as a Crosswork Planning CLI_JOB. "
            "Builds a plan file from an OSPF or IS-IS link-state database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "\nCredentials: ~/.crosswork/<ip>.jwt when --username, --password, and\n"
            "--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,\n"
            f"{ENV_USERNAME}/{ENV_PASSWORD}, or an interactive prompt.\n"
            "\nparse_igp options (from parse_igp -help):\n"
            "\n  -igp-protocol, -database-file, -out-file   required\n"
            "  -plan-file                     existing plan to merge with (optional)\n"
            "  -isis-level                    1 | 2 (default) | both  (ISIS only)\n"
            "  -area                          area id or 'all' (default 0)\n"
            "  -lic-product                   Collector (default) | Design\n"
            "  -use-dns                       true | false (default false)\n"
            "  -find-ip-manage                true (default) | false\n"
            "  -create-subnet-links           true (default) | false\n"
            "  -get-segments                  true | false (default false)\n"
            "                                 ISIS on IOS XR only\n"
            "  -add-node-tag                  comma-separated tags for every node\n"
            "  plus generic: -simple-txt-out-file, -verbosity, -log-file,\n"
            "                -suppress-progress, -options-file, -no-global-options\n"
            "\nThe database file holds the router output of one of:\n"
            "  Cisco OSPF:    show ip ospf database router\n"
            "  Cisco ISIS:    show isis database verbose\n"
            "  Juniper OSPF:  show ospf database extensive\n"
            "  Juniper ISIS:  show isis database extensive\n"
            "\n-database-dir and stdin ('-') inputs are not supported over REST.\n"
            "\nFile names sent to the engine must be 1-255 characters of "
            "letters, digits,\n"
            "'.', '_' or '-', and must start with a letter or digit. Uploads and the\n"
            f"result tar are limited to {MAX_TRANSFER_BYTES} bytes.\n"
            "\nExamples:\n"
            '  %(prog)s --ip "$CW_HOST" -k -igp-protocol isis '
            "-database-file isis_db.txt -out-file /tmp/isis_plan.txt\n"
            "  %(prog)s --ip 198.18.134.229 -k -igp-protocol isis "
            "-database-file isis_db.txt -out-file isis_plan.pln "
            "-isis-level both -get-segments true\n"
            "  %(prog)s --ip 198.18.134.229 --jwt ~/.crosswork/198.18.134.229.jwt "
            "-igp-protocol ospf -database-file ospf_db.txt -area all "
            "-plan-file us_wan.txt -out-file us_wan_ospf.txt\n"
        ),
    )

    conn = parser.add_argument_group("Crosswork connection")
    conn.add_argument(
        "--ip",
        default=os.environ.get(ENV_HOST),
        help=f"Crosswork controller IP or hostname (or set {ENV_HOST})",
    )
    conn.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Crosswork HTTPS port (default: {DEFAULT_PORT})",
    )
    conn.add_argument(
        "--username",
        "-u",
        default=None,
        help=f"Username (or set {ENV_USERNAME})",
    )
    conn.add_argument(
        "--password",
        "-p",
        default=None,
        help=(
            f"Password (or set {ENV_PASSWORD}; "
            "prompting avoids exposing it in the process list)"
        ),
    )
    conn.add_argument(
        "--jwt",
        "-j",
        dest="jwt",
        default=None,
        metavar="<file>",
        help=(
            "Path to a JWT file (default when no credentials are given: "
            "~/.crosswork/<ip>.jwt)"
        ),
    )
    conn.add_argument(
        "--http-timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        metavar="<sec>",
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    conn.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (lab only; not recommended)",
    )

    job = parser.add_argument_group("Job control")
    job.add_argument(
        "--job-name",
        dest="job_name",
        default=None,
        metavar="<name>",
        help="Job name (default: CLI: parse_igp <protocol> <database> -> <out>)",
    )
    job.add_argument(
        "--profile",
        dest="requested_profile",
        default=DEFAULT_PROFILE,
        metavar="<profile>",
        help=f"requestedProfile (default: {DEFAULT_PROFILE})",
    )
    job.add_argument(
        "--job-priority",
        dest="job_priority",
        default="HIGH",
        choices=JOB_PRIORITIES,
        help="Job priority (default: HIGH)",
    )
    job.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        metavar="<sec>",
        help=f"Seconds between job-status polls (default: {DEFAULT_POLL_INTERVAL:g})",
    )
    job.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=DEFAULT_JOB_TIMEOUT,
        metavar="<sec>",
        help=(
            f"Max seconds to wait for job completion (default: {DEFAULT_JOB_TIMEOUT:g})"
        ),
    )
    job.add_argument(
        "--keep-tar",
        dest="keep_tar",
        default=None,
        metavar="<path>",
        help="Save the job result tar to this path",
    )

    parser.add_argument(
        "-igp-protocol",
        dest="igp_protocol",
        required=True,
        type=str.lower,
        choices=IGP_PROTOCOLS,
        metavar="<ospf|ospfv3|isis|isisv6>",
        help="IGP of the database",
    )
    parser.add_argument(
        "-database-file",
        dest="database_file",
        required=True,
        metavar="<filename>",
        help=(
            "Local database file (imported) or userspace OTHERS file name "
            "holding router 'show ... database' output"
        ),
    )
    parser.add_argument(
        "-out-file",
        dest="out_file",
        required=True,
        metavar="<filename>",
        help=(
            "Local output path; extension selects format (.pln, .txt, .db). "
            "Use '-' for stdout"
        ),
    )
    parser.add_argument(
        "-plan-file",
        dest="plan_file",
        default=None,
        metavar="<filename>",
        help="Existing plan to merge with: local path (imported) or userspace PLANFILE",
    )
    parser.add_argument(
        "-database-dir",
        dest="database_dir",
        default=None,
        metavar="<dirname>",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-isis-level",
        dest="isis_level",
        default=None,
        type=str.lower,
        choices=ISIS_LEVELS,
        metavar="<1|2|both>",
        help="ISIS level to read from the database (default 2 in tool)",
    )
    parser.add_argument(
        "-lic-product",
        dest="lic_product",
        default=None,
        type=str.capitalize,
        choices=LIC_PRODUCTS,
        metavar="<Collector|Design>",
        help="Base product license to check out (default Collector in tool)",
    )
    parser.add_argument(
        "-use-dns",
        dest="use_dns",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Resolve router IP addresses with DNS (default false in tool)",
    )
    parser.add_argument(
        "-area",
        dest="area",
        default=None,
        metavar="<area|all>",
        help="Numeric area to read, or 'all' (default 0 in tool)",
    )
    parser.add_argument(
        "-find-ip-manage",
        dest="find_ip_manage",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Find management IP addresses in the database (default true in tool)",
    )
    parser.add_argument(
        "-create-subnet-links",
        dest="create_subnet_links",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help=(
            "Create links by subnet matching even without adjacencies "
            "(default true in tool)"
        ),
    )
    parser.add_argument(
        "-get-segments",
        dest="get_segments",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help=(
            "Collect Segment Routing data (ISIS on IOS XR only; default false in tool)"
        ),
    )
    parser.add_argument(
        "-add-node-tag",
        dest="add_node_tag",
        default=None,
        metavar="<tag[,tag...]>",
        help="Comma-separated tags appended to every discovered node",
    )
    parser.add_argument(
        "-simple-txt-out-file",
        dest="simple_txt_out_file",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Remove empty tables/columns from .txt output (default false in tool)",
    )
    parser.add_argument(
        "-verbosity",
        dest="verbosity",
        type=int,
        default=None,
        metavar="<1-60>",
        help="Log verbosity (default 30 in tool)",
    )
    parser.add_argument(
        "-log-file",
        dest="log_file",
        default=None,
        metavar="<filename>",
        help="Log file for warnings and errors (created on the engine)",
    )
    parser.add_argument(
        "-suppress-progress",
        dest="suppress_progress",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Suppress progress output (default true in tool)",
    )
    parser.add_argument(
        "-options-file",
        dest="options_file",
        default=None,
        metavar="<filename>",
        help="Read additional options from file (local files are imported and staged)",
    )
    parser.add_argument(
        "-no-global-options",
        dest="no_global_options",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Do not load global options file",
    )

    parsed = parser.parse_args()

    if not parsed.ip:
        parser.error(f"--ip is required (or set {ENV_HOST})")
    if parsed.database_dir is not None:
        parser.error(
            "-database-dir is not supported over REST; "
            "pass a single -database-file per job"
        )
    if _as_str(parsed.database_file) == "-":
        parser.error(
            "-database-file '-' (stdin) is not supported; "
            "provide a local path or userspace filename"
        )
    if parsed.plan_file is not None and _as_str(parsed.plan_file) == "-":
        parser.error(
            "-plan-file '-' (stdin) is not supported; "
            "provide a local path or userspace filename"
        )
    if parsed.password:
        print(
            "WARNING: --password exposes the password in the process list; "
            "prefer a prompt or CW_PASSWORD",
            file=sys.stderr,
        )

    def as_bool(value: str | None) -> bool | None:
        if value is None:
            return None
        return _validate_bool(value) == "true"

    verify_ssl = not parsed.insecure
    if parsed.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

    try:
        run_import_igp_job(
            ip=parsed.ip,
            igp_protocol=parsed.igp_protocol,
            database_file=parsed.database_file,
            out_file=parsed.out_file,
            plan_file=parsed.plan_file,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
            jwt_path=parsed.jwt,
            verify_ssl=verify_ssl,
            http_timeout=parsed.http_timeout,
            job_name=parsed.job_name,
            requested_profile=parsed.requested_profile,
            job_priority=parsed.job_priority,
            poll_interval=parsed.poll_interval,
            timeout=parsed.timeout,
            keep_tar=parsed.keep_tar,
            isis_level=parsed.isis_level,
            lic_product=parsed.lic_product,
            use_dns=as_bool(parsed.use_dns),
            area=parsed.area,
            find_ip_manage=as_bool(parsed.find_ip_manage),
            create_subnet_links=as_bool(parsed.create_subnet_links),
            get_segments=as_bool(parsed.get_segments),
            add_node_tag=parsed.add_node_tag,
            simple_txt_out_file=as_bool(parsed.simple_txt_out_file),
            verbosity=parsed.verbosity,
            log_file=parsed.log_file,
            suppress_progress=as_bool(parsed.suppress_progress),
            options_file=parsed.options_file,
            no_global_options=as_bool(parsed.no_global_options),
        )
        return 0
    except (
        ImportIgpError,
        CrossworkAuthError,
        ValueError,
        requests.RequestException,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"Error: {_one_line(str(exc), 500)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
