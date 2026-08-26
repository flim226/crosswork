#!/usr/bin/env python3
"""Retrieve Segment Routing policy operational data from Crosswork CNC."""

import argparse
import getpass
import json
import os
import sys

import requests
import urllib3

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
SR_POLICIES_PATH = (
    "/crosswork/nbi/optima/v2/restconf/data/"
    "cisco-crosswork-segment-routing-policy:sr-policies"
)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTP adapter that applies a default timeout to every request."""

    def __init__(self, timeout: int = CONNECT_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(**kwargs)

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(*args, **kwargs)


def _create_session(verify_ssl: bool = True, timeout: int = CONNECT_TIMEOUT) -> requests.Session:
    """Create an HTTP session with configurable certificate verification and timeout."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter(timeout=timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def check_response(resp: requests.Response, context: str) -> None:
    """Raise a useful error for a failed Crosswork request."""
    if resp.ok:
        return
    body = (resp.text or "").strip()
    if len(body) > 500:
        body = body[:500] + "..."
    if not body:
        body = "<empty response body>"
    reason = f" {resp.reason}" if resp.reason else ""
    raise CrossworkAuthError(f"{context} returned HTTP {resp.status_code}{reason}: {body}")


def get_ticket(session: requests.Session, base_url: str, username: str, password: str) -> str:
    """Obtain an SSO ticket-granting ticket."""
    response = session.post(
        f"{base_url}/crosswork/sso/v1/tickets",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check_response(response, "get_ticket")

    location = response.headers.get("Location", "")
    if location:
        ticket = location.rstrip("/").split("/")[-1]
    else:
        try:
            ticket = response.json().get("ticket", "")
        except ValueError:
            ticket = response.text.strip()
    if not ticket:
        raise CrossworkAuthError("Could not extract ticket from the SSO response")
    return ticket


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange an SSO ticket for a JWT bearer token."""
    response = session.post(
        f"{base_url}/crosswork/sso/v2/tickets/jwt",
        data={"tgt": ticket, "service": f"{base_url}/app-dashboard"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check_response(response, "get_token")
    try:
        token = response.json().get("token", "")
    except ValueError:
        token = response.text.strip()
    if not token:
        raise CrossworkAuthError("Could not extract JWT from the SSO response")
    return token


def get_jwt(ip: str, username: str, password: str, verify_ssl: bool = True, port: int = BASE_PORT) -> str:
    """Authenticate to Crosswork and return a JWT bearer token."""
    session = _create_session(verify_ssl=verify_ssl)
    base_url = f"https://{ip}:{port}"
    return get_token(session, base_url, get_ticket(session, base_url, username, password))


def _resolve_credentials(username=None, password=None) -> tuple:
    """Resolve credentials from CLI flags, environment, then interactive input."""
    username = username or os.environ.get(ENV_USERNAME) or input("Username: ")
    password = password or os.environ.get(ENV_PASSWORD) or getpass.getpass("Password: ")
    return username, password


def load_token_from_file(path: str) -> str:
    """Read a JWT token from a file."""
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


def default_jwt_path(ip: str) -> str:
    """Return the default path written by cw_get_jwt.py for ``ip``."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def get_sr_policies(session: requests.Session, base_url: str, token: str) -> dict:
    """Retrieve all Segment Routing policies from the CNC RESTCONF API."""
    response = session.get(
        f"{base_url}{SR_POLICIES_PATH}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/yang-data+json",
        },
    )
    check_response(response, "get_sr_policies")
    try:
        return response.json()
    except ValueError as exc:
        raise CrossworkAuthError("get_sr_policies returned invalid JSON") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve Segment Routing policy operational data from Crosswork CNC.",
        epilog=(
            "When --username, --password, and --jwt are omitted, the script uses "
            "~/.crosswork/<ip>.jwt when it exists. Otherwise, credentials are resolved "
            f"in order: CLI flags > environment variables ({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address or hostname")
    parser.add_argument("--port", type=int, default=BASE_PORT,
                        help=f"Crosswork HTTPS port (default: {BASE_PORT})")
    parser.add_argument("--username", "-u", default=None, help=f"Username (or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; prompts if omitted)")
    parser.add_argument("--jwt", "-j", help="Path to a JWT file (skips username/password authentication)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument("--timeout", type=int, default=CONNECT_TIMEOUT,
                        help=f"HTTP timeout in seconds (default: {CONNECT_TIMEOUT})")
    args = parser.parse_args()

    verify_ssl = not args.insecure
    stored_jwt_path = default_jwt_path(args.ip)
    base_url = f"https://{args.ip}:{args.port}"

    try:
        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}", file=sys.stderr)
        elif not args.username and not args.password and os.path.isfile(stored_jwt_path):
            token = load_token_from_file(stored_jwt_path)
            print(f"Using JWT from {stored_jwt_path}", file=sys.stderr)
        else:
            print(f"Authenticating to {args.ip}...", file=sys.stderr)
            username, password = _resolve_credentials(args.username, args.password)
            token = get_jwt(args.ip, username, password, verify_ssl=verify_ssl, port=args.port)

        if args.insecure:
            print("WARNING: SSL verification disabled", file=sys.stderr)
        session = _create_session(verify_ssl=verify_ssl, timeout=args.timeout)
        policies = get_sr_policies(session, base_url, token)
        json.dump(policies, sys.stdout, indent=2)
        print()
    except (CrossworkAuthError, requests.RequestException, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
