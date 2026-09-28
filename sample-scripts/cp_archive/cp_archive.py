#!/usr/bin/env python3
"""Import or export plan files for a Crosswork Planning archive.

Self-contained client: connection options come first, then exactly one of
--import or --export. This script does not import the other archive scripts.

  cp_archive.py --ip IP [--port PORT] [--username USER] [--password PASS] [--jwt FILE]
                --import [options] FILE_OR_DIR [...]

  cp_archive.py --ip IP [--port PORT] [--username USER] [--password PASS] [--jwt FILE]
                --export [options]
"""

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import urllib3

DEFAULT_PORT = 30603
IMPORT_TIMEOUT = 60
EXPORT_TIMEOUT = 120
DEFAULT_WORKERS = 8

ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"

SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")
ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9_@.&+\-]+$")
ARCHIVE_NAME_MAX = 100
TIMESTAMP_NAME = re.compile(r"^\d{8}_\d{4}_UTC\.pln$")
RETRY_STATUS = {429, 500, 502, 503, 504}


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or an archive API call fails."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to every request."""

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
    timeout: int = IMPORT_TIMEOUT,
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


def list_archives(session: requests.Session, base_url: str, token: str) -> dict:
    """List archives on the controller."""
    url = f"{base_url}/cp/apiGateway/v1/archives"
    resp = session.get(url, headers=get_auth_headers(token))
    check_response(resp, "list_archives")
    return resp.json()


def archive_exists(session: requests.Session, base_url: str, token: str, archive: str) -> bool:
    """Return True when ``archive`` is present on the controller."""
    body = list_archives(session, base_url, token)
    archives = (body.get("data") or {}).get("archives") or []
    return any(item.get("name") == archive for item in archives)


def validate_archive_name(archive: str) -> None:
    """Reject a name the create-archive API will not accept."""
    if not archive or len(archive) > ARCHIVE_NAME_MAX or not ARCHIVE_NAME.fullmatch(archive):
        raise CrossworkAuthError(
            f"archive name {archive!r} must be 1-{ARCHIVE_NAME_MAX} characters "
            "from A-Z, a-z, 0-9, and _ @ . & + -"
        )


def create_archive(
    session: requests.Session,
    base_url: str,
    token: str,
    archive: str,
) -> dict:
    """Create an archive with PUT /cp/apiGateway/v1/archives."""
    validate_archive_name(archive)
    url = f"{base_url}/cp/apiGateway/v1/archives"
    resp = session.put(
        url,
        headers=get_auth_headers(token),
        json={"name": archive},
    )
    check_response(resp, f"create archive {archive}")
    payload = resp.json()
    errors = payload.get("errors") or []
    if errors:
        raise CrossworkAuthError(f"create archive {archive} returned errors: {errors}")
    return payload


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
    print(f"Using JWT from {jwt_path}")
    return token


def _token_from_args(args, verify_ssl: bool, timeout: int) -> str:
    if args.jwt:
        return _read_jwt_file(os.path.expanduser(args.jwt))
    if args.username is None and args.password is None:
        default_jwt = _default_jwt_path(args.ip)
        if os.path.isfile(default_jwt):
            return _read_jwt_file(default_jwt)
    username, password = _resolve_credentials(args)
    print(f"Authenticating to {args.ip}...")
    return get_jwt(
        args.ip,
        username,
        password,
        port=args.port,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )


def connect(args, timeout: int, *, create_if_missing: bool = False):
    """Authenticate, open a session, and confirm the archive exists.

    ``create_if_missing`` creates the archive on --import when the controller
    does not already have it.
    """
    verify_ssl = not args.insecure
    if args.insecure:
        print("WARNING: TLS verification disabled", file=sys.stderr)
    token = _token_from_args(args, verify_ssl, timeout)
    session = _create_session(verify_ssl=verify_ssl, timeout=timeout)
    base_url = f"https://{args.ip}:{args.port}"
    if not archive_exists(session, base_url, token, args.archive):
        if not create_if_missing:
            raise CrossworkAuthError(
                f"archive {args.archive!r} does not exist on {args.ip}"
            )
        print(f"Archive {args.archive!r} does not exist; creating it")
        payload = create_archive(session, base_url, token, args.archive)
        created = (payload.get("data") or {}).get("name") or args.archive
        print(f"Created archive {created}")
    return verify_ssl, token, session, base_url


