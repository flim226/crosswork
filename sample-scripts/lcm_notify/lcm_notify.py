#!/usr/bin/env python3
"""
Subscribe to and listen for LCM Recommendation Events from Crosswork Network Controller.

API References:
  https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/
  https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/
  https://developer.cisco.com/docs/crosswork/network-controller/retrieve-an-lcm-recommendation/
  https://developer.cisco.com/docs/crosswork/network-controller/preview-lcm-msl-recommendation/
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Literal, Optional, TextIO

import requests
import urllib3
from requests.utils import getproxies_environment

# ===========================================================================
# Constants
# ===========================================================================

# --- Networking defaults ---

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
DEFAULT_AUTH_TIMEOUT = 30
DEFAULT_RECONNECT_DELAY = 3
CHUNK_SIZE = 4096

# --- Environment variable names ---

ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
ENV_HTTP_PROXY = "http_proxy"
ENV_HTTPS_PROXY = "https_proxy"

# --- RESTCONF API base paths ---

OPTIMIZATION_V3_BASE = "/crosswork/nbi/optimization/v3/restconf"
OPTIMA_V2_BASE = "/crosswork/nbi/optima/v2/restconf"

# --- YANG notification / operation identifiers ---

LCM_NOTIFICATION_V3 = (
    "(urn:com:cisco:crosswork:optimization-engine:lcm-recommendation:operations?"
    "revision=2021-05-06)lcm-recommendation-event"
)
STREAM_NAME_V2 = "lcm-recommendation-event"
LISTEN_PATH_V2 = (
    "/notif/notification-stream/"
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:"
    "lcm-recommendation-event/JSON"
)
LCM_REC_EVENT_KEY = (
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:"
    "lcm-recommendation-event"
)
LCM_REC_OUTPUT_KEY = (
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output"
)
GET_LCM_RECOMMENDATION_OP = (
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:"
    "get-lcm-recommendation"
)
GET_LCM_MSL_PREVIEW_OP = (
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:"
    "get-lcm-msl-recommendation-preview"
)

# --- Output markers ---

LOG_REQUEST_MARKER = ">>>"
LOG_RESPONSE_MARKER = "<<<"

ExchangeDirection = Literal["request", "response"]


def local_timestamp(when: datetime | None = None) -> str:
    """Format a local date/time for request and response logs."""
    moment = when or datetime.now().astimezone()
    return moment.strftime("%Y-%m-%d %H:%M:%S") + f".{moment.microsecond // 1000:03d}"


def print_exchange(
    direction: ExchangeDirection,
    headline: str,
    body: str | None = None,
    *,
    timestamp: str | None = None,
    file: TextIO = sys.stdout,
    leading_blank: bool = False,
    trailing_blank: bool = False,
) -> None:
    """Print one request or response with a single timestamp and direction marker."""
    if leading_blank:
        print(file=file)
    ts = timestamp or local_timestamp()
    marker = LOG_REQUEST_MARKER if direction == "request" else LOG_RESPONSE_MARKER
    print(f"{ts} {marker} {headline}", file=file)
    if body:
        print(body, file=file)
    if trailing_blank:
        print(file=file)


def format_exchange_line(
    direction: ExchangeDirection,
    headline: str,
    body: str | None = None,
    *,
    timestamp: str | None = None,
) -> str:
    """Format one request or response line for file output."""
    ts = timestamp or local_timestamp()
    marker = LOG_REQUEST_MARKER if direction == "request" else LOG_RESPONSE_MARKER
    lines = [f"{ts} {marker} {headline}"]
    if body:
        lines.append(body)
    return "\n".join(lines)

# --- Type aliases ---

ApiMode = Literal["auto", "v3", "legacy"]
ReconnectFactory = Callable[[], "StreamSession"]


# ===========================================================================
# Exceptions
# ===========================================================================


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


# ===========================================================================
# HTTP adapters and session factory
# ===========================================================================


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""

    def __init__(self, timeout: int = CONNECT_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(**kwargs)

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(*args, **kwargs)


class _VerboseAdapter(_TimeoutAdapter):
    """HTTPAdapter that logs request and response details to stderr."""

    def __init__(
        self,
        timeout: int = CONNECT_TIMEOUT,
        pretty: bool = False,
        suppress_rec_rpc: bool = False,
        **kwargs,
    ):
        self._pretty = pretty
        self._suppress_rec_rpc = suppress_rec_rpc
        super().__init__(timeout=timeout, **kwargs)

    def _should_log(self, request: requests.PreparedRequest) -> bool:
        if not self._suppress_rec_rpc:
            return True
        url = request.url or ""
        return (
            GET_LCM_RECOMMENDATION_OP not in url
            and GET_LCM_MSL_PREVIEW_OP not in url
        )

    def send(self, request, *args, **kwargs):
        if not self._should_log(request):
            return super().send(request, *args, **kwargs)

        request_time = datetime.now().astimezone()
        request_ts = local_timestamp(request_time)
        request_details = _format_verbose_request_details(request, self._pretty)
        print_exchange(
            "request",
            f"{request.method} {request.url}",
            request_details or None,
            timestamp=request_ts,
            file=sys.stderr,
        )

        stream = kwargs.get("stream", False)
        resp = super().send(request, *args, **kwargs)
        response_time = datetime.now().astimezone()
        response_ts = local_timestamp(response_time)

        if stream:
            print_exchange(
                "response",
                f"HTTP {resp.status_code} {resp.reason}",
                timestamp=response_ts,
                file=sys.stderr,
                leading_blank=True,
                trailing_blank=True,
            )
        else:
            content = resp.content
            response_body = format_http_log_body(content, pretty=self._pretty)
            print_exchange(
                "response",
                f"{resp.status_code} {resp.reason} ({len(content)} bytes)",
                response_body,
                timestamp=response_ts,
                file=sys.stderr,
                leading_blank=True,
                trailing_blank=True,
            )
        return resp


def _read_env_proxy(name: str) -> str | None:
    """Read a proxy URL from the environment, preferring lowercase names."""
    value = os.environ.get(name)
    if value:
        return value
    return os.environ.get(name.upper()) or None


def _configured_proxies() -> dict[str, str]:
    """Return http/https proxy URLs from http_proxy and https_proxy env vars."""
    proxies: dict[str, str] = {}
    http_proxy = _read_env_proxy(ENV_HTTP_PROXY)
    if http_proxy:
        proxies["http"] = http_proxy
    https_proxy = _read_env_proxy(ENV_HTTPS_PROXY)
    if https_proxy:
        proxies["https"] = https_proxy
    return proxies


def _apply_proxy_config(session: requests.Session) -> dict[str, str]:
    """Apply proxy settings from the environment to a requests session."""
    session.trust_env = True
    proxies = _configured_proxies()
    if not proxies:
        # Fall back to requests' broader *_proxy environment discovery.
        proxies = {
            scheme: url
            for scheme, url in getproxies_environment().items()
            if scheme in ("http", "https") and url
        }
    if proxies:
        session.proxies.update(proxies)
    return proxies


def _log_proxy_config(proxies: dict[str, str], *, base_url: str | None = None) -> None:
    """Log configured proxy servers to stderr for verbose mode."""
    parts: list[str] = []
    http_proxy = proxies.get("http")
    https_proxy = proxies.get("https")
    if http_proxy:
        parts.append(f"http_proxy={http_proxy}")
    if https_proxy:
        parts.append(f"https_proxy={https_proxy}")
    elif http_proxy and base_url and base_url.startswith("https:"):
        parts.append(f"https (via {ENV_HTTP_PROXY})={http_proxy}")
    if parts:
        print(f"Using proxy: {', '.join(parts)}", file=sys.stderr)
    else:
        print("Using proxy: none (direct connection)", file=sys.stderr)


def _create_session(
    verify_ssl: bool = True,
    timeout: int = CONNECT_TIMEOUT,
    verbose: bool = False,
    pretty: bool = False,
    suppress_rec_rpc: bool = False,
    base_url: str | None = None,
) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    proxies = _apply_proxy_config(session)
    if verbose:
        _log_proxy_config(proxies, base_url=base_url)
    if verbose:
        adapter = _VerboseAdapter(
            timeout=timeout,
            pretty=pretty,
            suppress_rec_rpc=suppress_rec_rpc,
        )
    else:
        adapter = _TimeoutAdapter(timeout=timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ===========================================================================
# HTTP response helpers
# ===========================================================================


def response_text(resp: requests.Response, limit: int = 500) -> str:
    """Decode response body to a truncated string for display purposes."""
    body = resp.content.decode("utf-8", errors="replace").strip()
    if len(body) > limit:
        body = body[:limit] + "..."
    return body or "<empty response body>"


LEGACY_V2_UNAVAILABLE_HINT = (
    "The legacy optima v2 notification API is not available on this CNC release "
    "(typical on CNC 7.2+). Use --api v3 or --api auto instead of --api legacy."
)


def _legacy_v2_unavailable_hint(resp: requests.Response) -> str | None:
    """Return guidance when the v2 streams model is missing on newer controllers."""
    if resp.status_code != 409:
        return None
    body = resp.content.decode("utf-8", errors="replace")
    if "data-missing" in body:
        return LEGACY_V2_UNAVAILABLE_HINT
    return None


def check_response(resp: requests.Response, context: str, *, hint: str | None = None) -> None:
    """Raise CrossworkAuthError if the response indicates failure."""
    if resp.ok:
        return
    body = resp.content.decode("utf-8", errors="replace").strip()
    if len(body) > 500:
        body = body[:500] + "..."
    if not body:
        body = "<empty response body>"
    reason = getattr(resp, "reason", "")
    status = f"HTTP {resp.status_code}"
    if reason:
        status = f"{status} {reason}"
    message = f"{context} returned {status}: {body}"
    if hint:
        message = f"{message}\nHint: {hint}"
    raise CrossworkAuthError(message)


def response_json(resp: requests.Response, context: str) -> dict:
    """Validate response and parse JSON body, raising on error."""
    check_response(resp, context)
    try:
        return json.loads(resp.content)
    except json.JSONDecodeError as exc:
        raise CrossworkAuthError(
            f"{context} returned invalid JSON: {response_text(resp)}"
        ) from exc


def stream_log(message: str, *, file: TextIO = sys.stderr) -> None:
    """Log a notification-stream status message without request/response markers."""
    print(message, file=file)


def _parse_stream_chunk_payload(text: str) -> Optional[object]:
    """Extract JSON payload from an SSE data chunk or raw JSON stream chunk."""
    data_parts: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_parts.append(line[5:].lstrip())

    if data_parts:
        return json.loads("\n".join(data_parts))

    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    return None


def stream_chunk_log(
    chunk: bytes,
    text: str,
    *,
    file: TextIO = sys.stderr,
    when: datetime | None = None,
) -> None:
    """Log a non-notification stream chunk (for example SSE keepalive pings)."""
    print_exchange(
        "response",
        f"chunk ({len(chunk)} bytes): {text.strip()}",
        timestamp=local_timestamp(when),
        file=file,
    )


def serialize_rest_body(data: object, *, pretty: bool) -> str:
    """Serialize a RESTCONF payload for display."""
    if pretty:
        return format_rest_body(data)
    text = json.dumps(data, separators=(",", ":"), sort_keys=False)
    if len(text) > 500:
        text = text[:500] + "..."
    return text


def format_rest_body(data: object) -> str:
    """Serialize a RESTCONF payload as indented JSON for display."""
    return json.dumps(data, indent=2, sort_keys=False)


def format_http_log_body(raw: str | bytes, *, pretty: bool) -> str:
    """Format an HTTP request/response body for verbose logging."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw

    if pretty:
        try:
            return format_rest_body(json.loads(text))
        except json.JSONDecodeError:
            pass

    if len(text) > 500:
        text = text[:500] + "..."
    return text


