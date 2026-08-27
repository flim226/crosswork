#!/usr/bin/env python3
"""Obtain or decode a JWT token from Crosswork Network Controller."""

import argparse
import base64
import getpass
import json
import os
import stat
import sys
from datetime import datetime, timezone

import requests
import urllib3

BASE_PORT = 30603
CONNECT_TIMEOUT = 20

# Environment variable names for credential lookup
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""
    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = CONNECT_TIMEOUT
        return super().send(*args, **kwargs)


def _create_session(verify_ssl: bool = True) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
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


def get_jwt(ip: str, username: str, password: str, verify_ssl: bool = True) -> str:
    """Authenticate to Crosswork and return the JWT token string."""
    session = _create_session(verify_ssl=verify_ssl)
    base_url = f"https://{ip}:{BASE_PORT}"
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def decode_jwt(token: str) -> None:
    """Decode and print JWT header, payload, and metadata."""
    parts = token.strip().split(".")
    if len(parts) not in (2, 3):
        print("Error: not a valid JWT (expected 2 or 3 dot-separated parts)", file=sys.stderr)
        sys.exit(1)

    def _decode_part(part: str) -> dict:
        padding = 4 - len(part) % 4
        if padding != 4:
            part += "=" * padding
        return json.loads(base64.urlsafe_b64decode(part))

    header = _decode_part(parts[0])
    payload = _decode_part(parts[1])

    print("=== JWT Header ===")
    print(json.dumps(header, indent=2))

    print("\n=== JWT Payload ===")
    print(json.dumps(payload, indent=2))

    print("\n=== Token Details ===")
    if "sub" in payload:
        print(f"  Subject:    {payload['sub']}")
    if "iss" in payload:
        print(f"  Issuer:     {payload['iss']}")
    if "aud" in payload:
        aud = payload["aud"]
        if isinstance(aud, list):
            aud = ", ".join(aud)
        print(f"  Audience:   {aud}")

    now = datetime.now(timezone.utc)
    if "iat" in payload:
        iat = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
        print(f"  Issued At:  {iat.isoformat()} ({payload['iat']})")
    if "exp" in payload:
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        delta = exp - now
        expired = " [EXPIRED]" if delta.total_seconds() < 0 else f" [expires in {delta}]"
        print(f"  Expires:    {exp.isoformat()} ({payload['exp']}){expired}")
    if "nbf" in payload:
        nbf = datetime.fromtimestamp(payload["nbf"], tz=timezone.utc)
        print(f"  Not Before: {nbf.isoformat()} ({payload['nbf']})")

    if "scope" in payload:
        print(f"  Scope:      {payload['scope']}")
    if "roles" in payload:
        print(f"  Roles:      {payload['roles']}")

    has_sig = len(parts) == 3 and parts[2]
    print(f"  Signature:  {'present' if has_sig else 'none'}")


def _resolve_credentials(args) -> tuple:
    """Resolve username and password from args, environment, or interactive prompt.

    Priority: CLI arg > environment variable > interactive prompt.
    Password is never required on the command line to avoid process-list exposure.
    """
    username = args.username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")

    password = args.password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")

    return username, password


def _write_token_file(path: str, token: str) -> None:
    """Write token to file with restrictive permissions (owner-only read/write)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(
        description="Get or decode JWT token from Crosswork Network Controller",
        epilog=(
            "Credentials are resolved in order: CLI flags > environment variables "
            f"({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt.\n"
            "If IP is omitted and -f is given, decodes the JWT file instead of authenticating."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ip_positional",
        nargs="?",
        default=None,
        metavar="IP",
        help="CNC IP address or hostname (legacy positional form; use --ip)",
    )
    parser.add_argument("--ip", dest="ip_option", default=None,
                        metavar="IP", help="CNC IP address or hostname")
    parser.add_argument("--username", "-u", default=None,
                        help=f"Username (or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)")
    parser.add_argument("-f", "--filename", default=None,
                        help="JWT filename to save to or decode from (default: ~/.crosswork/<ip>.jwt)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    args = parser.parse_args()

    if args.ip_positional is not None and args.ip_option is not None:
        parser.error("provide the CNC IP either positionally or with --ip, not both")
    args.ip = args.ip_option if args.ip_option is not None else args.ip_positional

    # Decode mode: no IP provided, just decode the JWT file
    if args.ip is None:
        if not args.filename:
            parser.error("either provide an IP to authenticate, or -f FILENAME to decode a JWT")
        if not os.path.isfile(args.filename):
            print(f"Error: file not found: {args.filename}", file=sys.stderr)
            sys.exit(1)
        with open(args.filename, "r") as f:
            token = f.read().strip()
        decode_jwt(token)
        return

    # Auth mode: authenticate and save JWT
    username, password = _resolve_credentials(args)

    default_dir = os.path.expanduser("~/.crosswork")
    os.makedirs(default_dir, mode=0o700, exist_ok=True)
    output_file = args.filename if args.filename else os.path.join(default_dir, f"{args.ip}.jwt")

    if args.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

    try:
        print(f"Authenticating to {args.ip}...")
        token = get_jwt(args.ip, username, password, verify_ssl=not args.insecure)
        _write_token_file(output_file, token)
        print(f"JWT written to {output_file}")
    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
