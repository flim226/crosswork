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
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

import requests
import urllib3

BOOLEAN_STRINGS = ("true", "false")
VERSION_TYPES = ("release", "schema")
TO_VERSION_SPECIAL = ("current", "keep")
JOB_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "ABORTED", "CANCELLED")

DEFAULT_PORT = 30603
DEFAULT_TIMEOUT = 30
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
ENV_HOST = "CW_HOST"
DEFAULT_PROFILE = "4000m vCPU, 16Gi Mem"
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_JOB_TIMEOUT = 300
PROFILE_RE = re.compile(r"^\d+m vCPU,\s*\d+Gi Mem$")
JOB_ID_RE = re.compile(r"Job Id:\s*(\d+)", re.IGNORECASE)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or an API call fails."""


class MateConvertError(RuntimeError):
    """Raised when mate_convert REST job staging, submit, or export fails."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""

    def __init__(self, timeout: int):
        self._timeout = timeout
        super().__init__()

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(*args, **kwargs)


def _create_session(*, verify_ssl: bool, timeout: int) -> requests.Session:
    """Create an HTTP session with a default timeout and TLS verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter(timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError when an HTTP response is not successful."""
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
    raise CrossworkAuthError(f"{context} returned {status}: {body}")


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
        raise CrossworkAuthError(f"Could not extract ticket from response: {resp.text[:300]}")
    return ticket


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token."""
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
        raise CrossworkAuthError(f"Could not extract token from response: {resp.text[:300]}")
    return token


def get_jwt(
    ip: str,
    username: str,
    password: str,
    *,
    port: int = DEFAULT_PORT,
    verify_ssl: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Authenticate to Crosswork and return the JWT token string."""
    session = _create_session(verify_ssl=verify_ssl, timeout=timeout)
    base_url = f"https://{ip}:{port}"
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def _resolve_credentials(args) -> tuple:
    """Resolve username and password from CLI, environment, or a prompt.

    Priority: CLI argument, then environment variable, then interactive prompt.
    """
    username = args.username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")

    password = args.password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")

    return username, password


def get_auth_headers(token: str) -> dict:
    """Return bearer authorization headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _as_str(value: str) -> str:
    return str(value).strip()


def _validate_bool(value: str) -> str:
    v = value.strip().lower()
    if v not in BOOLEAN_STRINGS:
        raise ValueError(f"Expected one of {BOOLEAN_STRINGS}, got: {value!r}")
    return v


def _validate_to_version(value: str) -> str:
    """Allow current|keep or versions such as 6.4, 7.9, 7.10.-1."""
    v = _as_str(value)
    if not v:
        raise ValueError("to_version must be non-empty")
    if v.lower() in TO_VERSION_SPECIAL:
        return v.lower()
    allowed = set("0123456789._-")
    if any(ch not in allowed for ch in v):
        raise ValueError(f"to_version contains unsupported characters: {value!r}")
    return v


def _validate_version_type(value: str) -> str:
    v = _as_str(value).lower()
    if v not in VERSION_TYPES:
        raise ValueError(f"version_type must be one of {VERSION_TYPES}, got: {value!r}")
    return v


def _validate_profile(value: str) -> str:
    v = _as_str(value)
    if not PROFILE_RE.match(v):
        raise ValueError(
            f"requestedProfile must match '<N>m vCPU, <N>Gi Mem', got: {value!r}"
        )
    return v


def _safe_basename(path: str) -> str:
    name = Path(_as_str(path)).name
    if not name or name in (".", ".."):
        raise ValueError(f"invalid file name: {path!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"file name must not contain path separators: {path!r}")
    return name


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

    if include_netint is not None:
        args.extend(["-include-netint", "true" if include_netint else "false"])

    if simple_txt_out_file is not None:
        args.extend(
            ["-simple-txt-out-file", "true" if simple_txt_out_file else "false"]
        )

    if verbosity is not None:
        if not 1 <= verbosity <= 60:
            raise ValueError("verbosity must be between 1 and 60")
        args.extend(["-verbosity", str(verbosity)])

    if log_file is not None and str(log_file).strip():
        args.extend(["-log-file", _safe_basename(log_file)])

    if suppress_progress is not None:
        args.extend(["-suppress-progress", "true" if suppress_progress else "false"])

    if options_file is not None and str(options_file).strip():
        args.extend(["-options-file", _safe_basename(options_file)])

    if no_global_options is not None:
        args.extend(["-no-global-options", "true" if no_global_options else "false"])

    return args


def build_job_config(
    *,
    out_file: str,
    options: Sequence[str] | None = None,
) -> str:
    """Return the CLI_JOB ``jobConfig`` JSON string for native mate_convert."""
    engine_out = _safe_basename(out_file)
    option_str = " ".join(options) if options else ""
    return json.dumps(
        {
            "scriptType": "CLI",
            "script": "mate_convert",
            "outFile": engine_out,
            "options": option_str,
        }
    )


def engine_out_file_name(out_file: str, plan_file: str) -> str:
    """Basename written on the engine; derived from ``-out-file`` or the plan."""
    dest = _as_str(out_file)
    if dest == "-":
        stem = Path(_safe_basename(plan_file)).stem or "converted"
        suffix = Path(_safe_basename(plan_file)).suffix or ".txt"
        return f"{stem}_converted{suffix}"
    return _safe_basename(dest)


def parse_job_id(resp: dict[str, Any]) -> int:
    """Extract numeric job id from a POST /jobs envelope."""
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("jobId", "id"):
            if data.get(key) is not None:
                return int(data[key])
        msg = str(data.get("message") or "")
        match = JOB_ID_RE.search(msg)
        if match:
            return int(match.group(1))
    raise MateConvertError(f"could not parse job id from response: {resp!r}")


def parse_fs_id(resp: dict[str, Any]) -> str:
    """Read ``data.fsId`` from GET /jobs/input. Empty fsId is a failure."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    fs_id = str((data or {}).get("fsId") or "").strip()
    if not fs_id:
        raise MateConvertError(
            "jobs/input returned empty fsId (invalid or missing fileId)"
        )
    return fs_id