def _thread_session(local: threading.local, verify_ssl: bool, timeout: int) -> requests.Session:
    existing = getattr(local, "session", None)
    if existing is None:
        existing = _create_session(verify_ssl=verify_ssl, timeout=timeout)
        local.session = existing
    return existing


def _safe_token(value) -> str:
    """Return a single path segment safe to embed in a local filename."""
    text = re.sub(r"[^A-Za-z0-9._+-]", "_", str(value)).strip("._")
    if not text or text in {".", ".."}:
        return "plan"
    return text


def parse_bound(value: str, *, end_of_day: bool) -> datetime:
    """Parse a CLI date or timestamp into an aware UTC datetime."""
    text = value.strip()
    if "T" in text:
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CrossworkAuthError(f"invalid timestamp {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CrossworkAuthError(
            f"invalid date {value!r} (use YYYY-MM-DD or RFC3339)"
        ) from exc
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


def format_api_time(value: datetime) -> str:
    """Format a UTC datetime the archive search API accepts."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")


def resolve_window(args) -> tuple[datetime | None, datetime | None]:
    """Return the inclusive UTC window, or (None, None) when no date filter applies."""
    if args.lastdays is not None:
        if args.lastdays < 1:
            raise CrossworkAuthError("--lastdays must be a positive number of days")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.lastdays)
        return start, end
    start = parse_bound(args.from_date, end_of_day=False) if args.from_date else None
    end = parse_bound(args.to_date, end_of_day=True) if args.to_date else None
    if start and end and start > end:
        raise CrossworkAuthError("--from is after --to")
    return start, end


def select_latest(plans: list[dict], count: int) -> list[dict]:
    """Return the newest ``count`` plans by timestamp, then plan id."""
    ordered = sorted(
        plans,
        key=lambda item: (item.get("timestamp") or "", item.get("id") or 0),
        reverse=True,
    )
    return ordered[:count]


def search_plans(
    session: requests.Session,
    base_url: str,
    token: str,
    archive: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict]:
    """List archived plans, optionally limited by fromTst/toTst."""
    url = f"{base_url}/cp/apiGateway/v1/archives/{quote(archive, safe='')}/plans"
    params = {}
    if start is not None:
        params["fromTst"] = format_api_time(start)
    if end is not None:
        params["toTst"] = format_api_time(end)
    resp = session.get(url, headers=get_auth_headers(token), params=params or None)
    check_response(resp, "search_plans")
    payload = resp.json()
    plans = (payload.get("data") or {}).get("planfiles") or []
    return [plan for plan in plans if _in_window(plan.get("timestamp"), start, end)]


def _in_window(timestamp: str | None, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    if not timestamp:
        return False
    text = timestamp.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def safe_download_name(remote_path: str, plan_id) -> str:
    """Use only the final path segment, and only when it is a plain filename."""
    name = os.path.basename((remote_path or "").replace("\\", "/"))
    if name in {".", ".."} or not name or not SAFE_NAME.fullmatch(name):
        return f"plan_{_safe_token(plan_id)}.pln"
    return name


def assign_destinations(plans: list[dict], output_dir: str) -> list[tuple[dict, str]]:
    """Map each plan to a local path. Disambiguate basename collisions with the plan id."""
    used: dict[str, object] = {}
    assigned = []
    for plan in plans:
        plan_id = plan.get("id")
        name = safe_download_name(plan.get("path") or "", plan_id)
        if name in used and used[name] != plan_id:
            stem, ext = os.path.splitext(name)
            ext = ext or ".pln"
            name = f"{stem}_{_safe_token(plan_id)}{ext}"
            suffix = 2
            while name in used and used[name] != plan_id:
                name = f"{stem}_{_safe_token(plan_id)}_{suffix}{ext}"
                suffix += 1
        if name in {".", ".."}:
            name = f"plan_{_safe_token(plan_id)}.pln"
        used[name] = plan_id
        assigned.append((plan, os.path.join(output_dir, name)))
    return assigned


def download_plan(
    session: requests.Session,
    base_url: str,
    token: str,
    archive: str,
    plan_id,
    dest_path: str,
    expected_size: int | None,
) -> dict:
    """Download one plan to ``dest_path``. Retries transient failures."""
    url = (
        f"{base_url}/cp/apiGateway/v1/archives/{quote(archive, safe='')}"
        f"/plans/{quote(str(plan_id), safe='')}/download"
    )
    headers = get_auth_headers(token)
    headers["Accept"] = "*/*"
    partial = dest_path + ".partial"
    last_error = None
    for attempt in range(1, 4):
        try:
            with session.get(url, headers=headers, stream=True) as resp:
                if resp.status_code in RETRY_STATUS and attempt < 3:
                    last_error = CrossworkAuthError(
                        f"download {plan_id} returned HTTP {resp.status_code}"
                    )
                    time.sleep(1.5 * attempt)
                    continue
                if not resp.ok:
                    check_response(resp, f"download {plan_id}")
                written = 0
                with open(partial, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
            os.replace(partial, dest_path)
            mismatch = expected_size is not None and written != expected_size
            return {
                "id": plan_id,
                "file": os.path.basename(dest_path),
                "bytes": written,
                "expected": expected_size,
                "size_mismatch": mismatch,
            }
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            _remove(partial)
            time.sleep(1.5 * attempt)
    _remove(partial)
    raise CrossworkAuthError(f"download {plan_id} failed after retries: {last_error}")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def progress_bar(done: int, total: int, width: int = 30) -> str:
    """Return a fixed-width bar that fills once per completed plan."""
    if width < 1:
        return ""
    if total <= 0 or done >= total:
        return "#" * width
    filled = width * done // total
    if done > 0 and filled == 0:
        filled = 1
    if filled >= width:
        filled = width - 1
    return "#" * filled + "-" * (width - filled)


def format_progress(label: str, done: int, total: int, *, ok: int, failed: int,
                    mismatch: int, elapsed: float, name: str) -> str:
    """Format one progress line. ``done`` is the number of plans finished."""
    parts = [
        f"{label} [{progress_bar(done, total)}] {done}/{total}",
        f"ok={ok}",
        f"fail={failed}",
    ]
    if mismatch:
        parts.append(f"size_mismatch={mismatch}")
    parts.append(f"{elapsed:.0f}s")
    if name:
        parts.append(name)
    return " ".join(parts)


class TransferProgress:
    """Redraw a one-line bar as each plan transfer finishes.

    On a terminal the line is updated in place. Otherwise each finished plan
    is written on its own line so a log still shows every transfer.
    """

    def __init__(self, total: int, label: str):
        self.total = total
        self.label = label
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.mismatch = 0
        self.started = time.time()
        self._tty = sys.stderr.isatty()
        self._last_len = 0
        self._closed = False
        self._lock = threading.Lock()
        self._render("")

    def update(self, name: str, *, ok: bool = False, failed: bool = False,
               mismatch: bool = False, error: str | None = None) -> None:
        """Count one finished plan and redraw."""
        with self._lock:
            self.done += 1
            if failed:
                self.failed += 1
            elif mismatch:
                self.mismatch += 1
            elif ok:
                self.ok += 1
            if error:
                self._erase()
                print(f"FAIL {name}: {error}", file=sys.stderr)
            self._render(name)

    def close(self) -> None:
        """Leave the last progress line in place and move to the next line."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._tty:
                sys.stderr.write("\n")
                sys.stderr.flush()

    def _render(self, name: str) -> None:
        line = format_progress(
            self.label,
            self.done,
            self.total,
            ok=self.ok,
            failed=self.failed,
            mismatch=self.mismatch,
            elapsed=time.time() - self.started,
            name=name,
        )
        if not self._tty:
            print(line, file=sys.stderr)
            return
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if len(line) >= cols:
            line = line[:cols - 1]
        pad = " " * max(0, self._last_len - len(line))
        sys.stderr.write("\r" + line + pad)
        sys.stderr.flush()
        self._last_len = len(line)

    def _erase(self) -> None:
        if self._tty and self._last_len:
            sys.stderr.write("\r" + " " * self._last_len + "\r")
            sys.stderr.flush()
            self._last_len = 0


