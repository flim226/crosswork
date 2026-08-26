#!/usr/bin/env python3
"""
Script to retrieve a plan file from Crosswork Network Controller.
"""

import argparse
import base64
import getpass
import os
import sys
from datetime import datetime

import requests
import urllib3

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""

    def __init__(self, timeout: int = CONNECT_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(**kwargs)

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(*args, **kwargs)


def _create_session(verify_ssl: bool = True, timeout: int = CONNECT_TIMEOUT) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter(timeout=timeout)
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


def get_jwt(ip: str, username: str, password: str, verify_ssl: bool = True, port: int = BASE_PORT) -> str:
    """Authenticate to Crosswork and return the JWT token string."""
    session = _create_session(verify_ssl=verify_ssl)
    base_url = f"https://{ip}:{port}"
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def _resolve_credentials(username=None, password=None) -> tuple:
    """Resolve username and password from args, environment, or interactive prompt."""
    username = username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")

    password = password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")

    return username, password


def load_token_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


def default_jwt_path(ip: str) -> str:
    """Return the default JWT path created by cw_get_jwt.py for *ip*."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def get_plan(session: requests.Session, base_url: str, token: str, plan_name: str, plan_format: str, version: str) -> bytes:
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
        return base64.b64decode(output["planfile-content"])
    if "plan-file-content" in output:
        return base64.b64decode(output["plan-file-content"])
    return response.content


def get_dlm_node_coordinates(session: requests.Session, base_url: str, token: str) -> dict:
    """Return DLM Inventory coordinates keyed by case-insensitive hostname."""
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
        raise CrossworkAuthError("get_dlm_node_coordinates returned invalid JSON") from exc
    if not isinstance(nodes, list):
        raise CrossworkAuthError("get_dlm_node_coordinates returned an unexpected response format")

    coordinates = {}
    for node in nodes:
        hostname = node.get("host_name")
        geo = node.get("geo_info", {}).get("coordinates", {})
        latitude = geo.get("latitude", {}).get("value")
        longitude = geo.get("longitude", {}).get("value")
        if isinstance(hostname, str) and latitude is not None and longitude is not None:
            coordinates[hostname.casefold()] = (longitude, latitude)
    return coordinates


def update_plan_node_coordinates(plan_content: bytes, coordinates: dict) -> tuple:
    """Update Longitude and Latitude columns in a text plan's ``<Nodes>`` table.

    Inventory host names and plan node names are matched case-insensitively.  Nodes
    without both coordinates, and plan nodes without an inventory match, are left
    unchanged.
    """
    try:
        plan_text = plan_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("--geoloc requires a UTF-8 text plan (.txt), not a binary plan") from exc

    lines = plan_text.splitlines(keepends=True)
    in_nodes_table = False
    header_indexes = None
    updated = 0
    plan_node_count = 0

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

    return "".join(lines).encode("utf-8"), updated, plan_node_count


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve a plan file from Crosswork Network Controller",
        epilog=(
            "When --username, --password, and --jwt are omitted, the script uses "
            "~/.crosswork/<ip>.jwt when it exists. Otherwise, credentials are resolved "
            "in order: CLI flags > environment variables "
            f"({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument("--port", type=int, default=BASE_PORT,
                        help=f"Crosswork HTTPS port (default: {BASE_PORT})")
    parser.add_argument("--username", "-u", default=None,
                        help=f"Username (or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument("--planfile", "-f",
                        help="Plan file name (must end with .txt or .pln). Default: <ip>.<timestamp>.<format>")
    parser.add_argument("--format", choices=["txt", "pln"], default="txt",
                        help="Plan file format (default: txt). Used for default filename when --planfile is not provided.")
    parser.add_argument("--version", "-v", default="", help="Planfile schema version (default: empty string)")
    parser.add_argument("--geoloc", action="store_true",
                        help="Populate <Nodes> Longitude and Latitude from the DLM Inventory API (text plans only)")
    parser.add_argument("--timeout", type=int, default=CONNECT_TIMEOUT,
                        help=f"HTTP timeout in seconds (default: {CONNECT_TIMEOUT})")

    args = parser.parse_args()

    # Generate default filename if not provided, using --format for extension
    if not args.planfile:
        now = datetime.now().astimezone()
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        utc_offset = now.strftime("%z")
        args.planfile = f"{args.ip}.{timestamp}{utc_offset}.{args.format}"
        file_format = args.format
    else:
        ext = os.path.splitext(args.planfile)[1].lower()
        if ext == ".txt":
            file_format = "txt"
        elif ext == ".pln":
            file_format = "pln"
        else:
            print(f"Error: --planfile must have .txt or .pln extension, got '{ext}'")
            sys.exit(1)

    if args.geoloc and file_format != "txt":
        print("Error: --geoloc requires a .txt plan file; binary .pln files cannot be updated in place")
        sys.exit(1)

    verify_ssl = not args.insecure
    if args.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

    base_url = f"https://{args.ip}:{args.port}"
    stored_jwt_path = default_jwt_path(args.ip)

    try:
        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}")
        elif not args.username and not args.password and os.path.isfile(stored_jwt_path):
            token = load_token_from_file(stored_jwt_path)
            print(f"Using JWT from {stored_jwt_path}")
        else:
            print(f"Authenticating to {args.ip}...")
            username, password = _resolve_credentials(args.username, args.password)
            token = get_jwt(args.ip, username, password, verify_ssl=verify_ssl, port=args.port)

        session = _create_session(verify_ssl=verify_ssl, timeout=args.timeout)

        print(f"Retrieving plan: {args.planfile}...")
        plan_content = get_plan(session, base_url, token, args.planfile, file_format, args.version)

        if args.geoloc:
            print("Retrieving node coordinates from DLM Inventory...")
            coordinates = get_dlm_node_coordinates(session, base_url, token)
            plan_content, updated_nodes, plan_nodes = update_plan_node_coordinates(plan_content, coordinates)
            print(f"Updated coordinates for {updated_nodes} of {plan_nodes} plan nodes.")

        with open(args.planfile, "wb") as f:
            f.write(plan_content)

        print(f"Plan saved to: {args.planfile}")

    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
