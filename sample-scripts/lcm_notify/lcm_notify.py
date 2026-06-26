#!/usr/bin/env python3
"""
Subscribe to and listen for LCM Recommendation Events from Crosswork Network Controller.

API References:
  https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/
  https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal, Optional, TextIO

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 30603
DEFAULT_AUTH_TIMEOUT = 30
DEFAULT_RECONNECT_DELAY = 3
VERIFY_SSL = False
CHUNK_SIZE = 4096

OPTIMIZATION_V3_BASE = "/crosswork/nbi/optimization/v3/restconf"
OPTIMA_V2_BASE = "/crosswork/nbi/optima/v2/restconf"
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

ApiMode = Literal["auto", "v3", "legacy"]
ReconnectFactory = Callable[[], "StreamSession"]

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def response_text(resp: requests.Response, limit: int = 500) -> str:
    body = resp.content.decode("utf-8", errors="replace").strip()
    if len(body) > limit:
        body = body[:limit] + "..."
    return body or "<empty response body>"


def check_response(resp: requests.Response, context: str) -> None:
    if resp.ok:
        return
    reason = getattr(resp, "reason", "")
    status = f"HTTP {resp.status_code}"
    if reason:
        status = f"{status} {reason}"
    raise CrossworkAuthError(f"{context} returned {status}: {response_text(resp)}")


def response_json(resp: requests.Response, context: str) -> dict:
    check_response(resp, context)
    try:
        return json.loads(resp.content)
    except json.JSONDecodeError as exc:
        raise CrossworkAuthError(
            f"{context} returned invalid JSON: {response_text(resp)}"
        ) from exc


# ---------------------------------------------------------------------------
# Configuration and authentication
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    verify_ssl: bool = VERIFY_SSL
    timeout: int = DEFAULT_AUTH_TIMEOUT


def restconf_headers(token: str, *, accept: str = "application/yang-data+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
    }


def get_ticket(config: ClientConfig, username: str, password: str) -> str:
    url = f"{config.base_url}/crosswork/sso/v1/tickets"
    resp = requests.post(
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        verify=config.verify_ssl,
        timeout=config.timeout,
    )
    check_response(resp, "get_ticket")

    location = resp.headers.get("Location", "")
    if location:
        ticket = location.rstrip("/").split("/")[-1]
    else:
        try:
            ticket = json.loads(resp.content).get("ticket", "")
        except (ValueError, json.JSONDecodeError):
            ticket = response_text(resp, limit=10000)

    if not ticket:
        raise CrossworkAuthError(
            f"Could not extract ticket from response: {response_text(resp, limit=300)}"
        )
    return ticket


def get_token(config: ClientConfig, ticket: str) -> str:
    url = f"{config.base_url}/crosswork/sso/v2/tickets/jwt"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"tgt": ticket, "service": f"{config.base_url}/app-dashboard"},
        verify=config.verify_ssl,
        timeout=config.timeout,
    )
    check_response(resp, "get_token")

    try:
        token = json.loads(resp.content).get("token", "")
    except (ValueError, json.JSONDecodeError):
        token = response_text(resp, limit=10000)

    if not token:
        raise CrossworkAuthError(
            f"Could not extract token from response: {response_text(resp, limit=300)}"
        )
    return token


def authenticate(config: ClientConfig, username: str, password: str) -> str:
    return get_token(config, get_ticket(config, username, password))


def load_token_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class ParserMode(str, Enum):
    SSE = "sse"
    JSON = "json"


class StreamingEventParser:
    """Incrementally parse JSON notifications from chunked HTTP bodies."""

    def __init__(self, mode: ParserMode):
        self.mode = mode
        self.buffer = ""
        self._json_decoder = json.JSONDecoder()

    def feed(self, chunk: bytes) -> list[dict]:
        if not chunk:
            return []
        self.buffer += chunk.decode("utf-8", errors="replace")
        if self.mode is ParserMode.SSE:
            return self._feed_sse()
        return self._feed_json()

    def _feed_sse(self) -> list[dict]:
        events = []
        while "\n\n" in self.buffer:
            event_block, self.buffer = self.buffer.split("\n\n", 1)
            data_parts = []
            for line in event_block.splitlines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_parts.append(line[5:].lstrip())
            if data_parts:
                events.append(json.loads("\n".join(data_parts)))
        return events

    def _feed_json(self) -> list[dict]:
        events = []
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


def format_notification(notification: dict, *, pretty: bool) -> str:
    envelope = {
        "received-at": datetime.now(timezone.utc).isoformat(),
        "notification": notification,
    }
    if pretty:
        return json.dumps(envelope, indent=2, sort_keys=False)
    return json.dumps(envelope, separators=(",", ":"), sort_keys=False)


# ---------------------------------------------------------------------------
# Notification stream setup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamSession:
    url: str
    headers: dict[str, str]


class NotificationStreamClient:
    """Create and manage LCM recommendation notification streams."""

    def __init__(self, config: ClientConfig, token: str):
        self.config = config
        self.token = token

    def create_v3_stream(self) -> str:
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
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )
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
        url = f"{self.config.base_url}{OPTIMIZATION_V3_BASE}/streams/json/{stream_id}"
        return StreamSession(
            url=url,
            headers=restconf_headers(self.token, accept="text/event-stream"),
        )

    def setup_v2_stream(self) -> None:
        streams_url = (
            f"{self.config.base_url}{OPTIMA_V2_BASE}/data/"
            "ietf-restconf-monitoring:restconf-state/streams"
        )
        resp = requests.get(
            streams_url,
            headers=restconf_headers(self.token),
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )
        check_response(resp, "enable_notification_stream_v2")

        subscribe_url = (
            f"{self.config.base_url}{OPTIMA_V2_BASE}/data/"
            "ietf-restconf-monitoring:restconf-state/"
            f"streams/stream={STREAM_NAME_V2}/access=JSON/location"
        )
        resp = requests.get(
            subscribe_url,
            headers=restconf_headers(self.token),
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )
        check_response(resp, "subscribe_notification_stream_v2")

    def v2_listen_session(self) -> StreamSession:
        self.setup_v2_stream()
        return StreamSession(
            url=f"{self.config.base_url}{OPTIMA_V2_BASE}{LISTEN_PATH_V2}",
            headers=restconf_headers(self.token),
        )


# ---------------------------------------------------------------------------
# Notification listener
# ---------------------------------------------------------------------------


@dataclass
class ListenOptions:
    pretty: bool = False
    output_file: Optional[str] = None
    max_events: Optional[int] = None


class NotificationListener:
    """Listen for notifications on a RESTCONF stream until stopped."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self._stop_requested = False

    def request_stop(self, signum: int, _frame=None) -> None:
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
        print("Press Ctrl+C to stop.", file=sys.stderr)

        event_count = 0
        outfile: Optional[TextIO] = None
        if options.output_file:
            outfile = open(options.output_file, "a", encoding="utf-8")

        current_session = session
        try:
            while not self._stop_requested:
                if self._reached_max_events(event_count, options.max_events):
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

                current_session = self._wait_and_reconnect(on_reconnect)
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
        print(f"Listening on {session.url}", file=sys.stderr)
        new_events = 0

        try:
            with requests.get(
                session.url,
                headers=session.headers,
                stream=True,
                verify=self.config.verify_ssl,
                timeout=(DEFAULT_AUTH_TIMEOUT, None),
            ) as resp:
                check_response(resp, "listen_notification_stream")
                parser = StreamingEventParser(parser_mode)

                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if self._stop_requested or not chunk:
                        break

                    for notification in parser.feed(chunk):
                        if self._stop_requested:
                            break

                        new_events += 1
                        self._emit(notification, options=options, outfile=outfile)

                        if self._reached_max_events(event_count + new_events, options.max_events):
                            self._log_max_events(options.max_events)
                            return new_events

        except (CrossworkAuthError, requests.RequestException) as exc:
            if not self._stop_requested:
                print(f"Stream error: {exc}", file=sys.stderr)
        else:
            if not self._should_exit(event_count + new_events, options.max_events):
                print("Stream closed by server.", file=sys.stderr)

        return new_events

    def _wait_and_reconnect(self, on_reconnect: ReconnectFactory) -> StreamSession:
        print(
            f"Reconnecting in {DEFAULT_RECONNECT_DELAY} second(s)...",
            file=sys.stderr,
        )
        time.sleep(DEFAULT_RECONNECT_DELAY)
        return on_reconnect()

    def _emit(
        self,
        notification: dict,
        *,
        options: ListenOptions,
        outfile: Optional[TextIO],
    ) -> None:
        line = format_notification(notification, pretty=options.pretty)
        print(line)
        if outfile:
            outfile.write(line + "\n")
            outfile.flush()

    @staticmethod
    def _reached_max_events(event_count: int, max_events: Optional[int]) -> bool:
        return max_events is not None and event_count >= max_events

    def _should_exit(self, event_count: int, max_events: Optional[int]) -> bool:
        return self._stop_requested or self._reached_max_events(event_count, max_events)

    @staticmethod
    def _log_max_events(max_events: Optional[int]) -> None:
        print(f"Reached --max-events limit ({max_events}).", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Subscribe to and listen for LCM Recommendation Events from "
            "Crosswork Network Controller"
        ),
    )
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Crosswork HTTPS port (default: {DEFAULT_PORT})",
    )
    parser.add_argument("--username", "-u", default="admin", help="Username (default: admin)")
    parser.add_argument("--password", "-p", default="admin", help="Password (default: admin)")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_AUTH_TIMEOUT,
        help=f"HTTP timeout in seconds for auth/setup requests (default: {DEFAULT_AUTH_TIMEOUT})",
    )
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
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print each notification as indented JSON",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Append received notifications to this file (one JSON object per line)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Stop after receiving this many events (default: listen until interrupted)",
    )
    return parser