def run_export(args, timeout: int) -> None:
    """Download plans from an archive into a local directory."""
    start, end = resolve_window(args)
    verify_ssl, token, session, base_url = connect(args, timeout)
    plans = search_plans(session, base_url, token, args.archive, start, end)
    matched = len(plans)
    if args.last is not None:
        plans = select_latest(plans, args.last)
    plans.sort(key=lambda item: (item.get("timestamp") or "", item.get("id") or 0))
    if args.last is not None:
        print(
            f"Listed {matched} plan(s) in {args.archive}; "
            f"exporting the latest {len(plans)}"
        )
    else:
        print(f"Listed {len(plans)} plan(s) in {args.archive}")
    if not plans:
        return

    output_dir = os.path.abspath(args.output or f"{args.ip}-{args.archive}-export")
    os.makedirs(output_dir, mode=0o755, exist_ok=True)
    jobs = assign_destinations(plans, output_dir)
    local = threading.local()

    def _one(plan: dict, dest: str) -> dict:
        rec = download_plan(
            _thread_session(local, verify_ssl, timeout),
            base_url,
            token,
            args.archive,
            plan.get("id"),
            dest,
            plan.get("size"),
        )
        rec["timestamp"] = plan.get("timestamp")
        return rec

    ok = 0
    failed = 0
    mismatches = 0
    results = []
    progress = TransferProgress(len(jobs), "export")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_one, plan, dest): (plan, dest) for plan, dest in jobs}
            for future in as_completed(futures):
                plan, dest = futures[future]
                name = os.path.basename(dest)
                try:
                    rec = future.result()
                except (CrossworkAuthError, requests.RequestException, OSError) as exc:
                    failed += 1
                    results.append({
                        "id": plan.get("id"),
                        "file": name,
                        "status": "failed",
                        "error": str(exc),
                    })
                    progress.update(name, failed=True, error=str(exc))
                    continue
                status = "size_mismatch" if rec["size_mismatch"] else "ok"
                if rec["size_mismatch"]:
                    mismatches += 1
                else:
                    ok += 1
                results.append({
                    "id": rec["id"],
                    "file": rec["file"],
                    "status": status,
                    "bytes": rec["bytes"],
                    "timestamp": rec.get("timestamp"),
                })
                progress.update(
                    name,
                    ok=status == "ok",
                    mismatch=rec["size_mismatch"],
                )
    finally:
        progress.close()

    manifest = {
        "archive": args.archive,
        "fromTst": format_api_time(start) if start else None,
        "toTst": format_api_time(end) if end else None,
        "last": args.last,
        "lastdays": args.lastdays,
        "output": output_dir,
        "listed": len(plans),
        "ok": ok,
        "size_mismatch": mismatches,
        "failed": failed,
        "results": sorted(results, key=lambda item: (item.get("timestamp") or "", item.get("id") or 0)),
    }
    manifest_path = os.path.join(output_dir, "download_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    print(
        f"Exported {ok}/{len(plans)} to {output_dir}; "
        f"failed {failed}; size_mismatch {mismatches}"
    )
    print(f"Manifest: {manifest_path}")
    if failed or mismatches:
        sys.exit(1)


