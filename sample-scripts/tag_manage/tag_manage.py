#!/usr/bin/env python3
"""Script to manage tags on Crosswork Network Controller nodes."""

import argparse
import json
import sys
from urllib.parse import quote_plus

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_PORT = 30603


class CncClient:
    """Crosswork Network Controller API client."""

    def __init__(self, ip: str, username: str, password: str):
        self.base = f"https://{ip}:{BASE_PORT}"
        self._headers = {"Content-Type": "application/json"}
        self._authenticate(username, password)

    def _authenticate(self, username: str, password: str):
        form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(
            f"{self.base}/crosswork/sso/v1/tickets",
            data=f"username={quote_plus(username)}&password={quote_plus(password)}",
            headers=form_headers, verify=False,
        )
        resp.raise_for_status()
        tgt = resp.text.strip()

        resp = requests.post(
            f"{self.base}/crosswork/sso/v1/tickets/{tgt}",
            data=f"service={self.base}/app-dashboard",
            headers=form_headers, verify=False,
        )
        resp.raise_for_status()
        self._headers["Authorization"] = f"Bearer {resp.text.strip()}"

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(f"{self.base}{path}", headers=self._headers, json=body, verify=False)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        resp = requests.put(f"{self.base}{path}", headers=self._headers, json=body, verify=False)
        resp.raise_for_status()
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
        description="Manage tags on Crosswork Network Controller nodes"
    )
    parser.add_argument("host", nargs="?", default=None, metavar="HOST",
                        help="Target hostname (all hosts if not specified)")
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument("--username", required=True, help="Username")
    parser.add_argument("--password", required=True, help="Password")
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

    try:
        print(f"Authenticating to {args.ip}...")
        client = CncClient(args.ip, args.username, args.password)

        if args.get:
            cmd_get(client, args)
        elif args.add:
            cmd_add(client, args)
        else:
            cmd_remove(client, args)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        if e.response is not None:
            print(f"Response: {e.response.text}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"Connection Error: Could not connect to {args.ip}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
