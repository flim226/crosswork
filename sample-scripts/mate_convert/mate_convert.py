#!/usr/bin/env python3
"""
Submit native ``mate_convert`` as a Crosswork Planning CLI_JOB over REST.

Converts plan files between ``.txt`` / ``.pln`` / ``.db`` and between
schema/release versions by staging the plan in Design userspace, submitting
an immediate ``CLI_JOB`` (``script=mate_convert``), polling until completion,
and extracting the converted file from the result tar.

Tool options match CP 8.0 Build 385 ``mate_convert -help``. The engine
injects ``-plan-file`` / ``-out-file`` from ``inPlanFileId`` and
``jobConfig.outFile``; remaining flags go in ``jobConfig.options``.

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
VERSION_TYPES = ("release", "schema")
TO_VERSION_SPECIAL = ("current", "keep")
JOB_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "ABORTED", "CANCELLED")
FILE_CATEGORIES = frozenset({"PLANFILE", "OTHERS"})
OPTION_FLAGS = frozenset(
    {
        "-to-version",
        "-version-type",
        "-include-netint",
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
VERSION_RE = re.compile(r"^[0-9][0-9._-]{0,31}$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or an API call fails."""


class MateConvertError(RuntimeError):
    """Raised when mate_convert REST job staging, submit, or export fails."""


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
        raise MateConvertError(f"invalid {label}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MateConvertError(f"invalid {label}") from exc
    if number <= 0 or number > 2**63 - 1:
        raise MateConvertError(f"invalid {label}")
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
        raise MateConvertError("refusing non-HTTPS URL")


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
            raise MateConvertError("refusing non-HTTPS URL")
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
        raise MateConvertError(f"{context} returned an invalid Content-Length") from exc
    if size < 0 or size > limit:
        raise MateConvertError(f"{context} response exceeds {limit} bytes")


def _iter_body(resp: requests.Response, limit: int, context: str):
    _content_length(resp, limit, context)
    total = 0
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise MateConvertError(f"{context} response exceeds {limit} bytes")
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
        raise MateConvertError(f"{context} returned an empty body")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MateConvertError(f"{context} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise MateConvertError(f"{context} returned a JSON value that is not an object")
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


def _validate_to_version(value: str) -> str:
    """Allow current|keep or versions such as 6.4, 7.9, 7.10.-1."""
    text = _as_str(value)
    if not text:
        raise ValueError("to_version must be non-empty")
    if text.lower() in TO_VERSION_SPECIAL:
        return text.lower()
    if not VERSION_RE.fullmatch(text):
        raise ValueError(f"to_version contains unsupported characters: {value!r}")
    return text


def _validate_version_type(value: str) -> str:
    text = _as_str(value).lower()
    if text not in VERSION_TYPES:
        raise ValueError(f"version_type must be one of {VERSION_TYPES}, got: {value!r}")
    return text


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
        raise MateConvertError(f"invalid {label}")
    return text


def _add_bool(args: list[str], flag: str, value: bool | None) -> None:
    if value is not None:
        args.extend([flag, "true" if value else "false"])


def _checked_options(args: list[str]) -> list[str]:
    if len(args) % 2:
        raise ValueError("mate_convert options must be flag/value pairs")
    for flag, value in zip(args[0::2], args[1::2], strict=True):
        if flag not in OPTION_FLAGS or not SAFE_NAME_RE.fullmatch(value):
            raise ValueError("refusing an unsafe mate_convert option")
    option_str = " ".join(args)
    if len(option_str) > 4000:
        raise ValueError("mate_convert options are too long")
    return args


def build_mate_convert_options(
    *,
    to_version: str | None = None,
    version_type: str | None = None,
    include_netint: bool | None = None,
    simple_txt_out_file: bool | None = None,
    verbosity: int | None = None,
    log_file: str | None = None,
    suppress_progress: bool | None = None,
    options_file: str | None = None,
    no_global_options: bool | None = None,
) -> list[str]:
    """
    Build ``jobConfig.options`` flags for native mate_convert.

    Does not include ``-plan-file`` / ``-out-file``; the engine injects those
    from ``inPlanFileId`` and ``jobConfig.outFile``.
    """
    args: list[str] = []
    if to_version is not None:
        args.extend(["-to-version", _validate_to_version(to_version)])
    if version_type is not None:
        args.extend(["-version-type", _validate_version_type(version_type)])
    _add_bool(args, "-include-netint", include_netint)
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
    """Return the CLI_JOB ``jobConfig`` JSON string for native mate_convert."""
    checked = _checked_options(list(options or []))
    return json.dumps(
        {
            "scriptType": "CLI",
            "script": "mate_convert",
            "outFile": _safe_basename(out_file),
            "options": " ".join(checked),
        }
    )


def engine_out_file_name(out_file: str, plan_file: str) -> str:
    """Basename written on the engine; derived from ``-out-file`` or the plan."""
    dest = _as_str(out_file)
    if dest == "-":
        plan_name = _safe_basename(plan_file)
        stem = Path(plan_name).stem or "converted"
        suffix = Path(plan_name).suffix or ".txt"
        return _safe_basename(f"{stem}_converted{suffix}")
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
    raise MateConvertError("could not parse job id from the jobs response")


def parse_fs_id(resp: dict[str, Any]) -> str:
    """Read ``data.fsId`` from GET /jobs/input. Empty fsId is a failure."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    fs_id = str((data or {}).get("fsId") or "").strip()
    if (
        not fs_id
        or len(fs_id) > 1024
        or any(ch.isspace() or not ch.isprintable() for ch in fs_id)
    ):
        raise MateConvertError("jobs/input returned an invalid fsId")
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
            raise MateConvertError(
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
    raise MateConvertError("import response missing fileId")


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
            raise MateConvertError("result tar has too many members")
        if _tar_basename(member.name) != wanted:
            continue
        if not member.isreg():
            raise MateConvertError(
                f"{wanted!r} is not a regular file in the result tar"
            )
        if member.size <= 0 or member.size > MAX_TRANSFER_BYTES:
            raise MateConvertError(
                f"{wanted!r} in the result tar exceeds the size limit"
            )
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
                    raise MateConvertError(
                        f"{wanted!r} in the result tar exceeds the size limit"
                    )
                chunks.append(part)
                remaining -= len(part)
        if remaining:
            raise MateConvertError(f"{wanted!r} in the result tar was truncated")
        return b"".join(chunks)
    raise MateConvertError(f"{wanted!r} not found in job result tar")


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
        raise MateConvertError("job result is not a readable tar archive") from exc


def _local_file_size(path: Path) -> int:
    if not path.is_file():
        raise MateConvertError(f"file not found: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise MateConvertError(f"refusing to upload an empty file: {path.name}")
    if size > MAX_TRANSFER_BYTES:
        raise MateConvertError(f"file exceeds {MAX_TRANSFER_BYTES} bytes: {path.name}")
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
        raise MateConvertError("invalid userspace file category")
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
        raise MateConvertError("invalid output path")
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
        raise MateConvertError("invalid userspace file category")
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


def resolve_userspace_plan(
    session: requests.Session,
    base_url: str,
    token: str,
    plan_file: str,
) -> tuple[int, str]:
    """Import a local plan, or locate an existing userspace PLANFILE by basename."""
    local = Path(plan_file)
    basename = _safe_basename(plan_file)
    if local.is_file():
        print(f"Importing plan {basename} to userspace...", file=sys.stderr)
        resp = import_to_userspace(
            session,
            base_url,
            token,
            str(local),
            file_name=basename,
            category="PLANFILE",
        )
        return parse_import_file_info(resp, basename)

    found = search_userspace_file(
        session, base_url, token, basename, category="PLANFILE"
    )
    if found and found.get("fileId") is not None:
        return _positive_int(found["fileId"], "fileId"), _require_safe_name(
            str(found.get("fileName") or basename),
            "fileName",
        )
    raise MateConvertError(
        f"plan file not found locally or in userspace: {_one_line(plan_file, 200)}"
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
        raise MateConvertError("no file ids to stage")
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
        with _exchange(
            session,
            "POST",
            url,
            headers=get_auth_headers(token),
            params={"page": 0, "size": 1},
            json=body,
        ) as resp:
            content = (
                _response_json(resp, "poll_job").get("data", {}).get("content") or []
            )
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
    raise MateConvertError(
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
            raise MateConvertError(f"{context} response exceeds {limit} bytes")
        spool.write(chunk)
    spool.seek(0)


def export_converted_member(
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


def write_converted_output(data: bytes, out_file: str) -> None:
    """Write extracted convert output to ``-out-file`` or stdout when ``-``."""
    if _as_str(out_file) == "-":
        sys.stdout.buffer.write(data)
        return
    if not data:
        raise MateConvertError("converted plan is empty")
    _copy_to_path(io.BytesIO(data), out_file)
    print(
        f"Wrote converted plan to {out_file} ({len(data)} bytes)",
        file=sys.stderr,
    )


def run_mate_convert_job(
    *,
    ip: str,
    plan_file: str,
    out_file: str,
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
    to_version: str | None = None,
    version_type: str | None = None,
    include_netint: bool | None = None,
    simple_txt_out_file: bool | None = None,
    verbosity: int | None = None,
    log_file: str | None = None,
    suppress_progress: bool | None = None,
    options_file: str | None = None,
    no_global_options: bool | None = None,
) -> dict[str, Any]:
    """Authenticate, submit mate_convert CLI_JOB, and write the converted plan."""
    if _as_str(plan_file) == "-":
        raise MateConvertError(
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

    engine_out = engine_out_file_name(out_file, plan_file)
    options = build_mate_convert_options(
        to_version=to_version,
        version_type=version_type,
        include_netint=include_netint,
        simple_txt_out_file=simple_txt_out_file,
        verbosity=verbosity,
        log_file=log_file,
        suppress_progress=suppress_progress,
        options_file=options_file,
        no_global_options=no_global_options,
    )
    job_config = build_job_config(out_file=engine_out, options=options)
    name = job_name or f"CLI: mate_convert {_safe_basename(plan_file)} -> {engine_out}"

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
        file_id, stored_name = resolve_userspace_plan(
            session, base_url, token, plan_file
        )
        stage_ids = [file_id]
        extra = resolve_optional_local_file(
            session, base_url, token, options_file, category="OTHERS"
        )
        if extra:
            stage_ids.append(extra[0])

        print(f"Staging job input (fileId={file_id})...", file=sys.stderr)
        fs_id = stage_job_input(session, base_url, token, stage_ids)

        print(
            f"Submitting CLI_JOB mate_convert (fsId={_one_line(fs_id, 80)})...",
            file=sys.stderr,
        )
        submitted = submit_cli_job(
            session,
            base_url,
            token,
            job_name=name,
            fs_id=fs_id,
            job_config=job_config,
            in_plan_file_id=stored_name,
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
            raise MateConvertError(
                f"job {submitted} ended with status={status} "
                f"outputTarFileId={tar_id}{detail}"
            )

        print(f"Exporting result tar fileId={tar_id}...", file=sys.stderr)
        converted = export_converted_member(
            session,
            base_url,
            token,
            tar_id,
            engine_out,
            keep_tar,
        )
        write_converted_output(converted, out_file)
        return job
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Submit native mate_convert as a Crosswork Planning CLI_JOB. "
            "Converts plan files between .txt / .pln / .db and between "
            "schema/release versions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "\nCredentials: ~/.crosswork/<ip>.jwt when --username, --password, and\n"
            "--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,\n"
            f"{ENV_USERNAME}/{ENV_PASSWORD}, or an interactive prompt.\n"
            "\nCP 8.0 mate_convert options (from live -help on Build 385):\n"
            "\n  -plan-file, -out-file          required (stdin '-' is not supported)\n"
            "  -to-version                    current (default, 7.10.-1) | "
            "keep | e.g. 7.9\n"
            "  -version-type                  release (default) | schema\n"
            "  -include-netint                true (default) | false\n"
            "  -simple-txt-out-file           true | false (default false)\n"
            "  plus generic: -verbosity, -log-file, -suppress-progress,\n"
            "                -options-file, -no-global-options\n"
            "\nFile names sent to the engine must be 1-255 characters of "
            "letters, digits,\n"
            "'.', '_' or '-', and must start with a letter or digit. Uploads and the\n"
            f"result tar are limited to {MAX_TRANSFER_BYTES} bytes.\n"
            "\nExamples:\n"
            '  %(prog)s --ip "$CW_HOST" -k -plan-file us_wan.txt '
            "-out-file /tmp/us_wan.pln\n"
            "  %(prog)s --ip 198.18.134.229 -k -plan-file us_wan.txt "
            "-out-file us_wan.pln -to-version keep\n"
            "  %(prog)s --ip 198.18.134.229 --jwt ~/.crosswork/198.18.134.229.jwt "
            "-plan-file us_wan.txt -out-file us_wan_7.9.txt -to-version 7.9\n"
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
        help="Job name (default: CLI: mate_convert <plan> -> <out>)",
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
        "-plan-file",
        dest="plan_file",
        required=True,
        metavar="<filename>",
        help="Local plan path (imported) or userspace PLANFILE name",
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
        "-to-version",
        dest="to_version",
        default=None,
        metavar="<version>",
        help=(
            "Version to convert to: current (default), keep, "
            "or a release/schema id (e.g. 7.9)"
        ),
    )
    parser.add_argument(
        "-version-type",
        dest="version_type",
        default=None,
        choices=VERSION_TYPES,
        metavar="<release|schema>",
        help="How -to-version is interpreted (default release)",
    )
    parser.add_argument(
        "-include-netint",
        dest="include_netint",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Include <NetInt*> tables in output (default true in tool)",
    )
    parser.add_argument(
        "-simple-txt-out-file",
        dest="simple_txt_out_file",
        default=None,
        choices=BOOLEAN_STRINGS,
        metavar="<true|false>",
        help="Remove empty tables/columns from .txt output",
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
    if _as_str(parsed.plan_file) == "-":
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
        run_mate_convert_job(
            ip=parsed.ip,
            plan_file=parsed.plan_file,
            out_file=parsed.out_file,
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
            to_version=parsed.to_version,
            version_type=parsed.version_type,
            include_netint=as_bool(parsed.include_netint),
            simple_txt_out_file=as_bool(parsed.simple_txt_out_file),
            verbosity=parsed.verbosity,
            log_file=parsed.log_file,
            suppress_progress=as_bool(parsed.suppress_progress),
            options_file=parsed.options_file,
            no_global_options=as_bool(parsed.no_global_options),
        )
        return 0
    except (
        MateConvertError,
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