def collect_inputs(paths: list[str]) -> list[str]:
    """Expand file paths and directories into a sorted list of plan files.

    A directory contributes the ``*.pln`` files in that directory only.
    """
    found: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            names = sorted(
                name for name in os.listdir(path)
                if name.endswith(".pln") and os.path.isfile(os.path.join(path, name))
            )
            if not names:
                raise CrossworkAuthError(f"no .pln files in directory: {path}")
            candidates = [os.path.join(path, name) for name in names]
        elif os.path.isfile(path):
            candidates = [path]
        else:
            raise CrossworkAuthError(f"file not found: {raw}")
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    if not found:
        raise CrossworkAuthError("no plan files to import")
    return found


def import_plan(
    session: requests.Session,
    base_url: str,
    token: str,
    archive: str,
    file_path: str,
) -> dict:
    """Import one plan file. Retries transient HTTP failures."""
    name = os.path.basename(file_path)
    url = f"{base_url}/cp/apiGateway/v1/archives/{quote(archive, safe='')}/import"
    data = {"filename": name}
    if TIMESTAMP_NAME.match(name):
        data["useFileNameAsTimestamp"] = "true"
    headers = get_auth_headers(token)
    last_error = None
    for attempt in range(1, 4):
        try:
            with open(file_path, "rb") as handle:
                resp = session.post(
                    url,
                    headers=headers,
                    data=data,
                    files={"planfile": (name, handle, "application/octet-stream")},
                )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1.5 * attempt)
            continue
        if resp.status_code in RETRY_STATUS and attempt < 3:
            last_error = CrossworkAuthError(
                f"import {name} returned HTTP {resp.status_code}"
            )
            time.sleep(1.5 * attempt)
            continue
        check_response(resp, f"import {name}")
        payload = resp.json()
        errors = payload.get("errors") or []
        if errors:
            raise CrossworkAuthError(f"import {name} returned errors: {errors}")
        return payload
    raise CrossworkAuthError(f"import {name} failed after retries: {last_error}")