def parse_import_file_info(resp: dict[str, Any], fallback_name: str) -> tuple[int, str]:
    """Parse userspace fileId and stored name from a file/import envelope."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    extra: Any = (data or {}).get("additionalInfo")
    if isinstance(extra, str) and extra.strip():
        extra = json.loads(extra)
    if isinstance(extra, dict) and extra.get("fileId") is not None:
        stored = str(extra.get("fileName") or "").strip() or fallback_name
        return int(extra["fileId"]), stored
    if data and data.get("fileId") is not None:
        stored = str(data.get("fileName") or "").strip() or fallback_name
        return int(data["fileId"]), stored
    raise MateConvertError(f"import response missing fileId: {resp!r}")


def extract_member_from_tar(tar_bytes: bytes, member_name: str) -> bytes:
    """Return the contents of ``member_name`` (basename match) from a result tar."""
    wanted = _safe_basename(member_name)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or any(p == ".." for p in parts):
                continue
            if parts[-1] != wanted:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                break
            with handle:
                return handle.read()
    raise MateConvertError(f"{wanted!r} not found in job result tar")


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
    name = file_name or os.path.basename(file_path)
    data = {"fileName": name, "category": category}
    if tags:
        data["tags"] = tags

    url = f"{base_url}/cp/apiGateway/design/v1/file/import"
    with open(file_path, "rb") as handle:
        resp = session.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, handle)},
            data=data,
        )
    check_response(resp, "import_to_userspace")
    return resp.json()


def export_userspace_file(
    session: requests.Session,
    base_url: str,
    token: str,
    file_id: int,
    output_path: str,
) -> None:
    """Download a userspace file by fileId."""
    url = f"{base_url}/cp/apiGateway/design/v1/file/export"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
    }
    resp = session.get(url, headers=headers, params={"fileId": file_id})
    check_response(resp, "export_userspace_file")
    with open(output_path, "wb") as handle:
        handle.write(resp.content)


def search_userspace_file(
    session: requests.Session,
    base_url: str,
    token: str,
    file_name: str,
    *,
    category: str = "PLANFILE",
) -> dict[str, Any] | None:
    """Find a userspace file by exact fileName and category."""
    url = f"{base_url}/cp/apiGateway/design/v1/file/search"
    resp = session.get(
        url,
        headers=get_auth_headers(token),
        params={
            "page": 0,
            "size": 100,
            "fileCategories": category,
            "fileName": file_name,
        },
    )
    check_response(resp, "search_userspace_file")
    for entry in resp.json().get("data", {}).get("content", []):
        if entry.get("fileName") == file_name:
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
        return int(found["fileId"]), str(found.get("fileName") or basename)
    raise MateConvertError(
        f"plan file not found locally or in userspace: {plan_file}"
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
    file_ids: Sequence[int],
) -> str:
    """GET /design/v1/jobs/input?fileId=... and return a non-empty fsId."""
    url = f"{base_url}/cp/apiGateway/design/v1/jobs/input"
    params = [("fileId", str(fid)) for fid in file_ids]
    resp = session.get(url, headers=get_auth_headers(token), params=params)
    check_response(resp, "stage_job_input")
    return parse_fs_id(resp.json())


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
        "jobName": job_name,
        "jobType": "CLI_JOB",
        "fsId": fs_id,
        "jobConfig": job_config,
        "inPlanFileId": in_plan_file_id,
        "requestedProfile": requested_profile,
        "jobPriority": job_priority,
    }
    resp = session.post(url, headers=get_auth_headers(token), json=payload)
    check_response(resp, "submit_cli_job")
    return parse_job_id(resp.json())


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
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        resp = session.post(
            url,
            headers=get_auth_headers(token),
            params={"page": 0, "size": 1},
            json=body,
        )
        check_response(resp, "poll_job")
        content = resp.json().get("data", {}).get("content") or []
        job = content[0] if content else None
        if job and int(job.get("jobId") or 0) == job_id:
            status = str(job.get("jobStatus") or "")
            if status != last_status:
                print(f"Job {job_id} status: {status}", file=sys.stderr)
                last_status = status
            if status in TERMINAL_STATUSES:
                return job
        time.sleep(poll_interval)
    raise MateConvertError(
        f"timed out waiting for job {job_id} after {timeout:.0f}s"
        + (f" (last status {last_status})" if last_status else "")
    )


def download_result_tar(
    session: requests.Session,
    base_url: str,
    token: str,
    tar_file_id: int,
    keep_tar: str | None,
) -> bytes:
    """Export the job result tar; optionally copy it to ``keep_tar``."""
    fd, tmp_path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        export_userspace_file(session, base_url, token, tar_file_id, tmp_path)
        tar_bytes = Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if keep_tar:
        dest = Path(keep_tar)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(tar_bytes)
        print(f"Saved result tar to {dest}", file=sys.stderr)
    return tar_bytes


def _default_jwt_path(ip: str) -> str:
    """Return the JWT path cw_get_jwt.py writes for this host."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def _read_jwt_file(jwt_path: str) -> str:
    if not os.path.isfile(jwt_path):
        raise CrossworkAuthError(f"JWT file not found: {jwt_path}")
    with open(jwt_path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise CrossworkAuthError(f"JWT file is empty: {jwt_path}")
    print(f"Using JWT from {jwt_path}", file=sys.stderr)
    return token


def _token_from_args(
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
        return _read_jwt_file(os.path.expanduser(jwt_path))
    if username is None and password is None:
        default_jwt = _default_jwt_path(ip)
        if os.path.isfile(default_jwt):
            return _read_jwt_file(default_jwt)
    creds = argparse.Namespace(username=username, password=password)
    resolved_user, resolved_pass = _resolve_credentials(creds)
    print(f"Authenticating to {ip}...", file=sys.stderr)
    return get_jwt(
        ip,
        resolved_user,
        resolved_pass,
        port=port,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )


def write_converted_output(data: bytes, out_file: str) -> None:
    """Write extracted convert output to ``-out-file`` or stdout when ``-``."""
    if _as_str(out_file) == "-":
        sys.stdout.buffer.write(data)
        return
    dest = Path(out_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"Wrote converted plan to {dest} ({len(data)} bytes)", file=sys.stderr)


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
            "-plan-file '-' (stdin) is not supported; provide a local path or userspace filename"
        )

    profile = _validate_profile(requested_profile)
    priority = _as_str(job_priority).upper()
    if priority not in JOB_PRIORITIES:
        raise ValueError(f"job_priority must be one of {JOB_PRIORITIES}, got: {job_priority!r}")

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

    base_url = f"https://{ip}:{port}"
    session = _create_session(verify_ssl=verify_ssl, timeout=http_timeout)
    token = _token_from_args(
        ip=ip,
        port=port,
        username=username,
        password=password,
        jwt_path=jwt_path,
        verify_ssl=verify_ssl,
        timeout=http_timeout,
    )

    file_id, stored_name = resolve_userspace_plan(session, base_url, token, plan_file)
    stage_ids = [file_id]
    extra = resolve_optional_local_file(
        session, base_url, token, options_file, category="OTHERS"
    )
    if extra:
        stage_ids.append(extra[0])

    print(f"Staging job input (fileId={file_id})...", file=sys.stderr)
    fs_id = stage_job_input(session, base_url, token, stage_ids)

    print(f"Submitting CLI_JOB mate_convert (fsId={fs_id})...", file=sys.stderr)
    job_id = submit_cli_job(
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
    print(f"Submitted job {job_id}", file=sys.stderr)

    job = poll_job(
        session,
        base_url,
        token,
        job_id,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    status = str(job.get("jobStatus") or "")
    tar_id = int(job.get("outputTarFileId") or 0)
    if status != "COMPLETED" or tar_id <= 0:
        result = job.get("jobResult") or ""
        raise MateConvertError(
            f"job {job_id} ended with status={status} outputTarFileId={tar_id}"
            + (f" jobResult={result}" if result else "")
        )

    print(f"Exporting result tar fileId={tar_id}...", file=sys.stderr)
    tar_bytes = download_result_tar(session, base_url, token, tar_id, keep_tar)
    converted = extract_member_from_tar(tar_bytes, engine_out)
    write_converted_output(converted, out_file)
    return job


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Submit native mate_convert as a Crosswork Planning CLI_JOB. "
            "Converts plan files between .txt / .pln / .db and between "
            "schema/release versions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Credentials: ~/.crosswork/<ip>.jwt when --username, --password, and
--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,
{ENV_USERNAME}/{ENV_PASSWORD}, or an interactive prompt.

CP 8.0 mate_convert options (from live -help on Build 385):

  -plan-file, -out-file          required (stdin '-' is not supported)
  -to-version                    current (default, 7.10.-1) | keep | e.g. 7.9
  -version-type                  release (default) | schema
  -include-netint                true (default) | false
  -simple-txt-out-file           true | false (default false)
  plus generic: -verbosity, -log-file, -suppress-progress,
                -options-file, -no-global-options

Examples:
  %(prog)s --ip "$CW_HOST" -k -plan-file us_wan.txt -out-file /tmp/us_wan.pln
  %(prog)s --ip 198.18.134.229 -k -plan-file us_wan.txt -out-file us_wan.pln -to-version keep
  %(prog)s --ip 198.18.134.229 --jwt ~/.crosswork/198.18.134.229.jwt -plan-file us_wan.txt -out-file us_wan_7.9.txt -to-version 7.9
        """,
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
        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)",
    )
    conn.add_argument(
        "--jwt",
        "-j",
        dest="jwt",
        default=None,
        metavar="<file>",
        help="Path to a JWT file (default when no credentials are given: ~/.crosswork/<ip>.jwt)",
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
        help=f"Max seconds to wait for job completion (default: {DEFAULT_JOB_TIMEOUT:g})",
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
        help="Local output path; extension selects format (.pln, .txt, .db). Use '-' for stdout",
    )
    parser.add_argument(
        "-to-version",
        dest="to_version",
        default=None,
        metavar="<version>",
        help="Version to convert to: current (default), keep, or a release/schema id (e.g. 7.9)",
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
            "-plan-file '-' (stdin) is not supported; provide a local path or userspace filename"
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
    except (MateConvertError, CrossworkAuthError, ValueError, requests.RequestException, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