def resolve_api_mode(
    requested: ApiMode,
    stream_id: Optional[str],
    stream_client: NotificationStreamClient,
) -> tuple[ApiMode, Optional[str]]:
    if stream_id:
        return "v3", stream_id
    if requested != "auto":
        return requested, None

    try:
        print("Creating optimization v3 notification stream...", file=sys.stderr)
        new_stream_id = stream_client.create_v3_stream()
        print(f"Stream identifier: {new_stream_id}", file=sys.stderr)
        return "v3", new_stream_id
    except CrossworkAuthError as exc:
        print(
            f"Optimization v3 stream setup failed ({exc}); "
            "falling back to legacy optima v2 API.",
            file=sys.stderr,
        )
        return "legacy", None


def listen_v3(
    stream_client: NotificationStreamClient,
    listener: NotificationListener,
    stream_id: str,
    options: ListenOptions,
) -> int:
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


def listen_legacy(
    stream_client: NotificationStreamClient,
    listener: NotificationListener,
    options: ListenOptions,
) -> int:
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


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = ClientConfig(
        base_url=f"https://{args.ip}:{args.port}",
        verify_ssl=VERIFY_SSL,
        timeout=args.timeout,
    )
    options = ListenOptions(
        pretty=args.pretty,
        output_file=args.output,
        max_events=args.max_events,
    )

    listener = NotificationListener(config)
    signal.signal(signal.SIGINT, listener.request_stop)
    signal.signal(signal.SIGTERM, listener.request_stop)

    try:
        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}", file=sys.stderr)
        else:
            print(f"Authenticating to {args.ip}...", file=sys.stderr)
            token = authenticate(config, args.username, args.password)

        stream_client = NotificationStreamClient(config, token)
        api_mode, stream_id = resolve_api_mode(args.api, args.stream_id, stream_client)

        if api_mode == "v3":
            if not stream_id:
                print("Creating optimization v3 notification stream...", file=sys.stderr)
                stream_id = stream_client.create_v3_stream()
                print(f"Stream identifier: {stream_id}", file=sys.stderr)
            count = listen_v3(stream_client, listener, stream_id, options)
        else:
            count = listen_legacy(stream_client, listener, options)

        print(f"Received {count} event(s).", file=sys.stderr)
        return 0

    except (CrossworkAuthError, requests.RequestException, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