def run_import(args, timeout: int) -> None:
    """Upload plan files into an archive."""
    files = collect_inputs(args.filepath)
    verify_ssl, token, _session, base_url = connect(args, timeout, create_if_missing=True)
    print(f"Importing {len(files)} file(s) into {args.archive}")
    local = threading.local()

    def _one(path: str) -> dict:
        payload = import_plan(
            _thread_session(local, verify_ssl, timeout),
            base_url,
            token,
            args.archive,
            path,
        )
        data = payload.get("data") or {}
        return {
            "file": os.path.basename(path),
            "id": data.get("id"),
            "path": data.get("path"),
            "timestamp": data.get("timestamp"),
            "size": data.get("size"),
        }

    ok = 0
    failed: list[str] = []
    progress = TransferProgress(len(files), "import")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_one, path): path for path in files}
            for future in as_completed(futures):
                path = futures[future]
                name = os.path.basename(path)
                try:
                    rec = future.result()
                except (CrossworkAuthError, requests.RequestException, OSError, ValueError) as exc:
                    failed.append(name)
                    progress.update(name, failed=True, error=str(exc))
                    continue
                ok += 1
                progress.update(f"{name} id={rec['id']}", ok=True)
    finally:
        progress.close()

    print(f"Imported {ok}/{len(files)} into {args.archive}; failed {len(failed)}")
    if failed:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Connection options lead, then --import or --export."""
    parser = argparse.ArgumentParser(
        prog="cp_archive.py",
        description=(
            "Import or export plan files for a Crosswork Planning archive. "
            "Pass the connection options first, then exactly one of --import or --export."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        usage=(
            "%(prog)s --ip IP [--port PORT] [--username USER] [--password PASS] [--jwt FILE]\n"
            "                     --import [-k] --archive NAME [--timeout SEC] [--workers N]\n"
            "                     FILE_OR_DIR [FILE_OR_DIR ...]\n"
            "       %(prog)s --ip IP [--port PORT] [--username USER] [--password PASS] [--jwt FILE]\n"
            "                     --export [-k] --archive NAME [--from DATE] [--to DATE] [--last N]\n"
            "                     [--lastdays DAYS] [-o DIR] [--timeout SEC] [--workers N]"
        ),
        epilog=(
            "Credentials: ~/.crosswork/<ip>.jwt when --username, --password, and\n"
            "--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,\n"
            f"{ENV_USERNAME}/{ENV_PASSWORD}, or an interactive prompt.\n"
            "\n"
            "--import reads plan files and directories of *.pln files. If the archive\n"
            "is not on the server, it is created first. Filenames of the form\n"
            "YYYYMMDD_HHMM_UTC.pln keep that minute as the archive timestamp.\n"
            "\n"
            "--export selects plans with one of --from/--to, --last, or --lastdays.\n"
            "Omit all three to export every plan in the archive. Dates are UTC. A --to\n"
            "date without a time covers that whole UTC day.\n"
            "\n"
            "Examples:\n"
            "  cp_archive.py --ip 192.0.2.10 --jwt ~/.crosswork/192.0.2.10.jwt --export -k \\\n"
            "      --archive sr_topo_igp --last 10 -o ./192.0.2.10-sr_topo_igp-export\n"
            "  cp_archive.py --ip 192.0.2.10 --export -k --archive sr_topo_igp --lastdays 7\n"
            "  cp_archive.py --ip 192.0.2.10 --export -k --archive sr_topo_igp \\\n"
            "      --from 2026-09-17 --to 2026-09-25\n"
            "  cp_archive.py --ip 192.0.2.10 --jwt ~/.crosswork/192.0.2.10.jwt --import -k \\\n"
            "      --archive sr_topo_igp sr_topo_igp/\n"
            "  cp_archive.py --ip 192.0.2.10 --import -k --archive sr_topo_igp plan_a.pln plan_b.pln"
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

    mode = parser.add_argument_group(
        "mode",
        "Required. Pass exactly one of these immediately after the connection options.",
    )
    mode.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="Import plan files into the archive named by --archive",
    )
    mode.add_argument(
        "--export",
        dest="do_export",
        action="store_true",
        help="Export plan files from the archive named by --archive",
    )

    common = parser.add_argument_group("common options")
    common.add_argument(
        "-k", "--insecure",
        action="store_true",
        help="Disable TLS certificate verification",
    )
    common.add_argument(
        "--archive",
        required=True,
        help="Archive name. --import creates the archive when it is missing",
    )
    common.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"HTTP timeout in seconds (default: {IMPORT_TIMEOUT} for --import, {EXPORT_TIMEOUT} for --export)",
    )
    common.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel uploads or downloads (default: {DEFAULT_WORKERS})",
    )

    exporting = parser.add_argument_group("export options")
    exporting.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help="Window start, UTC (YYYY-MM-DD or RFC3339). --export only",
    )
    exporting.add_argument(
        "--to",
        dest="to_date",
        default=None,
        help="Window end, UTC (YYYY-MM-DD or RFC3339). --export only",
    )
    exporting.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="LAST",
        help="Export the newest LAST plan files",
    )
    exporting.add_argument(
        "--lastdays",
        type=int,
        default=None,
        metavar="DAYS",
        help="Export plans from the most recent DAYS days (UTC)",
    )
    exporting.add_argument(
        "-o", "--output",
        default=None,
        help="Output directory (default: ./IP-ARCHIVE-export)",
    )

    importing = parser.add_argument_group("import files")
    importing.add_argument(
        "filepath",
        nargs="*",
        help="Plan file, several plan files, or a directory of .pln files. Required with --import",
    )

    help_group = parser.add_argument_group("help")
    help_group.add_argument(
        "-h", "--help",
        action="help",
        help="Show this help message and exit",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.do_import == args.do_export:
        parser.error(
            "specify exactly one of --import or --export after the connection options"
        )
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.do_import:
        rejected = []
        if args.from_date or args.to_date:
            rejected.append("--from/--to")
        if args.last is not None:
            rejected.append("--last")
        if args.lastdays is not None:
            rejected.append("--lastdays")
        if args.output is not None:
            rejected.append("--output/-o")
        if rejected:
            parser.error("--export options " + ", ".join(rejected) + " were given with --import")
        if not args.filepath:
            parser.error("--import requires at least one plan file or directory")
        return
    if args.filepath:
        parser.error("plan file arguments belong with --import")
    date_filter = bool(args.from_date or args.to_date)
    if args.last is not None and args.last < 1:
        parser.error("--last must be a positive number of files")
    if sum(bool(x) for x in (args.last is not None, args.lastdays is not None, date_filter)) > 1:
        parser.error("use only one of --from/--to, --last, or --lastdays")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    timeout = args.timeout if args.timeout is not None else (
        EXPORT_TIMEOUT if args.do_export else IMPORT_TIMEOUT
    )
    try:
        if args.do_export:
            run_export(args, timeout)
        else:
            run_import(args, timeout)
    except (CrossworkAuthError, requests.RequestException, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
