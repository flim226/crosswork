#!/usr/bin/env python3
"""Script to manage tags on Crosswork Network Controller nodes."""

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


class CncClient:
    """Crosswork Network Controller API client."""

    def __init__(self, ip: str, token: str, *, port: int = BASE_PORT, verify_ssl: bool = True):
        self.base = f"https://{ip}:{port}"
        self._session = _create_session(verify_ssl=verify_ssl)
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _post(self, path: str, body: dict) -> dict:
        resp = self._session.post(f"{self.base}{path}", headers=self._headers, json=body)
        check_response(resp, f"POST {path}")
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        resp = self._session.put(f"{self.base}{path}", headers=self._headers, json=body)
        check_response(resp, f"PUT {path}")
        return resp.json()

    def get_nodes(self, host: str = None) -> list:
        body = {"filter": {"host_name": host}} if host else {}
        return self._post("/crosswork/inventory/v1/nodes/query", body).get("data", [])

    def create_tag(self, tag_name: str) -> dict:
        return self._post("/crosswork/inventory/v1/tags",
                          {"tags": [{"name": tag_name, "category": tag_name}]})

    def update_nodes(self, node_data_list: list) -> dict:
        return self._put("/crosswork/inventory/v1/nodes", {"data": node_data_list})

    def unassign_tags(self, node_data_list: list) -> dict:
        return self._put("/crosswork/inventory/v1/nodes/unassigntag", {"data": node_data_list})


def print_tags_table(nodes: list):
    """Print host/tags in tabular format."""
    if not nodes:
        print("No nodes found.")
        return

    rows = sorted(
        [(n.get("host_name", "N/A"), ", ".join(n.get("tag_names", [])) or "(none)") for n in nodes],
        key=lambda r: r[0],
    )
    host_w = max(len("Host"), max(len(r[0]) for r in rows))
    tags_w = max(len("Tags"), max(len(r[1]) for r in rows))

    print(f"{'Host':<{host_w}}  {'Tags':<{tags_w}}")
    print(f"{'-' * host_w}  {'-' * tags_w}")
    for host, tags in rows:
        print(f"{host:<{host_w}}  {tags:<{tags_w}}")


def print_job_result(result: dict, updated_items: list):
    """Print job result — errors on failure, updated hosts on success."""
    state = result.get("state", "unknown")
    print(f"Result: {state}")
    if state == "JOB_FAILED":
        for err in result.get("errors", []):
            print(f"  Error: {err}")
    else:
        for item in updated_items:
            print(f"  Updated: {item.get('host_name', 'N/A')}")


def cmd_get(client: CncClient, args):
    """Handle the --get command."""
    nodes = client.get_nodes(args.host)

    if args.raw:
        print(json.dumps({"data": nodes}, indent=2))
        return

    print_tags_table(nodes)


def cmd_add(client: CncClient, args):
    """Handle the --add command."""
    print(f"Creating tag '{args.tag}' in system...")
    try:
        client.create_tag(args.tag)
    except requests.exceptions.HTTPError:
        print(f"  Tag '{args.tag}' may already exist, continuing.")

    nodes = client.get_nodes(args.host)
    if not nodes:
        print("No nodes found.")
        return

    update_list = []
    for node in nodes:
        existing_tags = node.get("tag_names", [])
        if args.tag in existing_tags:
            print(f"  {node.get('host_name', 'N/A')}: tag already exists, skipping")
            continue
        node["tag_names"] = existing_tags + [args.tag]
        update_list.append(node)

    if not update_list:
        print("No nodes need updating.")
        return

    print(f"Adding tag '{args.tag}' to {len(update_list)} node(s)...")
    print_job_result(client.update_nodes(update_list), update_list)


def cmd_remove(client: CncClient, args):
    """Handle the --remove command."""
    nodes = client.get_nodes(args.host)
    if not nodes:
        print("No nodes found.")
        return

    remove_list = []
    for node in nodes:
        if args.tag not in node.get("tag_names", []):
            print(f"  {node.get('host_name', 'N/A')}: tag not present, skipping")
            continue
        remove_list.append({
            "inv_key_type": "UUID",
            "uuid": node.get("uuid"),
            "host_name": node.get("host_name"),
            "tag_names": [args.tag],
        })

    if not remove_list:
        print("No nodes need updating.")
        return

    print(f"Removing tag '{args.tag}' from {len(remove_list)} node(s)...")
    print_job_result(client.unassign_tags(remove_list), remove_list)


def main():
    parser = argparse.ArgumentParser(
        description="Manage tags on Crosswork Network Controller nodes",
        epilog=(
            "Credentials are resolved in order: CLI flags > environment variables "
            f"({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt.\n"
            "Use cw_get_jwt.py to obtain a JWT file for --jwt authentication."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("host", nargs="?", default=None, metavar="HOST",
                        help="Target hostname (all hosts if not specified)")
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
    parser.add_argument("--get", "--list", action="store_true", help="Get tags")
    parser.add_argument("--add", action="store_true", help="Add tag")
    parser.add_argument("--remove", "--rm", "--del", "--delete", action="store_true", help="Remove tag")
    parser.add_argument("--raw", action="store_true", help="Output raw JSON (use with --get)")
    parser.add_argument("--tag", help="Tag name (required with --add or --remove)")

    args = parser.parse_args()

    actions = [a for a in ("get", "add", "remove") if getattr(args, a)]
    if len(actions) == 0:
        parser.error("One of --get, --add, or --remove is required")
    if len(actions) > 1:
        parser.error("Cannot combine --get, --add, and --remove")
    if (args.add or args.remove) and not args.tag:
        parser.error("--tag is required when using --add or --remove")

    verify_ssl = not args.insecure
    if args.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

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

        client = CncClient(args.ip, token, port=args.port, verify_ssl=verify_ssl)

        if args.get:
            cmd_get(client, args)
        elif args.add:
            cmd_add(client, args)
        else:
            cmd_remove(client, args)
    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