def _format_verbose_request_details(
    request: requests.PreparedRequest,
    pretty: bool,
) -> str:
    """Format request headers and body for a single verbose request block."""
    lines: list[str] = []
    if request.headers:
        for key, value in request.headers.items():
            if key.lower() == "authorization":
                value = value[:20] + "..." if len(value) > 20 else value
            lines.append(f"{key}: {value}")
    if request.body:
        lines.append(format_http_log_body(request.body, pretty=pretty))
    return "\n".join(lines)


# ===========================================================================
# Credentials
# ===========================================================================


def _resolve_credentials(username=None, password=None) -> tuple[str, str]:
    """Resolve username and password from args, environment, or interactive prompt."""
    username = username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")

    password = password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")

    return username, password


def load_token_from_file(path: str) -> str:
    """Read a JWT token from a file."""
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


# ===========================================================================
# Configuration and authentication
# ===========================================================================


@dataclass(frozen=True)
class ClientConfig:
    """Immutable configuration for all HTTP interactions with Crosswork."""

    base_url: str
    verify_ssl: bool = True
    timeout: int = DEFAULT_AUTH_TIMEOUT
    verbose: bool = False
    pretty: bool = False
    session: requests.Session | None = None

    def get_session(self) -> requests.Session:
        if self.session is not None:
            return self.session
        return _create_session(verify_ssl=self.verify_ssl, timeout=self.timeout)


