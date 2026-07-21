#!/usr/bin/env python3
"""
Subscribe to and listen for LCM Recommendation Events from Crosswork Network Controller.

API References:
  https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/
  https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal, Optional, TextIO

import requests
import urllib3

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

    def send(self, request, *args, **kwargs):
        print(f">>> {request.method} {request.url}", file=sys.stderr)
        if request.headers:
            for key, value in request.headers.items():
                if key.lower() == "authorization":
                    value = value[:20] + "..." if len(value) > 20 else value
                print(f">>>   {key}: {value}", file=sys.stderr)
        if request.body:
            body = request.body if isinstance(request.body, str) else repr(request.body)
            if len(body) > 500:
                body = body[:500] + "..."
            print(f">>>   Body: {body}", file=sys.stderr)

        print(file=sys.stderr)

        stream = kwargs.get("stream", False)
        resp = super().send(request, *args, **kwargs)

        if stream:
            stream_log(f"HTTP {resp.status_code} {resp.reason}")
            print(file=sys.stderr)
        else:
            content = resp.content
            print(
                f"<<< {resp.status_code} {resp.reason} ({len(content)} bytes)",
                file=sys.stderr,
            )
            body = content.decode("utf-8", errors="replace")
            if len(body) > 500:
                body = body[:500] + "..."
            print(f"<<<   Body: {body}", file=sys.stderr)
            print(file=sys.stderr)
        return resp


def _create_session(
    verify_ssl: bool = True,
    timeout: int = CONNECT_TIMEOUT,
    verbose: bool = False,
) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter_cls = _VerboseAdapter if verbose else _TimeoutAdapter
    adapter = adapter_cls(timeout=timeout)
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


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError if the response indicates failure."""
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
    """Log a notification-stream status message."""
    print(f"<<<< {message}", file=file)


def stream_chunk_log(chunk: bytes, text: str, *, file: TextIO = sys.stderr) -> None:
    """Log a raw notification-stream HTTP body chunk."""
    print(f"<<<< chunk ({len(chunk)} bytes): {text}", file=file)


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
    pretty: bool,
    recommendation_details: Optional[dict] = None,
) -> str:
    """Wrap a notification in a timestamped envelope and serialize to JSON."""
    envelope: dict = {
        "received-at": datetime.now(timezone.utc).isoformat(),
        "notification": notification,
    }
    if recommendation_details is not None:
        envelope["recommendation-details"] = recommendation_details
    if pretty:
        return json.dumps(envelope, indent=2, sort_keys=False)
    return json.dumps(envelope, separators=(",", ":"), sort_keys=False)


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
        check_response(resp, "enable_notification_stream_v2")

        subscribe_url = (
            f"{self.config.base_url}{OPTIMA_V2_BASE}/data/"
            "ietf-restconf-monitoring:restconf-state/"
            f"streams/stream={STREAM_NAME_V2}/access=JSON/location"
        )
        resp = self.config.get_session().get(
            subscribe_url,
            headers=restconf_headers(self.token),
        )
        check_response(resp, "subscribe_notification_stream_v2")

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
    """Fetch LCM recommendation details via optima v2 RESTCONF operations."""

    def __init__(self, config: ClientConfig, token: str):
        self.config = config
        self.token = token

    def _post_operation(self, operation: str, payload: dict, context: str) -> dict:
        """Execute a RESTCONF operation (RPC) via POST."""
        url = f"{self.config.base_url}{OPTIMA_V2_BASE}/operations/{operation}"
        headers = {
            **restconf_headers(self.token),
            "Content-Type": "application/yang-data+json",
            "Cache-Control": "no-cache",
        }
        resp = self.config.get_session().post(url, headers=headers, json=payload)
        return response_json(resp, context)

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

        recommendation_response = self.get_recommendation(domain_id)
        output = _unwrap_lcm_output(recommendation_response)
        recommendation_id = str(
            output.get("recommendation-id", event.get("recommendation-id", ""))
        )

        previews: list[dict] = []
        for solution in output.get("solutions", []):
            node = solution.get("node")
            interface = solution.get("interface")
            if not node or not interface:
                continue
            preview_response = self.get_msl_preview(
                domain_id,
                recommendation_id,
                str(node),
                str(interface),
            )
            previews.append(
                {
                    "lcm-int": {"node": node, "interface": interface},
                    "response": preview_response,
                }
            )

        return {
            "get-lcm-recommendation": recommendation_response,
            "get-lcm-msl-recommendation-preview": previews,
        }


# ===========================================================================
# Notification listener
# ===========================================================================


@dataclass
class ListenOptions:
    """Options controlling how notifications are emitted."""

    pretty: bool = False
    output_file: Optional[str] = None
    max_events: Optional[int] = None
    get_rec: bool = False


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
                        if len(text) > 500:
                            text = text[:500] + "..."
                        stream_chunk_log(chunk, text)

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
            pretty=options.pretty,
            recommendation_details=recommendation_details,
        )
        output = f"<<<< {line}"
        print(output)
        if outfile:
            outfile.write(output + "\n")
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
        help="Pretty-print each notification as indented JSON",
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
            "get-lcm-recommendation and get-lcm-msl-recommendation-preview"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print API requests and responses to stderr",
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

    session = _create_session(
        verify_ssl=verify_ssl,
        timeout=args.timeout,
        verbose=args.verbose,
    )
    return ClientConfig(
        base_url=f"https://{args.ip}:{args.port}",
        verify_ssl=verify_ssl,
        timeout=args.timeout,
        verbose=args.verbose,
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
                "Recommendation retrieval enabled "
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