def restconf_headers(token: str, *, accept: str = "application/yang-data+json") -> dict[str, str]:
    """Build common RESTCONF request headers with bearer authentication."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
    }


def _get_ticket(session: requests.Session, base_url: str, username: str, password: str) -> str:
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


def _get_token(session: requests.Session, base_url: str, ticket: str) -> str:
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
        raise CrossworkAuthError(f"Could not extract token from response: {resp.text[:300]}")
    return token


def authenticate(config: ClientConfig, username: str, password: str) -> str:
    """Perform full SSO authentication: obtain ticket, exchange for JWT."""
    session = config.get_session()
    ticket = _get_ticket(session, config.base_url, username, password)
    return _get_token(session, config.base_url, ticket)


# ===========================================================================
# Event parsing
# ===========================================================================


class ParserMode(str, Enum):
    """Determines how the notification stream body is framed."""

    SSE = "sse"
    JSON = "json"


class StreamingEventParser:
    """Incrementally parse JSON notifications from chunked HTTP bodies."""

    def __init__(self, mode: ParserMode):
        self.mode = mode
        self.buffer = ""
        self._json_decoder = json.JSONDecoder()

    def feed(self, chunk: bytes) -> list[dict]:
        """Append raw bytes and return any complete notifications parsed so far."""
        if not chunk:
            return []
        self.buffer += chunk.decode("utf-8", errors="replace")
        if self.mode is ParserMode.SSE:
            return self._feed_sse()
        return self._feed_json()

    def _feed_sse(self) -> list[dict]:
        events: list[dict] = []
        while "\n\n" in self.buffer:
            event_block, self.buffer = self.buffer.split("\n\n", 1)
            data_parts: list[str] = []
            for line in event_block.splitlines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_parts.append(line[5:].lstrip())
            if data_parts:
                events.append(json.loads("\n".join(data_parts)))
        return events

    def _feed_json(self) -> list[dict]:
        events: list[dict] = []
        while self.buffer:
            self.buffer = self.buffer.lstrip()
            if not self.buffer:
                break
            try:
                obj, idx = self._json_decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                break
            events.append(obj)
            self.buffer = self.buffer[idx:]
        return events


def extract_lcm_recommendation_event(notification: dict) -> Optional[dict]:
    """Return the lcm-recommendation-event payload from a RESTCONF notification."""
    restconf = notification.get("ietf-restconf:notification", notification)
    if not isinstance(restconf, dict):
        return None

    event = restconf.get(LCM_REC_EVENT_KEY)
    if isinstance(event, dict):
        return event

    for key, value in restconf.items():
        if key.endswith("lcm-recommendation-event") and isinstance(value, dict):
            return value
    return None


def _unwrap_lcm_output(response: dict) -> dict:
    """Extract the LCM output payload from a RESTCONF RPC response."""
    output = response.get(LCM_REC_OUTPUT_KEY)
    if isinstance(output, dict):
        return output
    fallback = response.get("output")
    return fallback if isinstance(fallback, dict) else {}


def format_notification(
    notification: dict,
    *,
    recommendation_details: Optional[dict] = None,
) -> str:
    """Wrap a notification in a timestamped envelope and serialize to compact JSON."""
    envelope: dict = {
        "received-at": local_timestamp(),
        "notification": notification,
    }
    if recommendation_details is not None:
        envelope["recommendation-details"] = recommendation_details
    return json.dumps(envelope, separators=(",", ":"), sort_keys=False)


def emit_notification_event(
    notification: dict,
    *,
    recommendation_details: Optional[dict] = None,
    pretty: bool = False,
    file: TextIO = sys.stdout,
) -> None:
    """Print a notification and optional recommendation RPCs once in REST API style."""
    received_at = local_timestamp()
    print_exchange(
        "response",
        "notification",
        serialize_rest_body(notification, pretty=pretty),
        timestamp=received_at,
        file=file,
    )

    if recommendation_details is None:
        return

    recommendation = recommendation_details.get("get-lcm-recommendation")
    if recommendation is not None:
        _emit_rpc_exchange(
            recommendation,
            label="get-lcm-recommendation",
            pretty=pretty,
            file=file,
        )

    for preview in recommendation_details.get("get-lcm-msl-recommendation-preview", []):
        lcm_int = preview.get("lcm-int", {})
        node = lcm_int.get("node", "?")
        interface = lcm_int.get("interface", "?")
        label = f"get-lcm-msl-recommendation-preview ({node}/{interface})"
        _emit_rpc_exchange(preview, label=label, pretty=pretty, file=file)


def _emit_rpc_exchange(
    call: dict,
    *,
    label: str,
    pretty: bool,
    file: TextIO,
) -> None:
    """Print one RPC request/response pair."""
    request = call.get("request")
    response = call.get("response", call)
    status = call.get("status")

    if request is not None:
        print_exchange(
            "request",
            f"{request['method']} {request['url']}",
            serialize_rest_body(request["body"], pretty=pretty),
            timestamp=call.get("requested-at"),
            file=file,
        )

    if pretty:
        headline = label
    elif status is not None:
        headline = f"{status['code']} {status['reason']} ({status['size']} bytes)"
    else:
        headline = label

    print_exchange(
        "response",
        headline,
        serialize_rest_body(response, pretty=pretty),
        timestamp=call.get("responded-at"),
        file=file,
        leading_blank=request is not None,
        trailing_blank=True,
    )


# ===========================================================================
# Notification stream management
# ===========================================================================


@dataclass(frozen=True)
class StreamSession:
    """A resolved URL + headers ready to open a streaming GET request."""

    url: str
    headers: dict[str, str]


class NotificationStreamClient:
    """Create and manage LCM recommendation notification streams."""

    def __init__(self, config: ClientConfig, token: str):
        self.config = config
        self.token = token

    # --- v3 (optimization) API ---

    def create_v3_stream(self) -> str:
        """Create a new v3 notification stream and return its identifier."""
        url = (
            f"{self.config.base_url}{OPTIMIZATION_V3_BASE}/operations/"
            "sal-remote:create-notification-stream"
        )
        headers = {
            **restconf_headers(self.token),
            "Content-Type": "application/yang-data+json",
        }
        payload = {
            "input": {
                "notifications": [LCM_NOTIFICATION_V3],
                "notification-output-type": "JSON",
            }
        }
        resp = self.config.get_session().post(url, headers=headers, json=payload)
        data = response_json(resp, "create_notification_stream_v3")
        output = data.get("sal-remote:output", data.get("output", {}))
        stream_id = output.get("notification-stream-identifier", "")
        if not stream_id:
            raise CrossworkAuthError(
                "create_notification_stream_v3 did not return "
                "notification-stream-identifier: "
                f"{response_text(resp, limit=1000)}"
            )
        return stream_id

    def v3_listen_session(self, stream_id: str) -> StreamSession:
        """Return a StreamSession for listening on a v3 notification stream."""
        url = f"{self.config.base_url}{OPTIMIZATION_V3_BASE}/streams/json/{stream_id}"
        return StreamSession(
            url=url,
            headers=restconf_headers(self.token, accept="text/event-stream"),
        )

    # --- v2 (optima legacy) API ---

    def _setup_v2_stream(self) -> None:
        """Verify and subscribe to the legacy v2 notification stream."""
        streams_url = (
            f"{self.config.base_url}{OPTIMA_V2_BASE}/data/"
            "ietf-restconf-monitoring:restconf-state/streams"
        )
        resp = self.config.get_session().get(
            streams_url,
            headers=restconf_headers(self.token),
        )
        check_response(
            resp,
            "enable_notification_stream_v2",
            hint=_legacy_v2_unavailable_hint(resp),
        )

        subscribe_url = (
            f"{self.config.base_url}{OPTIMA_V2_BASE}/data/"
            "ietf-restconf-monitoring:restconf-state/"
            f"streams/stream={STREAM_NAME_V2}/access=JSON/location"
        )
        resp = self.config.get_session().get(
            subscribe_url,
            headers=restconf_headers(self.token),
        )
        check_response(
            resp,
            "subscribe_notification_stream_v2",
            hint=_legacy_v2_unavailable_hint(resp),
        )

    def v2_listen_session(self) -> StreamSession:
        """Set up and return a StreamSession for the legacy v2 stream."""
        self._setup_v2_stream()
        return StreamSession(
            url=f"{self.config.base_url}{OPTIMA_V2_BASE}{LISTEN_PATH_V2}",
            headers=restconf_headers(self.token),
        )


# ===========================================================================
# Recommendation retrieval
# ===========================================================================


class RecommendationClient:
    """Fetch LCM recommendation details via optimization v3 RESTCONF operations."""

    def __init__(self, config: ClientConfig, token: str):
        self.config = config
        self.token = token

    def _operation_url(self, operation: str) -> str:
        return f"{self.config.base_url}{OPTIMIZATION_V3_BASE}/operations/{operation}"

    def _post_operation(self, operation: str, payload: dict, context: str) -> dict:
        """Execute a RESTCONF operation (RPC) via POST."""
        url = self._operation_url(operation)
        headers = {
            **restconf_headers(self.token),
            "Content-Type": "application/yang-data+json",
            "Cache-Control": "no-cache",
        }
        requested_at = local_timestamp()
        resp = self.config.get_session().post(url, headers=headers, json=payload)
        responded_at = local_timestamp()
        return {
            "request": {"method": "POST", "url": url, "body": payload},
            "response": response_json(resp, context),
            "status": {
                "code": resp.status_code,
                "reason": resp.reason or "",
                "size": len(resp.content),
            },
            "requested-at": requested_at,
            "responded-at": responded_at,
        }

    def get_recommendation(self, domain_id: str) -> dict:
        """Retrieve the LCM recommendation for a given domain."""
        return self._post_operation(
            GET_LCM_RECOMMENDATION_OP,
            {"input": {"domain-id": domain_id}},
            "get_lcm_recommendation",
        )

    def get_msl_preview(
        self,
        domain_id: str,
        recommendation_id: str,
        node: str,
        interface: str,
    ) -> dict:
        """Retrieve a MSL preview for a specific interface in a recommendation."""
        return self._post_operation(
            GET_LCM_MSL_PREVIEW_OP,
            {
                "input": {
                    "domain-id": domain_id,
                    "recommendation-id": recommendation_id,
                    "lcm-int": {"node": node, "interface": interface},
                }
            },
            "get_lcm_msl_recommendation_preview",
        )

    def fetch_details_for_event(self, event: dict) -> dict:
        """Fetch full recommendation + MSL previews for an LCM event."""
        domain_id = str(event.get("domain-id", ""))
        if not domain_id:
            raise CrossworkAuthError("lcm-recommendation-event missing domain-id")

        recommendation_call = self.get_recommendation(domain_id)
        output = _unwrap_lcm_output(recommendation_call["response"])
        recommendation_id = str(
            output.get("recommendation-id", event.get("recommendation-id", ""))
        )

        previews: list[dict] = []
        for solution in output.get("solutions", []):
            node = solution.get("node")
            interface = solution.get("interface")
            if not node or not interface:
                continue
            preview_call = self.get_msl_preview(
                domain_id,
                recommendation_id,
                str(node),
                str(interface),
            )
            previews.append(
                {
                    "lcm-int": {"node": node, "interface": interface},
                    "request": preview_call["request"],
                    "response": preview_call["response"],
                    "status": preview_call["status"],
                    "requested-at": preview_call["requested-at"],
                    "responded-at": preview_call["responded-at"],
                }
            )

        return {
            "get-lcm-recommendation": recommendation_call,
            "get-lcm-msl-recommendation-preview": previews,
        }


# ===========================================================================
# Notification listener
# ===========================================================================


@dataclass
class ListenOptions:
    """Options controlling how notifications are emitted."""

    pretty: bool = False
    verbose: bool = False
    output_file: Optional[str] = None
    max_events: Optional[int] = None
    get_rec: bool = False

    @property
    def show_events(self) -> bool:
        """Whether notifications and recommendation RPCs are printed structurally."""
        return self.pretty or self.verbose

    @property
    def event_output(self) -> TextIO:
        """Destination for structured notification/RPC output."""
        return sys.stdout if self.pretty else sys.stderr


class NotificationListener:
    """Listen for notifications on a RESTCONF stream until stopped."""

    def __init__(
        self,
        config: ClientConfig,
        recommendation_client: Optional[RecommendationClient] = None,
    ):
        self.config = config
        self.recommendation_client = recommendation_client
        self._stop_requested = False

    def request_stop(self, signum: int, _frame=None) -> None:
        """Signal handler to gracefully stop listening."""
        self._stop_requested = True
        print(f"\nReceived signal {signum}, stopping...", file=sys.stderr)

    def listen(
        self,
        session: StreamSession,
        *,
        parser_mode: ParserMode,
        options: ListenOptions,
        on_reconnect: Optional[ReconnectFactory] = None,
    ) -> int:
        """Main listen loop: stream events, reconnect on failure, return event count."""
        print("Press Ctrl+C to stop.", file=sys.stderr)

        event_count = 0
        outfile: Optional[TextIO] = None
        if options.output_file:
            outfile = open(options.output_file, "a", encoding="utf-8")

        current_session = session
        try:
            while not self._stop_requested:
                if self._reached_max(event_count, options.max_events):
                    break

                event_count += self._listen_once(
                    current_session,
                    parser_mode=parser_mode,
                    options=options,
                    outfile=outfile,
                    event_count=event_count,
                )

                if self._should_exit(event_count, options.max_events):
                    break
                if on_reconnect is None:
                    break

                current_session = self._reconnect(on_reconnect)
        finally:
            if outfile:
                outfile.close()

        return event_count

    def _listen_once(
        self,
        session: StreamSession,
        *,
        parser_mode: ParserMode,
        options: ListenOptions,
        outfile: Optional[TextIO],
        event_count: int,
    ) -> int:
        """Open one streaming connection and consume events until it closes."""
        stream_log(f"Listening on {session.url}")
        new_events = 0

        try:
            with self.config.get_session().get(
                session.url,
                headers=session.headers,
                stream=True,
                timeout=(DEFAULT_AUTH_TIMEOUT, None),
            ) as resp:
                check_response(resp, "listen_notification_stream")
                parser = StreamingEventParser(parser_mode)

                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if self._stop_requested or not chunk:
                        break

                    if self.config.verbose:
                        text = chunk.decode("utf-8", errors="replace")
                        try:
                            payload = _parse_stream_chunk_payload(text)
                        except json.JSONDecodeError:
                            payload = None
                        if payload is None:
                            stream_chunk_log(
                                chunk,
                                text,
                                when=datetime.now().astimezone(),
                            )

                    for notification in parser.feed(chunk):
                        if self._stop_requested:
                            break

                        new_events += 1
                        self._emit(notification, options=options, outfile=outfile)

                        if self._reached_max(event_count + new_events, options.max_events):
                            stream_log(
                                f"Reached --max-events limit ({options.max_events})."
                            )
                            return new_events

        except (CrossworkAuthError, requests.RequestException) as exc:
            if not self._stop_requested:
                stream_log(f"error: {exc}")
        else:
            if not self._should_exit(event_count + new_events, options.max_events):
                stream_log("closed by server.")

        return new_events

    def _reconnect(self, factory: ReconnectFactory) -> StreamSession:
        """Wait briefly and then reconnect via the factory."""
        stream_log(f"reconnecting in {DEFAULT_RECONNECT_DELAY} second(s)...")
        time.sleep(DEFAULT_RECONNECT_DELAY)
        return factory()

    def _emit(
        self,
        notification: dict,
        *,
        options: ListenOptions,
        outfile: Optional[TextIO],
    ) -> None:
        """Format and output a single notification."""
        recommendation_details = None
        if options.get_rec and self.recommendation_client is not None:
            event = extract_lcm_recommendation_event(notification)
            if event is not None:
                try:
                    recommendation_details = (
                        self.recommendation_client.fetch_details_for_event(event)
                    )
                except (CrossworkAuthError, requests.RequestException) as exc:
                    print(
                        f"Failed to fetch recommendation details: {exc}",
                        file=sys.stderr,
                    )

        line = format_notification(
            notification,
            recommendation_details=recommendation_details,
        )
        if options.show_events:
            emit_notification_event(
                notification,
                recommendation_details=recommendation_details,
                pretty=options.pretty,
                file=options.event_output,
            )
            if options.pretty:
                print()
        else:
            received_at = local_timestamp()
            print_exchange(
                "response",
                "notification",
                line,
                timestamp=received_at,
            )
        if outfile:
            received_at = local_timestamp()
            outfile.write(
                format_exchange_line("response", "notification", line, timestamp=received_at)
                + "\n"
            )
            outfile.flush()

    @staticmethod
    def _reached_max(event_count: int, max_events: Optional[int]) -> bool:
        return max_events is not None and event_count >= max_events

    def _should_exit(self, event_count: int, max_events: Optional[int]) -> bool:
        return self._stop_requested or self._reached_max(event_count, max_events)


# ===========================================================================
# CLI
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Subscribe to and listen for LCM Recommendation Events from "
            "Crosswork Network Controller"
        ),
    )

    # Connection
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument(
        "--port",
        type=int,
        default=BASE_PORT,
        help=f"Crosswork HTTPS port (default: {BASE_PORT})",
    )
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_AUTH_TIMEOUT,
        help=f"HTTP timeout in seconds for auth/setup requests (default: {DEFAULT_AUTH_TIMEOUT})",
    )

    # Authentication
    parser.add_argument("--username", "-u", default=None,
                        help=f"Username (or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")

    # API mode
    parser.add_argument(
        "--api",
        choices=["auto", "v3", "legacy"],
        default="auto",
        help="Notification API variant (default: auto, prefers optimization v3)",
    )
    parser.add_argument(
        "--stream-id",
        help="Existing optimization v3 stream identifier (skips stream creation)",
    )

    # Output
    parser.add_argument(
        "--pretty",
        action="store_true",
        help=(
            "Pretty-print notifications and recommendation RPCs to stdout "
            "(indented JSON body after a single <<< line)"
        ),
    )
    parser.add_argument(
        "--output", "-o",
        help="Append received notifications to this file (one JSON object per line)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Stop after receiving this many events (default: listen until interrupted)",
    )

    # Behaviour
    parser.add_argument(
        "--get-rec",
        action="store_true",
        help=(
            "On lcm-recommendation-event, fetch recommendation details via "
            "optimization v3 get-lcm-recommendation and "
            "get-lcm-msl-recommendation-preview"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help=(
            "Print setup API traffic to stderr and show notifications/RPCs once "
            "in HTTP trace style (or pretty style when combined with --pretty)"
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# CLI helpers — keep main() focused on orchestration
# ---------------------------------------------------------------------------


def _build_config(args: argparse.Namespace) -> ClientConfig:
    """Construct a ClientConfig from parsed CLI arguments."""
    verify_ssl = not args.insecure
    if args.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

    base_url = f"https://{args.ip}:{args.port}"
    session = _create_session(
        verify_ssl=verify_ssl,
        timeout=args.timeout,
        verbose=args.verbose,
        pretty=args.pretty,
        suppress_rec_rpc=args.get_rec and (args.pretty or args.verbose),
        base_url=base_url,
    )
    return ClientConfig(
        base_url=base_url,
        verify_ssl=verify_ssl,
        timeout=args.timeout,
        verbose=args.verbose,
        pretty=args.pretty,
        session=session,
    )


def _obtain_token(args: argparse.Namespace, config: ClientConfig) -> str:
    """Authenticate or load a JWT based on CLI arguments."""
    if args.jwt:
        token = load_token_from_file(args.jwt)
        print(f"Using JWT from {args.jwt}", file=sys.stderr)
        return token

    print(f"Authenticating to {args.ip}...", file=sys.stderr)
    username, password = _resolve_credentials(args.username, args.password)
    return authenticate(config, username, password)


def _resolve_api_mode(
    requested: ApiMode,
    stream_id: Optional[str],
    stream_client: NotificationStreamClient,
) -> tuple[ApiMode, Optional[str]]:
    """Determine which API to use, auto-detecting v3 support when needed."""
    if stream_id:
        return "v3", stream_id
    if requested == "legacy":
        return "legacy", None

    # For both "auto" and explicit "v3", create a new stream.
    try:
        print("Creating optimization v3 notification stream...", file=sys.stderr)
        new_stream_id = stream_client.create_v3_stream()
        print(f"Stream identifier: {new_stream_id}", file=sys.stderr)
        return "v3", new_stream_id
    except CrossworkAuthError as exc:
        if requested == "v3":
            raise
        print(
            f"Optimization v3 stream setup failed ({exc}); "
            "falling back to legacy optima v2 API.",
            file=sys.stderr,
        )
        return "legacy", None


def _listen_v3(
    stream_client: NotificationStreamClient,
    listener: NotificationListener,
    stream_id: str,
    options: ListenOptions,
) -> int:
    """Start listening on the v3 notification stream with auto-reconnect."""
    current_stream_id = stream_id

    def on_reconnect() -> StreamSession:
        nonlocal current_stream_id
        print("Creating new optimization v3 notification stream...", file=sys.stderr)
        current_stream_id = stream_client.create_v3_stream()
        print(f"Stream identifier: {current_stream_id}", file=sys.stderr)
        return stream_client.v3_listen_session(current_stream_id)

    return listener.listen(
        stream_client.v3_listen_session(current_stream_id),
        parser_mode=ParserMode.SSE,
        options=options,
        on_reconnect=on_reconnect,
    )


def _listen_legacy(
    stream_client: NotificationStreamClient,
    listener: NotificationListener,
    options: ListenOptions,
) -> int:
    """Start listening on the legacy v2 notification stream with auto-reconnect."""
    print("Enabling legacy optima v2 notification stream...", file=sys.stderr)

    def on_reconnect() -> StreamSession:
        return stream_client.v2_listen_session()

    session = on_reconnect()
    print("Subscribed to lcm-recommendation-event.", file=sys.stderr)
    return listener.listen(
        session,
        parser_mode=ParserMode.JSON,
        options=options,
        on_reconnect=on_reconnect,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Parse arguments, authenticate, set up stream, and listen for events."""
    args = build_parser().parse_args(argv)
    config = _build_config(args)
    options = ListenOptions(
        pretty=args.pretty,
        verbose=args.verbose,
        output_file=args.output,
        max_events=args.max_events,
        get_rec=args.get_rec,
    )

    try:
        token = _obtain_token(args, config)

        recommendation_client = (
            RecommendationClient(config, token) if args.get_rec else None
        )
        listener = NotificationListener(config, recommendation_client)
        signal.signal(signal.SIGINT, listener.request_stop)
        signal.signal(signal.SIGTERM, listener.request_stop)

        if args.get_rec:
            print(
                "Recommendation retrieval enabled via optimization v3 "
                "(get-lcm-recommendation + get-lcm-msl-recommendation-preview).",
                file=sys.stderr,
            )

        stream_client = NotificationStreamClient(config, token)
        api_mode, stream_id = _resolve_api_mode(args.api, args.stream_id, stream_client)

        if api_mode == "v3":
            count = _listen_v3(stream_client, listener, stream_id, options)
        else:
            count = _listen_legacy(stream_client, listener, options)

        print(f"Received {count} event(s).", file=sys.stderr)
        return 0

    except (CrossworkAuthError, requests.RequestException, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
